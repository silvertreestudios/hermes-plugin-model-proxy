"""Owned task routing through the real host planner, with synthetic transport."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession
from hermes_constants import VALID_REASONING_EFFORTS, parse_reasoning_effort
from hermes_model_proxy.model_proxy import (
    REASONING_TASKS, model_proxy_app, parse_request, register, verify_reasoning_config,
)
from hermes_model_proxy.server import listening


@patch("hermes_cli.runtime_provider.resolve_requested_provider", return_value="openai-codex")
class Reasoning(unittest.IsolatedAsyncioTestCase):
    def test_validation_and_config(self, selection):
        base = {"messages": [{"role": "user", "content": "hello"}]}
        self.assertNotIn("task", parse_request(base))
        for value in (None, True, False, 1, [], {}, "bogus", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_request({**base, "reasoning_effort": value})
        for field in ("task", "extra_body", "reasoning"):
            with self.assertRaises(ValueError):
                parse_request({**base, field: {}})
        for effort in ("none", *VALID_REASONING_EFFORTS):
            for value in (effort, " " + effort.upper() + " "):
                self.assertEqual(parse_request({**base, "reasoning_effort": value})["task"],
                                 REASONING_TASKS[effort])
            for task in ({}, {"reasoning_effort": effort}):
                with patch("hermes_cli.config.load_config_readonly", return_value={
                        "auxiliary": {REASONING_TASKS[effort]: task}}):
                    verify_reasoning_config(effort)
            for task in ({"reasoning_effort": "bad"}, {"reasoning_effort": None},
                         {"extra_body": {"reasoning": {"effort": "low"}}},
                         {"provider": "other"}, {"model": "other"},
                         {"base_url": "custom"}, {"api_key": "synthetic"},
                         {"api_mode": "custom"}, {"fallback_chain": ["other"]}):
                with patch("hermes_cli.config.load_config_readonly", return_value={
                        "auxiliary": {REASONING_TASKS[effort]: task}}), self.assertRaises(PermissionError):
                    verify_reasoning_config(effort)
        for alias in ("false", "disabled", " NONE "):
            self.assertEqual(parse_reasoning_effort(alias), {"enabled": False})
            self.assertEqual(parse_request({**base, "reasoning_effort": alias})["task"],
                             REASONING_TASKS["none"])

    def test_selected_route_and_normalized_task_config(self, selection):
        for effort in ("none", *VALID_REASONING_EFFORTS):
            task = {"reasoning_effort": parse_reasoning_effort(effort),
                    "model": "chosen", "provider": "synthetic"}
            selection.return_value = "synthetic"
            with patch("hermes_cli.config.load_config_readonly", return_value={
                    "auxiliary": {REASONING_TASKS[effort]: task}}):
                self.assertEqual(verify_reasoning_config(
                    effort, model="chosen", provider="synthetic"), ("chosen", "synthetic"))
                selection.assert_called_with("synthetic")
        selection.return_value = "auto"
        with patch("hermes_cli.config.load_config_readonly", return_value={}):
            self.assertEqual(verify_reasoning_config("none"), (None, None))

    def test_all_effort_conflicts(self, selection):
        for effort in ("none", *VALID_REASONING_EFFORTS):
            other = "high" if effort != "high" else "none"
            with patch("hermes_cli.config.load_config_readonly", return_value={
                    "auxiliary": {REASONING_TASKS[effort]: {"reasoning_effort": other}}}), \
                    self.assertRaises(PermissionError):
                verify_reasoning_config(effort)

    async def test_http_fail_closed(self, selection):
        llm = SimpleNamespace(acomplete=AsyncMock())
        base = {"messages": [{"role": "user", "content": "hello"}]}
        async with listening(model_proxy_app(llm)) as url, ClientSession() as session:
            for value in (None, True, False, 1, [], {}, "bad", "", *VALID_REASONING_EFFORTS, "none"):
                async with session.post(url + "/v1/chat/completions", json={
                        **base, "reasoning_effort": value}) as response:
                    self.assertEqual(response.status, 403 if value in (*VALID_REASONING_EFFORTS, "none")
                                     else 400)
        llm.acomplete.assert_not_called()

    async def test_route_guards_and_valid_controls(self, selection):
        for active_provider in ("openai-codex", "synthetic"):
            selection.return_value = active_provider
            for requested_model in (None, "chosen"):
                expected_model = requested_model or "active-model"
                for mutated in (None, "provider", "model"):
                    reply = SimpleNamespace(provider=active_provider, model=expected_model,
                                            text="answer", usage=SimpleNamespace(
                                                input_tokens=1, output_tokens=1, total_tokens=2))
                    if mutated:
                        setattr(reply, mutated, "unexpected")
                    llm = SimpleNamespace(acomplete=AsyncMock(return_value=reply))
                    body = {"reasoning_effort": "high", "messages": [
                        {"role": "user", "content": "hello"}]}
                    if requested_model:
                        body["model"] = requested_model
                    with patch("hermes_cli.config.load_config_readonly", return_value={
                            "model": {"default": "active-model"}}):
                        async with listening(model_proxy_app(llm, reasoning_config_check=
                                                             verify_reasoning_config)) as url, \
                                ClientSession() as session:
                            async with session.post(url + "/v1/chat/completions", json=body) as response:
                                self.assertEqual(response.status, 502 if mutated else 200)
                    self.assertNotIn("provider", llm.acomplete.call_args.kwargs)

    async def test_config_denied_before_dispatch(self, selection):
        llm = SimpleNamespace(acomplete=AsyncMock())
        with patch("hermes_cli.config.load_config_readonly", return_value={
                "auxiliary": {REASONING_TASKS["high"]: {"reasoning_effort": "low"}}}):
            async with listening(model_proxy_app(llm, reasoning_config_check=
                                                 verify_reasoning_config)) as url, ClientSession() as session:
                async with session.post(url + "/v1/chat/completions", json={
                        "reasoning_effort": "high", "messages": [
                            {"role": "user", "content": "hello"}]}) as response:
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
        self.assertEqual({e["key"] for e in entries}, set(REASONING_TASKS.values()))
        self.assertEqual(len(entries), len(VALID_REASONING_EFFORTS) + 1)
        self.assertTrue(all(e["plugin"] == "model-proxy" for e in entries))
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
                for task in ("model_proxy_reflection_arbitrary", "vision"):
                    with self.assertRaises(PermissionError):
                        await llm.acomplete(task=task, messages=[{"role": "user", "content": "hello"}])
                async with listening(model_proxy_app(
                        llm, reasoning_config_check=verify_reasoning_config)) as url, \
                        ClientSession() as session:
                    for effort in (None, "none", *VALID_REASONING_EFFORTS, " DISABLED ", "false"):
                        body = {"model": "gpt-6.1-sol", "messages": [
                            {"role": "user", "content": "hello"}]}
                        if effort is not None:
                            body["reasoning_effort"] = effort
                        async with session.post(url + "/v1/chat/completions", json=body) as response:
                            self.assertEqual(response.status, 200, await response.text())
                for call in resolver.call_args_list:
                    self.assertIsNone(call.kwargs["provider"])
                    self.assertEqual(call.kwargs["model"], "gpt-6.1-sol")
                self.assertNotIn("reasoning", wires[0])
                from agent.reasoning_effort import clamp_effort, codex_supported_efforts
                for wire, effort in zip(wires[1:], ("none", *VALID_REASONING_EFFORTS, "none", "none"), strict=True):
                    self.assertEqual(wire["reasoning"]["effort"],
                                     clamp_effort(effort, codex_supported_efforts("gpt-6.1-sol")))
                    self.assertEqual(wire["model"], "gpt-6.1-sol")
                self.assertEqual(wires[5]["reasoning"]["effort"], "high")
                active_llm = make_plugin_llm_for_test(plugin_id="model-proxy", policy=_TrustPolicy(
                    plugin_id="model-proxy"))
                async with listening(model_proxy_app(
                        active_llm, reasoning_config_check=verify_reasoning_config)) as url, \
                        ClientSession() as session:
                    async with session.post(url + "/v1/chat/completions", json={
                            "reasoning_effort": "high", "messages": [
                                {"role": "user", "content": "hello"}]}) as response:
                        self.assertEqual(response.status, 200, await response.text())
                self.assertIsNone(resolver.call_args.kwargs["provider"])
                self.assertIsNone(resolver.call_args.kwargs["model"])
                self.assertEqual(wires[-1]["reasoning"]["effort"], "high")
        finally:
            sdk.close()
            manager.unload()
