"""Actual Hermes forwarding code, local synthetic upstream only."""
import unittest
from aiohttp import ClientSession, web
from hermes_cli.proxy.server import create_app
from helpers import Fake, listening


class Native(unittest.IsolatedAsyncioTestCase):
    async def test_passthrough_and_path_allowlist(self):
        captures = []

        async def capture(request):
            captures.append((await request.read(), dict(request.headers), request.query_string))
            return web.Response(body=b'synthetic upstream bytes')

        upstream = web.Application()
        upstream.router.add_post('/responses', capture)
        async with listening(upstream) as target:
            adapter = Fake(target)
            async with listening(create_app(adapter)) as url, ClientSession() as client:
                raw = b'{"model":"synthetic-test-model","input":"test"}'
                async with client.post(url+'/v1/responses?check=1', data=raw, headers={
                        'Authorization': 'Bearer unused-caller', 'originator': 'test-client',
                        'ChatGPT-Account-ID': 'synthetic-account'}) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(await response.read(), b'synthetic upstream bytes')
                self.assertEqual(captures[0][0], raw)
                self.assertEqual(captures[0][1]['Authorization'], 'Bearer mock-bearer')
                self.assertEqual(captures[0][1]['originator'], 'test-client')
                self.assertEqual(captures[0][2], 'check=1')
                for path in ('models', 'responses/other', 'chat/completions'):
                    async with client.post(url+'/v1/'+path) as response:
                        self.assertEqual(response.status, 404)
                        await response.read()
                self.assertEqual(adapter.calls, 1)

    async def test_auth_error_and_no_retry(self):
        async with listening(create_app(Fake('http://127.0.0.1:1', True))) as url, ClientSession() as client:
            async with client.post(url+'/v1/responses') as response:
                self.assertEqual(response.status, 401)
                await response.read()
        for status in (400, 401, 429, 500):
            calls = []

            async def reject(request):
                calls.append(1)
                return web.json_response({'error': 'synthetic failure'}, status=status)

            app = web.Application()
            app.router.add_post('/responses', reject)
            async with listening(app) as target, listening(create_app(Fake(target))) as url, ClientSession() as client:
                async with client.post(url+'/v1/responses', json={}) as response:
                    self.assertEqual(response.status, status)
                    await response.read()
            self.assertEqual(len(calls), 1)
