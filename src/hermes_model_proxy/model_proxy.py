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

from .server import listening

BODY_LIMIT = 1_000_000
_IDENTIFIER = re.compile(r"[A-Za-z0-9/][A-Za-z0-9._:/@+-]{0,199}\Z")


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
        "messages", "model", "stream", "n", "max_tokens", "temperature", "timeout"
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
    if "max_tokens" in body:
        kwargs["max_tokens"] = number(body["max_tokens"], 1, 32768, integer=True)
    if "temperature" in body:
        kwargs["temperature"] = number(body["temperature"], 0, 2)
    if "timeout" in body:
        kwargs["timeout"] = number(body["timeout"], 0.1, timeout)
    return kwargs


def error(status, message, kind):
    return web.json_response({"error": {"message": message, "type": kind, "code": kind}}, status=status)


def envelope(result):
    # Attribution is host supplied, but never echo arbitrary metadata or exceptions.
    model = identifier(result.model)
    provider = identifier(result.provider)
    if not isinstance(result.text, str):
        raise ValueError("Invalid host result")
    usage = result.usage
    counts = [usage.input_tokens, usage.output_tokens, usage.total_tokens]
    if any(type(n) is not int or n < 0 for n in counts):
        raise ValueError("Invalid host usage")
    return {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion",
            "created": int(time.time()), "model": model, "provider": provider,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": result.text},
                         "finish_reason": "stop"}],
            "usage": dict(zip(("prompt_tokens", "completion_tokens", "total_tokens"), counts))}


def model_proxy_app(llm, *, model=None, provider=None, timeout=90):
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
            async with asyncio.timeout(kwargs["timeout"]):
                result = await llm.acomplete(**kwargs, purpose="model-proxy.chat")
            return web.json_response(envelope(result))
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


async def serve(llm, args):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered = []
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
            registered.append(sig)
        app = model_proxy_app(llm, model=args.model, provider=args.provider, timeout=args.timeout)
        async with listening(app, port=args.port) as url:
            print(url + "/v1", flush=True)
            await stop.wait()
    finally:
        for sig in registered:
            loop.remove_signal_handler(sig)


def register(ctx):
    def run(args):
        for name in ("aiohttp.server", "aiohttp.access"):
            logger = logging.getLogger(name)
            logger.handlers = [logging.NullHandler()]
            logger.propagate = False
        try:
            asyncio.run(serve(ctx.llm, args))
        except KeyboardInterrupt:
            return 0
        except Exception:
            print("Model proxy unavailable; check Hermes compatibility and loopback port")
            return 1
        return 0
    ctx.register_cli_command("model-proxy", "Serve local text inference through Hermes",
                             setup_cli, run)
