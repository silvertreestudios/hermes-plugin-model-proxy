"""Structured judge contract, using synthetic host calls only."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from aiohttp import ClientSession
from hermes_model_proxy.model_proxy import model_proxy_app, parse_request
from hermes_model_proxy.server import listening


def payload(**fields):
    return {"messages": [{"role": "user", "content": "prompt-secret"}],
            "response_format": {"type": "json_object"}, **fields}


def reply(**fields):
    return SimpleNamespace(text='```json\n{"score": 1}\n```', parsed={"score": 1},
                           content_type="json", model="actual", provider="synthetic",
                           usage=SimpleNamespace(input_tokens=7, output_tokens=3, total_tokens=10),
                           **fields)


class Validation(unittest.TestCase):
    def test_token_aliases(self):
        for field in ("max_tokens", "max_completion_tokens"):
            for value in (1, 2048, 32768):
                self.assertEqual(parse_request(payload(**{field: value}))["max_tokens"], value)
            for value in (True, False, 0, -1, 32769, 1.5, "2048", None, float("inf")):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    parse_request(payload(**{field: value}))
        with self.assertRaises(ValueError):
            parse_request(payload(max_tokens=1, max_completion_tokens=1))
        plain = payload(max_completion_tokens=2048)
        del plain["response_format"]
        self.assertEqual(parse_request(plain)["max_tokens"], 2048)

    def test_formats_and_layouts(self):
        for fmt in (None, [], "json_object", {}, {"type": "text"},
                    {"type": "json_schema", "json_schema": {"name": "x", "schema": {}}},
                    {"type": "json_schema", "json_schema": []},
                    {"type": "json_object", "extra": True}):
            with self.subTest(fmt=fmt), self.assertRaises(ValueError):
                parse_request(payload(response_format=fmt))
        for roles in (["system"], ["developer", "user"], ["user", "assistant", "user"],
                      ["user", "user"], ["system", "system", "user"]):
            with self.subTest(roles=roles), self.assertRaises(ValueError):
                parse_request(payload(messages=[{"role": r, "content": "x"} for r in roles]))
        with self.assertRaises(ValueError):
            parse_request(payload(messages=[{"role": "user", "content": "  "}]))

    def test_plain_messages_unchanged(self):
        messages = [{"role": r, "content": "  original  "} for r in
                    ("system", "developer", "user", "assistant", "user")]
        self.assertEqual(parse_request({"messages": messages}),
                         {"messages": messages, "timeout": 90, "max_tokens": 1024})


class HTTP(unittest.IsolatedAsyncioTestCase):
    async def request(self, llm, body, **options):
        async with listening(model_proxy_app(llm, **options)) as url, ClientSession() as client:
            async with client.post(url + "/v1/chat/completions", json=body) as response:
                return response.status, await response.json()

    async def test_dispatch(self):
        llm = SimpleNamespace(acomplete=AsyncMock(), acomplete_structured=AsyncMock(return_value=reply()))
        messages = [{"role": "system", "content": "system prompt"},
                    {"role": "user", "content": "  prompt-secret  "}]
        status, body = await self.request(llm, payload(messages=messages, model="chosen",
                                                      max_completion_tokens=2048, temperature=0),
                                          provider="synthetic")
        self.assertEqual(status, 200)
        llm.acomplete.assert_not_called()
        llm.acomplete_structured.assert_awaited_once_with(
            instructions="Respond to the user input with a JSON object.",
            input=[{"type": "text", "text": "  prompt-secret  "}], system_prompt="system prompt",
            json_mode=True, model="chosen", provider="synthetic", max_tokens=2048,
            temperature=0, timeout=90, purpose="model-proxy.chat")
        self.assertEqual(json.loads(body["choices"][0]["message"]["content"]), {"score": 1})
        self.assertEqual(body["usage"], {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
        self.assertEqual((body["model"], body["provider"]), ("actual", "synthetic"))

    async def test_rejection_before_dispatch(self):
        llm = SimpleNamespace(acomplete=AsyncMock(), acomplete_structured=AsyncMock())
        for fields in ({"response_format": {"type": "json_schema", "json_schema": {}}},
                       {"max_tokens": 1, "max_completion_tokens": 1},
                       {"provider": "client-provider"}):
            self.assertEqual((await self.request(llm, payload(**fields)))[0], 400)
        async with listening(model_proxy_app(llm)) as url, ClientSession() as client:
            for headers, expected in (({"Origin": "null"}, 403),
                                      ({"Authorization": "token-secret"}, 400)):
                async with client.post(url + "/v1/chat/completions", json=payload(),
                                       headers=headers) as response:
                    self.assertEqual(response.status, expected)
                    self.assertNotIn("secret", await response.text())
        llm.acomplete.assert_not_called()
        llm.acomplete_structured.assert_not_called()

    async def test_invalid_results_and_private_errors(self):
        for field, value in (("parsed", None), ("parsed", []), ("parsed", {"x": float("nan")}),
                             ("content_type", "text"), ("text", None), ("model", "secret\n")):
            result = reply()
            setattr(result, field, value)
            llm = SimpleNamespace(acomplete_structured=AsyncMock(return_value=result))
            status, body = await self.request(llm, payload())
            self.assertEqual(status, 502)
            self.assertNotIn("secret", json.dumps(body))
        for failure, expected in ((PermissionError("prompt-secret"), 403),
                                  (RuntimeError("token-secret"), 502), (TimeoutError(), 504)):
            llm = SimpleNamespace(acomplete_structured=AsyncMock(side_effect=failure))
            status, body = await self.request(llm, payload())
            self.assertEqual(status, expected)
            self.assertNotIn("secret", json.dumps(body))
        plain_only = SimpleNamespace(acomplete=AsyncMock())
        self.assertEqual((await self.request(plain_only, payload()))[0], 502)
        plain_only.acomplete.assert_not_called()

    async def test_real_facade(self):
        from agent.plugin_llm import make_plugin_llm_for_test, _TrustPolicy
        calls = []
        async def host(**kwargs):
            calls.append(kwargs)
            return "synthetic", "actual", SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"score":1}'))],
                usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3, total_tokens=10))
        probe = make_plugin_llm_for_test(plugin_id="model-proxy", policy=_TrustPolicy(plugin_id="model-proxy"))
        if not hasattr(probe, "acomplete_structured"):
            self.skipTest("Pinned older Hermes has no structured facade; mock contract remains covered")
        for allowed in (False, True):
            llm = make_plugin_llm_for_test(plugin_id="model-proxy", policy=_TrustPolicy(
                plugin_id="model-proxy", allow_model_override=allowed,
                allow_provider_override=allowed), async_caller=host)
            status, body = await self.request(llm, payload(model="chosen", max_completion_tokens=2048),
                                              provider="synthetic")
            self.assertEqual(status, 200 if allowed else 403)
            if not allowed:
                self.assertEqual(calls, [])
            else:
                self.assertEqual(calls[0]["extra_body"], {"response_format": {"type": "json_object"}})
                self.assertEqual(calls[0]["model_override"], "chosen")
                self.assertEqual(calls[0]["provider_override"], "synthetic")
                self.assertEqual(calls[0]["max_tokens"], 2048)
                self.assertEqual(calls[0]["messages"][-1]["content"][-1],
                                 {"type": "text", "text": "prompt-secret"})
                self.assertEqual(body["usage"]["total_tokens"], 10)
