"""Offline HTTP contract and real Hermes facade delegation."""
import argparse
import asyncio
import importlib.metadata
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from aiohttp import ClientSession
from hermes_proxy_codex.model_proxy import (
    BODY_LIMIT, model_proxy_app, parse_request, register, serve, setup_cli,
)
from hermes_proxy_codex.server import listening


def result(text="answer"):
    return SimpleNamespace(text=text, model="actual-model", provider="synthetic",
                           usage=SimpleNamespace(input_tokens=7, output_tokens=3, total_tokens=10))


class Fake:
    def __init__(self):
        self.calls = []
        self.failure = None

    async def acomplete(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure
        await asyncio.sleep(0)
        return result(kwargs["messages"][-1]["content"])


class Validation(unittest.TestCase):
    def test_numbers_and_schema(self):
        base = {"messages": [{"role": "user", "content": "hello"}]}
        for field, invalid in {
            "max_tokens": [True, False, 0, -1, 32769, 1.2, 10**1000, "3", None, float("nan"), float("inf")],
            "temperature": [True, -1, 3, "1", None, float("nan"), float("inf")],
            "timeout": [True, 0, 301, "1", None, float("nan"), float("inf")],
            "stream": [True, 0, None, "false"], "n": [True, 0, 2, 1.0],
            "model": [None, "", "secret\nheader", {}, True],
            "messages": [None, [], {}, [{"role": "tool", "content": "x"}],
                         [{"role": "user", "content": []}],
                         [{"role": "user", "content": "x", "name": "x"}]],
            "tools": [[]], "provider": ["evil"], "base_url": ["http://example.com"],
        }.items():
            for value in invalid:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    parse_request({**base, field: value})
        with self.assertRaises(ValueError):
            parse_request({**base, "model": "other"}, model="fixed")
        parsed = parse_request({**base, "model": "chosen", "n": 1, "stream": False,
                                "max_tokens": 20, "temperature": 0, "timeout": 1})
        self.assertEqual(parsed["model"], "chosen")
        self.assertEqual(parsed["timeout"], 1)

    def test_cli(self):
        parser = argparse.ArgumentParser()
        setup_cli(parser)
        self.assertEqual(parser.parse_args([]).port, 8647)
        from contextlib import redirect_stderr
        import io
        for args in (["--port", "-1"], ["--port", "65536"], ["--timeout", "nan"],
                     ["--timeout", "inf"], ["--timeout", "0"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(args)

    def test_real_registration(self):
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import parse_manifest_file
        root = Path(__file__).resolve().parents[1]
        manifest = parse_manifest_file(root / "plugin.yaml", root, "user", "")
        self.assertEqual(set(manifest.capabilities), {"llm.model_override", "llm.provider_override"})
        manager = PluginManager()
        self.assertTrue(hasattr(PluginContext, "llm"))
        manager._load_plugin(manifest)
        loaded = manager._plugins["model-proxy"]
        self.assertIsNone(loaded.error)
        self.assertTrue(loaded.enabled)
        self.assertEqual(loaded.tools_registered, [])
        self.assertEqual(loaded.hooks_registered, [])
        entry = manager._cli_commands["model-proxy"]
        parser = argparse.ArgumentParser()
        entry["setup_fn"](parser)
        self.assertEqual(parser.parse_args([]).port, 8647)
        self.assertIsNotNone(entry["handler_fn"])
        directory = manager._load_directory_module(manifest)
        self.assertIs(directory.register, register)
        manager.unload()
        self.assertNotIn("model-proxy", manager._cli_commands)

    def test_directory_doctor(self):
        import shutil
        import tempfile
        from hermes_cli.plugin_dev import doctor_plugin
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary) / "model-proxy"
            stage.mkdir()
            for name in ("plugin.yaml", "__init__.py"):
                shutil.copy2(root / name, stage / name)
            shutil.copytree(root / "src", stage / "src",
                            ignore=shutil.ignore_patterns("__pycache__"))
            report = doctor_plugin(stage)
            self.assertTrue(report.ok, report.format_text())
            self.assertEqual(report.findings, [], report.format_text())
            self.assertEqual(report.registered_tools, ())
            self.assertEqual(report.registered_hooks, ())

    def test_wheel_discovery(self):
        eps = importlib.metadata.entry_points(group="hermes_agent.plugins")
        ep, = [e for e in eps if e.name == "model-proxy"]
        self.assertIs(ep.load(), register)
        from hermes_cli.plugins_discovery import discover_entrypoint_manifests
        manifest, = [m for m in discover_entrypoint_manifests() if m.name == "model-proxy"]
        self.assertEqual(set(manifest.capabilities), {"llm.model_override", "llm.provider_override"})


class HTTP(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = Fake()
        self.client = ClientSession()
        self.listener = listening(model_proxy_app(self.fake))
        self.url = await self.listener.__aenter__()

    async def asyncTearDown(self):
        await self.client.close()
        await self.listener.__aexit__(None, None, None)

    async def post(self, payload=None, **kwargs):
        if payload is None:
            payload = {"messages": [{"role": "user", "content": "hello"}]}
        async with self.client.post(self.url + "/v1/chat/completions", json=payload, **kwargs) as response:
            return response.status, await response.json()

    async def test_success_and_concurrency(self):
        async def request(i):
            status, body = await self.post({"messages": [{"role": "user", "content": str(i)}]})
            self.assertEqual(status, 200)
            self.assertEqual(body["choices"][0]["message"]["content"], str(i))
            self.assertEqual(body["usage"], {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
            self.assertEqual(body["model"], "actual-model")
        await asyncio.gather(*(request(i) for i in range(12)))
        self.assertNotIn("model", self.fake.calls[0])
        self.assertNotIn("profile", self.fake.calls[0])

    async def test_path_shaped_model_selection_and_attribution(self):
        path_model = "/app/models/example.gguf"
        parser = argparse.ArgumentParser()
        setup_cli(parser)
        self.assertEqual(parser.parse_args(["--model", path_model]).model, path_model)

        class PathModel(Fake):
            async def acomplete(self, **kwargs):
                reply = await super().acomplete(**kwargs)
                reply.model = path_model
                return reply

        fake = PathModel()
        async with listening(model_proxy_app(fake, model=path_model)) as url:
            for explicit in (False, True):
                payload: dict[str, object] = {"messages": [{"role": "user", "content": "hello"}]}
                if explicit:
                    payload["model"] = path_model
                async with self.client.post(url + "/v1/chat/completions", json=payload) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual((await response.json())["model"], path_model)
                self.assertEqual(fake.calls[-1]["model"], path_model)
        async with listening(model_proxy_app(fake)) as url:
            async with self.client.post(url + "/v1/chat/completions", json={
                "messages": [{"role": "user", "content": "hello"}]
            }) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["model"], path_model)
            self.assertNotIn("model", fake.calls[-1])

    async def test_browser_and_credentials(self):
        for headers, expected in [({"Origin": "null"}, 403), ({"Sec-Fetch-Site": "none"}, 403),
                                  ({"Host": "localhost"}, 403), ({"Authorization": "secret"}, 400),
                                  ({"X-API-Key": "secret"}, 400), ({"Cookie": "secret"}, 400)]:
            status, _ = await self.post(headers=headers)
            self.assertEqual(status, expected)
        self.assertEqual(self.fake.calls, [])

    async def test_bad_json_and_body_limit(self):
        for raw, expected in [(b'{', 400), (b'[]', 400),
                              (b'{"messages":[],"messages":[]}', 400),
                              (b'{"temperature":NaN}', 400), (b'\xff', 400),
                              (b' ' * (BODY_LIMIT + 1), 413)]:
            async with self.client.post(self.url + '/v1/chat/completions', data=raw,
                                        headers={"Content-Type": "application/json"}) as response:
                self.assertEqual(response.status, expected)
                await response.read()
        self.assertEqual(self.fake.calls, [])

    async def test_sanitized_errors(self):
        for failure, status in [(RuntimeError("token-secret prompt-secret"), 502),
                                (PermissionError("token-secret prompt-secret"), 403)]:
            self.fake.failure = failure
            actual, body = await self.post()
            self.assertEqual(actual, status)
            self.assertNotIn("secret", json.dumps(body))

    async def test_real_context_facade_and_trust(self):
        from agent.plugin_llm import make_plugin_llm_for_test, _TrustPolicy
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest
        calls = []
        async def host(**kwargs):
            calls.append(kwargs)
            return "synthetic", "actual-model", SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="host answer"))],
                usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3, total_tokens=10))
        ctx = PluginContext(PluginManifest(name="model-proxy"), PluginManager())
        for allowed in (False, True):
            ctx._llm = make_plugin_llm_for_test(plugin_id="model-proxy", policy=_TrustPolicy(
                plugin_id="model-proxy", allow_model_override=allowed,
                allow_provider_override=allowed), async_caller=host)
            async with listening(model_proxy_app(ctx.llm, provider="synthetic")) as url:
                async with self.client.post(url + '/v1/chat/completions', json={
                    "model": "chosen", "messages": [{"role": "developer", "content": "instruction"},
                                                       {"role": "user", "content": "hello"}]}) as response:
                    self.assertEqual(response.status, 200 if allowed else 403)
                    body = await response.json()
            if not allowed:
                self.assertEqual(calls, [])
            else:
                self.assertEqual(calls[0]["model_override"], "chosen")
                self.assertEqual(calls[0]["provider_override"], "synthetic")
                self.assertIsNone(calls[0]["profile_override"])
                self.assertEqual(body["choices"][0]["message"]["content"], "host answer")
                self.assertEqual(body["usage"]["total_tokens"], 10)

    async def test_default_host_selection_and_allowlist_denial(self):
        from agent.plugin_llm import make_plugin_llm_for_test, _TrustPolicy
        calls = []
        async def host(**kwargs):
            calls.append(kwargs)
            return "synthetic", "active-model", SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="active answer"))],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3))
        llm = make_plugin_llm_for_test(plugin_id="model-proxy", policy=_TrustPolicy(
            plugin_id="model-proxy", allow_model_override=True,
            allowed_models=frozenset({"allowed-model"})), async_caller=host)
        async with listening(model_proxy_app(llm)) as url:
            for model, expected in [(None, 200), ("denied-model", 403)]:
                body = {"messages": [{"role": "user", "content": "hello"}]}
                if model is not None:
                    body["model"] = model
                async with self.client.post(url + '/v1/chat/completions', json=body) as response:
                    self.assertEqual(response.status, expected)
                    result = await response.json()
                    if expected == 200:
                        self.assertEqual(result["model"], "active-model")
            self.assertEqual(len(calls), 1)
            self.assertIsNone(calls[0]["model_override"])
            self.assertIsNone(calls[0]["provider_override"])

    async def test_unsupported_routes_and_query(self):
        for path, expected in [("/v1/responses", 404), ("/v1/chat/completions?model=x", 400)]:
            async with self.client.post(self.url + path, json={}) as response:
                self.assertEqual(response.status, expected)
                await response.read()
        self.assertEqual(self.fake.calls, [])
        for malformed in (None, [], 1, {"messages": [{"role": "user", "content": []}]},
                          {"messages": [{"role": "user", "content": "x"}], "stream": True}):
            async with self.client.post(self.url + '/v1/chat/completions',
                                        data=json.dumps(malformed),
                                        headers={"Content-Type": "application/json"}) as response:
                self.assertEqual(response.status, 400)
                await response.read()
        self.assertEqual(self.fake.calls, [])

    async def test_disconnect_and_timeout_cleanup(self):
        started, ended = asyncio.Event(), asyncio.Event()
        class Blocking:
            async def acomplete(self, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    ended.set()
        async with listening(model_proxy_app(Blocking(), timeout=0.1)) as url:
            async with self.client.post(url + '/v1/chat/completions', json={
                "messages": [{"role": "user", "content": "hello"}]}) as response:
                self.assertEqual(response.status, 504)
                await response.read()
            await asyncio.wait_for(ended.wait(), 1)
        started.clear()
        ended.clear()
        async with listening(model_proxy_app(Blocking())) as url:
            from urllib.parse import urlsplit
            port = urlsplit(url).port
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = b'{"messages":[{"role":"user","content":"hello"}]}'
            writer.write((f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                          f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body)
            await writer.drain()
            await asyncio.wait_for(started.wait(), 1)
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(ended.wait(), 1)
            self.assertEqual(await reader.read(), b'')

    async def test_foreground_lifecycle(self):
        from contextlib import redirect_stdout
        import io
        from unittest.mock import patch
        output = io.StringIO()
        callbacks = []
        removed = []
        loop = asyncio.get_running_loop()
        with patch.object(loop, "add_signal_handler", side_effect=lambda sig, fn: callbacks.append((sig, fn))), \
                patch.object(loop, "remove_signal_handler", side_effect=removed.append), redirect_stdout(output):
            task = asyncio.create_task(serve(self.fake, argparse.Namespace(
                port=0, model=None, provider=None, timeout=1)))
            try:
                for _ in range(100):
                    if output.getvalue():
                        break
                    await asyncio.sleep(0.01)
                url = output.getvalue().strip().removesuffix("/v1")
                self.assertTrue(url.startswith("http://127.0.0.1:"))
                async with self.client.get(url + "/health") as response:
                    self.assertEqual(response.status, 200)
                    await response.read()
                callbacks[0][1]()
                await asyncio.wait_for(task, 1)
                self.assertEqual(removed, [sig for sig, _ in callbacks])
                from aiohttp import ClientConnectorError
                with self.assertRaises(ClientConnectorError):
                    await self.client.get(url + "/health")
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
