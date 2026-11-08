"""Owned task routing through the real host planner, with synthetic transport."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import ClientSession
from hermes_model_proxy.model_proxy import (
    REFLECTION_HIGH_TASK, model_proxy_app, parse_request, register, verify_high_config,
)
from hermes_model_proxy.server import listening


@patch("hermes_cli.runtime_provider.resolve_requested_provider", return_value="openai-codex")
class Reasoning(unittest.IsolatedAsyncioTestCase):
    def test_validation_and_config(self, selection):
        base = {"messages": [{"role": "user", "content": "hello"}]}
        self.assertNotIn("task", parse_request(base))
        for value in (None, True, 1, [], {}, "low", "HIGH", "high "):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_request({**base, "reasoning_effort": value})
        for field in ("task", "extra_body", "reasoning"):
            with self.assertRaises(ValueError):
                parse_request({**base, field: {}})
        self.assertEqual(parse_request({**base, "reasoning_effort": "high"})["task"],
                         REFLECTION_HIGH_TASK)
        for task in ({}, {"reasoning_effort": "high"}):
            with patch("hermes_cli.config.load_config_readonly", return_value={
                    "auxiliary": {REFLECTION_HIGH_TASK: task}}):
                verify_high_config()
        selection.return_value = "other"
        with self.assertRaises(PermissionError):
            verify_high_config()
        selection.return_value = "openai-codex"
        for task in ({"reasoning_effort": "low"}, {"reasoning_effort": None},
                     {"extra_body": {"reasoning": {"effort": "low"}}},
                     {"provider": "other"}, {"fallback_chain": [{"provider": "other"}]}):
            with patch("hermes_cli.config.load_config_readonly", return_value={
                    "auxiliary": {REFLECTION_HIGH_TASK: task}}), self.assertRaises(PermissionError):
                verify_high_config()

    async def test_http_fail_closed(self, selection):
        from unittest.mock import AsyncMock
        llm = SimpleNamespace(acomplete=AsyncMock())
        body = {"model": "gpt-6.1-sol", "reasoning_effort": "high",
                "messages": [{"role": "user", "content": "hello"}]}
        for provider, check in ((None, None), ("other", verify_high_config),
                                ("openai-codex", None),
                                ("openai-codex", lambda: (_ for _ in ()).throw(PermissionError()))):
            async with listening(model_proxy_app(llm, provider=provider,
                                                 high_config_check=check)) as url, ClientSession() as session:
                async with session.post(url + "/v1/chat/completions", json=body) as response:
                    self.assertEqual(response.status, 403)
        llm.acomplete.assert_not_called()

    async def test_wrong_actual_route_rejected(self, selection):
        from unittest.mock import AsyncMock
        body = {"model": "gpt-6.1-sol", "reasoning_effort": "high",
                "messages": [{"role": "user", "content": "hello"}]}
        for provider, model in (("other", "gpt-6.1-sol"), ("openai-codex", "other")):
            llm = SimpleNamespace(acomplete=AsyncMock(return_value=SimpleNamespace(
                provider=provider, model=model)))
            with patch("hermes_cli.config.load_config_readonly", return_value={}):
                async with listening(model_proxy_app(
                        llm, high_config_check=verify_high_config)) as url, ClientSession() as session:
                    async with session.post(url + "/v1/chat/completions", json=body) as response:
                        self.assertEqual(response.status, 502)
            self.assertNotIn("provider", llm.acomplete.call_args.kwargs)

    async def test_wrong_active_provider_rejected_before_dispatch(self, selection):
        from unittest.mock import AsyncMock
        selection.return_value = "other"
        llm = SimpleNamespace(acomplete=AsyncMock())
        body = {"model": "gpt-6.1-sol", "reasoning_effort": "high",
                "messages": [{"role": "user", "content": "hello"}]}
        async with listening(model_proxy_app(
                llm, high_config_check=verify_high_config)) as url, ClientSession() as session:
            async with session.post(url + "/v1/chat/completions", json=body) as response:
                self.assertEqual(response.status, 403)
        llm.acomplete.assert_not_called()

    async def test_real_owned_task_to_codex_wire(self, selection):
        import httpx
        from openai import OpenAI
        from agent import auxiliary_client as aux
        from agent.plugin_llm import make_plugin_llm_for_test, _TrustPolicy
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest

        manager = PluginManager()
        ctx = PluginContext(PluginManifest(name="model-proxy"), manager)
        register(ctx)
        entries = list(manager._aux_tasks.values())
        self.assertEqual(entries[0]["defaults"]["reasoning_effort"], "high")
        wires = []

        def transport(request):
            wires.append(json.loads(request.content))
            final = {
                "id": "resp_test", "object": "response", "created_at": 0,
                "model": "gpt-6.1-sol", "status": "completed",
                "output": [{"id": "msg_test", "type": "message", "role": "assistant",
                            "status": "completed", "content": [
                                {"type": "output_text", "text": "answer", "annotations": []}]}],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
            event = {"type": "response.completed", "response": final}
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                                  text="data: " + json.dumps(event) + "\n\n")

        sdk = OpenAI(api_key="synthetic", base_url="https://chatgpt.com/backend-api/codex",
                     http_client=httpx.Client(transport=httpx.MockTransport(transport)))
        adapter = aux._CodexCompletionsAdapter(sdk, "gpt-6.1-sol")
        client = SimpleNamespace(base_url=sdk.base_url, chat=SimpleNamespace(
            completions=aux._AsyncCompletionsAdapter(adapter)))
        llm = make_plugin_llm_for_test(plugin_id="model-proxy", policy=_TrustPolicy(
            plugin_id="model-proxy", allow_model_override=True, allow_provider_override=False))
        try:
            with patch("hermes_cli.config.load_config_readonly", return_value={}), \
                    patch("hermes_cli.plugins.get_plugin_auxiliary_tasks", return_value=entries), \
                    patch.object(aux, "_resolve_call_client", return_value=(
                        client, "gpt-6.1-sol", "openai-codex", "openai-codex")) as resolver:
                async with listening(model_proxy_app(
                        llm, high_config_check=verify_high_config)) as url, \
                        ClientSession() as session:
                    for explicit in (False, True):
                        body = {"model": "gpt-6.1-sol", "messages": [
                            {"role": "user", "content": "hello"}]}
                        if explicit:
                            body["reasoning_effort"] = "high"
                        async with session.post(url + "/v1/chat/completions", json=body) as response:
                            self.assertEqual(response.status, 200, await response.text())
                for call in resolver.call_args_list:
                    self.assertIsNone(call.kwargs["provider"])
                    self.assertEqual(call.kwargs["model"], "gpt-6.1-sol")
                self.assertNotIn("reasoning", wires[0])
                self.assertEqual(wires[1]["reasoning"]["effort"], "high")
                self.assertEqual(wires[1]["model"], "gpt-6.1-sol")
        finally:
            sdk.close()
            manager.unload()
