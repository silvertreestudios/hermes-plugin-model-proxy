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
                         {"key_env": "SYNTHETIC_KEY"}, {"api_key_env": "SYNTHETIC_KEY"},
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
                self.assertIsNone(verify_reasoning_config(
                    effort, model="chosen", provider="synthetic"))
                selection.assert_called_with("synthetic")
        selection.return_value = "auto"
        with patch("hermes_cli.config.load_config_readonly", return_value={}):
            self.assertIsNone(verify_reasoning_config("none"))

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

    async def test_host_normalized_routes_and_permission_gates(self, selection):
        from agent.plugin_llm import make_plugin_llm_for_test, _TrustPolicy
        from hermes_cli.model_normalize import normalize_model_for_provider
        from hermes_cli.runtime_provider_custom import expand_direct_api_alias

        prefixed = "openai/gpt-6.1-sol"
        canonical = normalize_model_for_provider(prefixed, "openai-codex")
        self.assertEqual(canonical, "gpt-6.1-sol")
        with patch("hermes_cli.runtime_provider._get_named_custom_provider", return_value=None), \
                patch("hermes_cli.runtime_provider_custom.get_secret_str", return_value=""):
            self.assertEqual(expand_direct_api_alias("openai", None),
                             ("custom", "https://api.openai.com/v1"))
        entries = [{"key": task, "plugin": "model-proxy"} for task in REASONING_TASKS.values()]
        for requested_provider, actual_provider in ((None, "openai-codex"), ("openai", "custom")):
            selection.return_value = requested_provider or "openai-codex"
            for effort in ("none", *VALID_REASONING_EFFORTS):
                for denied in (None, "model", "provider", "model_allowlist", "provider_allowlist"):
                    if denied and denied.startswith("provider") and requested_provider is None:
                        continue
                    llm = make_plugin_llm_for_test(plugin_id="model-proxy", policy=_TrustPolicy(
                        plugin_id="model-proxy", allow_model_override=denied != "model",
                        allow_provider_override=denied != "provider",
                        allowed_models=frozenset({"other"}) if denied == "model_allowlist" else None,
                        allowed_providers=frozenset({"other"}) if denied == "provider_allowlist" else None))

                    async def host(**kwargs):
                        kwargs["route_info"].update(provider=actual_provider, model="route-model")
                        return SimpleNamespace(model=canonical, choices=[SimpleNamespace(
                            message=SimpleNamespace(content="answer"))])

                    with patch("hermes_cli.config.load_config_readonly", return_value={}), \
                            patch("hermes_cli.plugins.get_plugin_auxiliary_tasks", return_value=entries), \
                            patch("agent.auxiliary_client.async_call_llm", side_effect=host) as caller:
                        async with listening(model_proxy_app(llm, provider=requested_provider,
                                reasoning_config_check=verify_reasoning_config)) as url, ClientSession() as session:
                            async with session.post(url + "/v1/chat/completions", json={
                                    "reasoning_effort": effort, "model": prefixed,
                                    "messages": [{"role": "user", "content": "hello"}]}) as response:
                                self.assertEqual(response.status, 403 if denied else 200)
                                body = await response.json()
                        if denied:
                            caller.assert_not_called()
                        else:
                            self.assertEqual((body["model"], body["provider"]), (canonical, actual_provider))
                            self.assertEqual(caller.call_args.kwargs["model"], prefixed)
                            self.assertEqual(caller.call_args.kwargs["provider"], requested_provider)

    async def test_credential_aliases_denied_before_dispatch(self, selection):
        for effort in ("none", *VALID_REASONING_EFFORTS):
            for field in ("api_key", "key_env", "api_key_env"):
                llm = SimpleNamespace(acomplete=AsyncMock())
                with patch("hermes_cli.config.load_config_readonly", return_value={
                        "auxiliary": {REASONING_TASKS[effort]: {field: "SYNTHETIC_KEY"}}}), \
                        patch("agent.auxiliary_client._scoped_key_env") as credential_reader:
                    async with listening(model_proxy_app(llm, reasoning_config_check=
                            verify_reasoning_config)) as url, ClientSession() as session:
                        async with session.post(url + "/v1/chat/completions", json={
                                "reasoning_effort": effort,
                                "messages": [{"role": "user", "content": "hello"}]}) as response:
                            self.assertEqual(response.status, 403)
                credential_reader.assert_not_called()
                llm.acomplete.assert_not_called()

    async def test_config_denied_before_dispatch(self, selection):
        for effort in ("none", *VALID_REASONING_EFFORTS):
            for task in ({"reasoning_effort": "low" if effort != "low" else "none"},
                         {"model": "redirect"}, {"provider": "redirect"}):
                llm = SimpleNamespace(acomplete=AsyncMock())
                with patch("hermes_cli.config.load_config_readonly", return_value={
                        "auxiliary": {REASONING_TASKS[effort]: task}}):
                    async with listening(model_proxy_app(llm, reasoning_config_check=
                            verify_reasoning_config)) as url, ClientSession() as session:
                        async with session.post(url + "/v1/chat/completions", json={
                                "reasoning_effort": effort, "messages": [
                                    {"role": "user", "content": "hello"}]}) as response:
                            self.assertEqual(response.status, 403)
                llm.acomplete.assert_not_called()

    async def test_reasoning_envelope_identifier_checks(self, selection):
        for field in ("model", "provider"):
            reply = SimpleNamespace(model="canonical", provider="synthetic", text="answer",
                                    usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2))
            setattr(reply, field, "invalid\nidentifier")
            llm = SimpleNamespace(acomplete=AsyncMock(return_value=reply))
            with patch("hermes_cli.config.load_config_readonly", return_value={}):
                async with listening(model_proxy_app(llm, reasoning_config_check=
                        verify_reasoning_config)) as url, ClientSession() as session:
                    async with session.post(url + "/v1/chat/completions", json={
                            "reasoning_effort": "high", "messages": [
                                {"role": "user", "content": "hello"}]}) as response:
                        self.assertEqual(response.status, 502)
                        self.assertNotIn("identifier", await response.text())

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
