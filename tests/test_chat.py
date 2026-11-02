"""Mocked Responses fixtures, never live inference evidence."""
import asyncio
from contextlib import asynccontextmanager
import json
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, ClientTimeout, web
from hermes_proxy_codex.chat import chat_app
from helpers import MODEL, listening
from helpers import Fake


def frame(kind, **values):
    return 'data: ' + json.dumps({'type': kind, **values}, ensure_ascii=False) + '\n\n'


def fixture(text='Héllo 🌱'):
    return (frame('response.output_text.delta', delta=text[:2])
            + frame('response.output_text.delta', delta=text[2:])
            + frame('response.completed', response={
                'model': MODEL, 'status': 'completed',
                'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}],
                'usage': {'input_tokens': 5, 'output_tokens': 3, 'total_tokens': 8}}))


def body(stream=False):
    return {'model': MODEL, 'messages': [
        {'role': 'system', 'content': 'Exact\n system  '},
        {'role': 'developer', 'content': 'Developer ü'},
        {'role': 'user', 'content': 'first'},
        {'role': 'assistant', 'content': 'prior answer'},
        {'role': 'user', 'content': 'last\n字'}], 'stream': stream}


class Chat(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def running(self, handler):
        upstream = web.Application()
        upstream.router.add_post('/responses', handler)
        async with listening(upstream) as target:
            adapter = Fake(target)
            with patch('hermes_proxy_codex.chat.codex_cloudflare_headers', return_value={
                    'originator': 'provider-helper', 'ChatGPT-Account-ID': 'private-account',
                    'x-openai-internal-codex-residency': 'private-region'}) as helper:
                async with chat_app(adapter) as app, listening(app) as url, ClientSession(timeout=ClientTimeout(total=5)) as client:
                    yield url, client, adapter, helper

    async def test_json_stream_integrity_headers_and_native_route(self):
        captures = []
        async def capture(request):
            captures.append((await request.read(), dict(request.headers)))
            return web.Response(text=fixture(), content_type='text/event-stream', headers={'ChatGPT-Account-ID': 'private-account'})
        async with self.running(capture) as (url, client, adapter, helper):
            for stream in (False, True):
                async with client.post(url+'/v1/chat/completions', json=body(stream), headers={
                        'X-Forwarded-Host': 'attacker.invalid', 'Authorization': 'Bearer caller',
                        'ChatGPT-Account-ID': 'caller-account', 'originator': 'caller',
                        'x-openai-internal-codex-residency': 'caller-region', 'X-Secret': 'caller-secret'}) as response:
                    self.assertEqual(response.status, 200)
                    raw = await response.text()
                    for secret in ('private-account', 'private-region', 'mock-bearer', 'caller-secret'):
                        self.assertNotIn(secret, raw)
                        self.assertNotIn(secret, str(response.headers))
                    if stream:
                        records = [json.loads(f[6:]) for f in raw.split('\n\n') if f.startswith('data: ') and f[6:] != '[DONE]']
                        self.assertEqual(records[0]['choices'][0]['delta'], {'role': 'assistant'})
                        self.assertEqual(''.join(r['choices'][0]['delta'].get('content', '') for r in records), 'Héllo 🌱')
                        self.assertEqual(records[-1]['choices'][0]['finish_reason'], 'stop')
                        self.assertTrue(raw.endswith('data: [DONE]\n\n'))
                        self.assertTrue(all(r['object'] == 'chat.completion.chunk' for r in records))
                    else:
                        data = json.loads(raw)
                        self.assertEqual(data['object'], 'chat.completion')
                        self.assertEqual(data['choices'][0]['message'], {'role': 'assistant', 'content': 'Héllo 🌱'})
                        self.assertEqual(data['choices'][0]['finish_reason'], 'stop')
                        self.assertEqual(data['usage'], {'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8})
                wire, headers = captures[-1]
                expected = {'model': MODEL, 'instructions': 'Exact\n system  \n\nDeveloper ü',
                            'input': [{'role': m['role'], 'content': [{'type': 'output_text' if m['role'] == 'assistant' else 'input_text', 'text': m['content']}]} for m in body()['messages'][2:]],
                            'tools': [], 'store': False, 'stream': True}
                self.assertEqual(json.loads(wire), expected)
                self.assertEqual(wire, json.dumps(expected).encode())
                self.assertEqual(headers['Authorization'], 'Bearer mock-bearer')
                self.assertEqual(headers['originator'], 'provider-helper')
                self.assertEqual(headers['ChatGPT-Account-ID'], 'private-account')
                self.assertEqual(headers['x-openai-internal-codex-residency'], 'private-region')
                self.assertNotIn('X-Secret', headers)
            self.assertEqual(helper.call_count, 2)
            # Public Responses route still uses installed byte-for-byte forwarding.
            raw = b'{"arbitrary": "native body"}'
            async with client.post(url+'/v1/responses', data=raw) as response:
                self.assertEqual(response.status, 200)
                await response.read()
            self.assertEqual(captures[-1][0], raw)

    async def test_browser_and_rebound_requests_never_reach_credentials(self):
        async def never(request):
            self.fail('Rejected browser request reached upstream')

        async with self.running(never) as (url, client, adapter, helper):
            for path in ('/v1/chat/completions', '/v1/responses', '/health'):
                for headers in (
                    {'Origin': 'https://attacker.invalid'},
                    {'Origin': 'null'},
                    {'Origin': url},
                    {'Sec-Fetch-Site': 'cross-site'},
                    {'Sec-Fetch-Mode': 'navigate'},
                    {'Host': 'attacker.invalid'},
                    {'Host': '127.0.0.1:1'},
                    {'Host': 'localhost'},
                ):
                    # text/plain is a browser simple request, so CORS alone is
                    # not sufficient to stop inference side effects.
                    async with client.post(url + path, data=json.dumps(body()), headers={
                        'Content-Type': 'text/plain', **headers
                    }) as response:
                        self.assertEqual(response.status, 403)
                        await response.read()
            self.assertEqual(adapter.calls, 0)
            helper.assert_not_called()

    async def test_large_single_line_terminal_below_response_limit(self):
        text = 'x' * 140_000
        raw = fixture(text)
        self.assertLess(len(raw.encode()), 1_000_000)

        async def reply(request):
            return web.Response(text=raw, content_type='text/event-stream')

        async with self.running(reply) as (url, client, adapter, helper):
            for stream in (False, True):
                async with client.post(url+'/v1/chat/completions', json=body(stream)) as response:
                    self.assertEqual(response.status, 200)
                    output = await response.text()
                    self.assertNotIn('"error"', output)
                    if stream:
                        chunks = [json.loads(frame[6:]) for frame in output.split('\n\n')
                                  if frame.startswith('data: ') and frame != 'data: [DONE]']
                        self.assertEqual(''.join(chunk['choices'][0]['delta'].get('content', '') for chunk in chunks), text)
                        self.assertTrue(output.endswith('data: [DONE]\n\n'))
                    else:
                        self.assertEqual(json.loads(output)['choices'][0]['message']['content'], text)

    async def test_validation_before_auth(self):
        async def never(request):
            self.fail('Validation reached upstream')
        bad = [None, [], {}, {'model': MODEL, 'messages': []}]
        for key, value in [('tools', []), ('functions', []), ('temperature', 0), ('max_tokens', 5),
                           ('max_completion_tokens', 5), ('response_format', {'type': 'json_object'}),
                           ('stream', 1), ('n', 2), ('n', True), ('model', ''), ('stream_options', {})]:
            bad.append({**body(), key: value})
        for msg in ({'role': 'tool', 'content': 'x'}, {'role': 'user', 'content': None},
                    {'role': [], 'content': 'x'}, {'role': 'user', 'content': [{'type': 'text', 'text': 'x'}]},
                    {'role': 'user', 'content': 'x', 'name': 'x'}):
            bad.append({**body(), 'messages': [msg]})
        bad.extend([{**body(), 'messages': [body()['messages'][-1]] * 2},
                    {**body(), 'messages': [body()['messages'][0]]},
                    {**body(), 'messages': [body()['messages'][-1], body()['messages'][0]]}])
        async with self.running(never) as (url, client, adapter, helper):
            for value in bad:
                async with client.post(url+'/v1/chat/completions', json=value) as response:
                    self.assertEqual(response.status, 400, value)
                    await response.read()
            for suffix, data in (('', b'{broken'), ('?url=http://attacker.invalid', b'{}')):
                async with client.post(url+'/v1/chat/completions'+suffix, data=data) as response:
                    self.assertEqual(response.status, 400)
                    await response.read()
            async with client.get(url+'/v1/chat/completions') as response:
                self.assertEqual(response.status, 405)
                await response.read()
            self.assertEqual(adapter.calls, 0)
            helper.assert_not_called()

    async def test_http_failures_sanitized_no_retry(self):
        for status in (400, 401, 429, 500, 302):
            calls = []
            async def reject(request):
                calls.append(1)
                return web.Response(status=status, text='private-account mock-bearer', headers={'Location': 'http://attacker.invalid'})
            async with self.running(reject) as (url, client, adapter, helper):
                async with client.post(url+'/v1/chat/completions', json=body()) as response:
                    self.assertEqual(response.status, status if status != 302 else 502)
                    self.assertNotIn('mock-bearer', await response.text())
                    self.assertNotIn('Location', response.headers)
                self.assertEqual(len(calls), 1)

    async def test_sse_failures_and_truncation(self):
        failures = ['', 'data: [DONE]\n\n', 'data: {broken}\n\n', 'data: {}',
                    frame('error', error={'message': 'mock-bearer'}), frame('response.failed'),
                    frame('response.incomplete'), fixture()+frame('response.failed'),
                    fixture().replace('"completed"', '"incomplete"'),
                    fixture().replace(MODEL, 'wrong-model'),
                    fixture().replace('"total_tokens": 8', '"total_tokens": null'),
                    fixture().replace('"delta": "Hé"', '"delta": "wrong"')]
        for raw in failures:
            async def reply(request):
                return web.Response(text=raw, content_type='text/event-stream')
            async with self.running(reply) as (url, client, adapter, helper):
                for stream in (False, True):
                    async with client.post(url+'/v1/chat/completions', json=body(stream)) as response:
                        text = await response.text()
                        self.assertEqual(response.status, 200 if stream else 502, raw)
                        self.assertIn('"error"', text)
                        self.assertNotIn('[DONE]', text)
                        self.assertNotIn('"finish_reason": "stop"', text)
                        self.assertNotIn('mock-bearer', text)

    async def test_disconnect_cancels_upstream(self):
        closed = asyncio.Event()
        async def slow(request):
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            try:
                while True:
                    await response.write(frame('response.output_text.delta', delta='x').encode())
                    await asyncio.sleep(.01)
            finally:
                closed.set()
        async with self.running(slow) as (url, client, adapter, helper):
            with self.assertLogs(level='WARNING'):
                response = await client.post(url+'/v1/chat/completions', json=body(True))
                await response.content.readany()
                response.close()
                await asyncio.wait_for(closed.wait(), 2)
                await asyncio.sleep(.02)

    async def test_octet_stream_sse_requires_valid_completion(self):
        for data in (fixture(), 'not an SSE response'):
            async def reply(request):
                return web.Response(text=data, content_type='application/octet-stream')
            async with self.running(reply) as (url, client, adapter, helper):
                for stream in (False, True):
                    async with client.post(url+'/v1/chat/completions', json=body(stream)) as response:
                        raw = await response.text()
                        if data == fixture():
                            self.assertEqual(response.status, 200)
                            if stream:
                                self.assertTrue(raw.endswith('data: [DONE]\n\n'))
                            else:
                                self.assertEqual(json.loads(raw)['choices'][0]['message']['content'], 'Héllo 🌱')
                        else:
                            self.assertEqual(response.status, 200 if stream else 502)
                            self.assertIn('"error"', raw)
                            self.assertNotIn('[DONE]', raw)

    async def test_auth_failure_is_sanitized(self):
        async def never(request):
            self.fail('Auth failure reached upstream')
        async with self.running(never) as (url, client, adapter, helper):
            adapter.fail = True
            async with client.post(url+'/v1/chat/completions', json=body()) as response:
                self.assertEqual(response.status, 401)
                self.assertNotIn('Sanitized auth failure', await response.text())

    async def test_terminal_only_and_empty_instructions(self):
        captures = []
        terminal_only = fixture('terminal text').split('\n\n')[2] + '\n\n'
        async def capture(request):
            captures.append(await request.json())
            return web.Response(text=terminal_only, content_type='text/event-stream')
        async with self.running(capture) as (url, client, adapter, helper):
            for stream in (False, True):
                async with client.post(url+'/v1/chat/completions', json={
                        'model': MODEL, 'messages': [{'role': 'user', 'content': '  exact\n字  '}], 'stream': stream}) as response:
                    self.assertEqual(response.status, 200)
                    text = await response.text()
                    self.assertIn('terminal text', text)
                    self.assertNotIn('"error"', text)
                self.assertEqual(captures[-1]['instructions'], '')
                self.assertEqual(captures[-1]['input'][0]['content'][0]['text'], '  exact\n字  ')
                self.assertEqual(captures[-1]['tools'], [])

    async def test_size_limits_and_non_sse(self):
        async def non_sse(request):
            return web.json_response({'secret': 'private-account'})
        async with self.running(non_sse) as (url, client, adapter, helper):
            for stream in (False, True):
                async with client.post(url+'/v1/chat/completions', json=body(stream)) as response:
                    self.assertEqual(response.status, 502)
                    self.assertNotIn('private-account', await response.text())
            calls = adapter.calls
            async with client.post(url+'/v1/chat/completions', data=b' ' * 1_000_001) as response:
                self.assertEqual(response.status, 400)
                await response.read()
            self.assertEqual(adapter.calls, calls)

    async def test_abrupt_upstream_disconnect(self):
        async def broken(request):
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            await response.write(frame('response.output_text.delta', delta='partial').encode())
            request.transport.close()
            return response
        async with self.running(broken) as (url, client, adapter, helper):
            with self.assertLogs('hermes_cli.proxy.server', level='WARNING'):
                for stream in (False, True):
                    async with client.post(url+'/v1/chat/completions', json=body(stream)) as response:
                        self.assertEqual(response.status, 200 if stream else 502)
                        text = await response.text()
                        self.assertIn('"error"', text)
                        self.assertNotIn('[DONE]', text)
                        self.assertNotIn('"finish_reason": "stop"', text)
