"""Text-only HTTP inference through the public, host-owned PluginContext LLM API."""
import argparse
import asyncio
import json
import logging
import math
import re
import signal
import time
import uuid

from aiohttp import web
from hermes_constants import VALID_REASONING_EFFORTS, parse_reasoning_effort

from .server import listening

REASONING_TASKS = {effort: f"model_proxy_reflection_{effort}"
                   for effort in ("none", *VALID_REASONING_EFFORTS)}

BODY_LIMIT = 1_000_000
_IDENTIFIER = re.compile(r"[A-Za-z0-9/][A-Za-z0-9._:/@+-]{0,199}\Z")


def reasoning_effort(value):
    """Normalize the HTTP string using the host's public reasoning contract."""
    parsed = parse_reasoning_effort(value) if type(value) is str else None
    if parsed is None:
        raise ValueError("Invalid reasoning effort")
    return parsed["effort"] if parsed.get("enabled") else "none"


def identifier(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid model or provider identifier")
    return value


def number(value, low, high, *, integer=False):
    if (type(value) not in ((int,) if integer else (int, float))
            or not low <= value <= high or not math.isfinite(value)):
        raise ValueError("Numeric option outside supported bounds")
    return value


def parse_request(body, *, model=None, timeout=90):
    if not isinstance(body, dict) or set(body) - {
        "messages", "model", "stream", "n", "max_tokens", "max_completion_tokens",
        "temperature", "timeout", "response_format", "reasoning_effort"
    }:
        raise ValueError("Unsupported request fields")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > 1024:
        raise ValueError("Expected 1–1024 text messages")
    for message in messages:
        if (not isinstance(message, dict) or set(message) != {"role", "content"}
                or message["role"] not in ("system", "developer", "user", "assistant")
                or not isinstance(message["content"], str)):
            raise ValueError("Only role and text content messages are supported")
    if "stream" in body and body["stream"] is not False:
        raise ValueError("Only stream=false is supported")
    if "n" in body and (type(body["n"]) is not int or body["n"] != 1):
        raise ValueError("Only n=1 is supported")
    selected = identifier(body["model"]) if "model" in body else model
    if model is not None and selected != model:
        raise ValueError("Request model differs from configured model")
    kwargs = {"messages": messages, "timeout": timeout, "max_tokens": 1024}
    if selected is not None:
        kwargs["model"] = selected
    if "max_tokens" in body and "max_completion_tokens" in body:
        raise ValueError("Token aliases cannot be combined")
    for token_field in ("max_tokens", "max_completion_tokens"):
        if token_field in body:
            kwargs["max_tokens"] = number(body[token_field], 1, 32768, integer=True)
    if "temperature" in body:
        kwargs["temperature"] = number(body["temperature"], 0, 2)
    if "timeout" in body:
        kwargs["timeout"] = number(body["timeout"], 0.1, timeout)
    if "response_format" in body:
        if body["response_format"] != {"type": "json_object"}:
            raise ValueError("Only response_format json_object is supported")
        if ([m["role"] for m in messages] not in (["user"], ["system", "user"])
                or not messages[-1]["content"].strip()):
            raise ValueError("JSON mode requires optional system then one nonempty user message")
        del kwargs["messages"]
        kwargs.update(instructions="Respond to the user input with a JSON object.",
                      input=[{"type": "text", "text": messages[-1]["content"]}],
                      json_mode=True)
        if len(messages) == 2:
            kwargs["system_prompt"] = messages[0]["content"]
    if "reasoning_effort" in body:
        kwargs["task"] = REASONING_TASKS[reasoning_effort(body["reasoning_effort"])]
    return kwargs


def error(status, message, kind):
    return web.json_response({"error": {"message": message, "type": kind, "code": kind}}, status=status)


def envelope(result, *, structured=False):
    # Attribution is host supplied, but never echo arbitrary metadata or exceptions.
    model = identifier(result.model)
    provider = identifier(result.provider)
    if not isinstance(result.text, str):
        raise ValueError("Invalid host result")
    text = result.text
    if structured:
        if result.content_type != "json" or not isinstance(result.parsed, dict):
            raise ValueError("Invalid structured host result")
        # Use the facade's validated value, including when it removed code fences.
        text = json.dumps(result.parsed, ensure_ascii=False, allow_nan=False)
    usage = result.usage
    counts = [usage.input_tokens, usage.output_tokens, usage.total_tokens]
    if any(type(n) is not int or n < 0 for n in counts):
        raise ValueError("Invalid host usage")
    return {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion",
            "created": int(time.time()), "model": model, "provider": provider,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": dict(zip(("prompt_tokens", "completion_tokens", "total_tokens"), counts))}


def verify_reasoning_config(effort, *, model=None, provider=None):
    """Reject task controls that change the requested effort or selected route.

    Public selection/config APIs do not resolve credentials or clients. An auto
    provider or unspecified active model cannot be pinned before host resolution.
    """
    from hermes_cli.config import load_config_readonly
    from hermes_cli.runtime_provider import resolve_requested_provider
    config = load_config_readonly()
    selected_provider = resolve_requested_provider(provider)
    model_config = config.get("model", {})
    selected_model = model or (model_config.get("default") if isinstance(model_config, dict)
                               else model_config)
    auxiliary = config.get("auxiliary", {})
    task = auxiliary.get(REASONING_TASKS[effort], {}) if isinstance(auxiliary, dict) else None
    if not isinstance(task, dict):
        raise PermissionError("Invalid reflection task configuration")
    effective = {"reasoning_effort": effort, **task}
    if (parse_reasoning_effort(effective.get("reasoning_effort")) != parse_reasoning_effort(effort)
            or effective.get("extra_body") not in (None, {})
            or any(effective.get(k) for k in ("base_url", "api_key", "api_mode", "fallback_chain"))
            or effective.get("provider", "auto") not in ("auto", selected_provider)
            or effective.get("model", "") not in ("", selected_model)):
        raise PermissionError("Reflection task configuration conflicts with requested reasoning")
    return selected_model, selected_provider if selected_provider != "auto" else None


def model_proxy_app(llm, *, model=None, provider=None, timeout=90, reasoning_config_check=None):
    number(timeout, 0.1, 300)
    if model is not None:
        identifier(model)
    if provider is not None:
        identifier(provider)

    async def complete(request):
        if request.query or any(h in request.headers for h in (
            "Authorization", "Proxy-Authorization", "X-API-Key", "API-Key", "Cookie"
        )):
            return error(400, "Query parameters and client credentials are unsupported", "invalid_request_error")
        if request.content_type != "application/json":
            return error(415, "Expected application/json", "invalid_request_error")
        try:
            raw = await request.read()
            # Reject duplicate keys as well as nonstandard JSON numeric constants.
            def pairs(items):
                value = {}
                for key, item in items:
                    if key in value:
                        raise ValueError("Duplicate JSON field")
                    value[key] = item
                return value
            def constant(_):
                raise ValueError("Nonstandard JSON number")
            body = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
            kwargs = parse_request(body, model=model, timeout=timeout)
        except web.HTTPRequestEntityTooLarge:
            return error(413, "Request body too large", "invalid_request_error")
        except (ValueError, UnicodeError, RecursionError):
            return error(400, "Invalid or unsupported text completion request", "invalid_request_error")
        if provider is not None:
            kwargs["provider"] = provider
        try:
            if "task" in kwargs:
                if reasoning_config_check is None:
                    raise PermissionError("Reasoning configuration check unavailable")
                effort = next(k for k, v in REASONING_TASKS.items() if v == kwargs["task"])
                expected_model, expected_provider = reasoning_config_check(
                    effort, model=kwargs.get("model"), provider=provider)
            structured = "json_mode" in kwargs
            async with asyncio.timeout(kwargs["timeout"]):
                method = llm.acomplete_structured if structured else llm.acomplete
                result = await method(**kwargs, purpose="model-proxy.chat")
            if "task" in kwargs and (
                    (expected_model and result.model != expected_model)
                    or (expected_provider and result.provider != expected_provider)):
                raise ValueError("Unexpected reflection route attribution")
            return web.json_response(envelope(result, structured=structured))
        except PermissionError:
            return error(403, "Host denied inference or an override; review plugin permissions", "permission_error")
        except TimeoutError:
            return error(504, "Host inference timed out", "timeout_error")
        except Exception:
            return error(502, "Host inference unavailable", "provider_error")

    async def health(request):
        return web.json_response({"status": "ok"})

    app = web.Application(client_max_size=BODY_LIMIT)
    app.router.add_post("/v1/chat/completions", complete)
    app.router.add_get("/health", health)
    return app


def _port(value):
    try:
        return number(int(value), 0, 65535, integer=True)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be 0–65535") from exc


def _timeout(value):
    try:
        return number(float(value), 0.1, 300)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be 0.1–300 seconds") from exc


def setup_cli(parser):
    parser.add_argument("--port", type=_port, default=8647)
    parser.add_argument("--timeout", type=_timeout, default=90)
    parser.add_argument("--model", type=identifier, help="Host-gated model override")
    parser.add_argument("--provider", type=identifier, help="Host-gated provider override")


async def serve(llm, args, *, reasoning_config_check=None):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered = []
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
            registered.append(sig)
        app = model_proxy_app(llm, model=args.model, provider=args.provider, timeout=args.timeout,
                              reasoning_config_check=reasoning_config_check)
        async with listening(app, port=args.port) as url:
            print(url + "/v1", flush=True)
            await stop.wait()
    finally:
        for sig in registered:
            loop.remove_signal_handler(sig)


def register(ctx):
    for effort, task in REASONING_TASKS.items():
        ctx.register_auxiliary_task(
            task, display_name=f"Model proxy reflection {effort}",
            description=f"Host-managed {effort} reasoning for the model proxy",
            defaults={"reasoning_effort": effort})
    def run(args):
        for name in ("aiohttp.server", "aiohttp.access"):
            logger = logging.getLogger(name)
            logger.handlers = [logging.NullHandler()]
            logger.propagate = False
        try:
            asyncio.run(serve(ctx.llm, args, reasoning_config_check=verify_reasoning_config))
        except KeyboardInterrupt:
            return 0
        except Exception:
            print("Model proxy unavailable; check Hermes compatibility and loopback port")
            return 1
        return 0
    ctx.register_cli_command("model-proxy", "Serve local text inference through Hermes",
                             setup_cli, run)
