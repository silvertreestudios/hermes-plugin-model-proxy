"""Strict text-only Chat Completions composition over the installed native proxy."""
import asyncio
from contextlib import asynccontextmanager
import json
import time
import uuid

from aiohttp import ClientSession, ClientTimeout, web
from .server import listening
from agent.codex_headers import codex_cloudflare_headers
from hermes_cli.proxy.server import create_app

MAX_BYTES = 1_000_000


def error_body(message, code="upstream_error"):
    return {"error": {"message": message, "type": code, "code": code}}


def translate(body):
    if not isinstance(body, dict):
        raise ValueError("Request must be an object")
    if set(body) - {"model", "messages", "stream", "n"}:
        raise ValueError("Unsupported request fields; supported: model, messages, stream, n=1")
    if not isinstance(body.get("model"), str) or not body["model"].strip():
        raise ValueError("An explicit nonempty model is required")
    if "stream" in body and type(body["stream"]) is not bool:
        raise ValueError("stream must be boolean")
    if "n" in body and (type(body["n"]) is not int or body["n"] != 1):
        raise ValueError("Only n=1 is supported")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a nonempty array")
    instructions, inputs = [], []
    for msg in messages:
        if (not isinstance(msg, dict) or set(msg) != {"role", "content"}
                or msg.get("role") not in ("system", "developer", "user", "assistant")
                or not isinstance(msg.get("content"), str)):
            raise ValueError("Messages require only a supported role and string content")
        role, text = msg["role"], msg["content"]
        if role in ("system", "developer"):
            if inputs:
                raise ValueError("system/developer messages must precede conversation messages")
            instructions.append(text)
        else:
            if inputs and inputs[-1]["role"] == role:
                raise ValueError("Conversation roles must alternate")
            inputs.append({"role": role, "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": text}]})
    if not inputs:
        raise ValueError("At least one conversation message is required")
    return {"model": body["model"], "instructions": "\n\n".join(instructions),
            "input": inputs, "tools": [], "store": False, "stream": True}


async def events(content):
    """Bounded SSE parser; only complete, UTF-8 data frames are accepted."""
    lines, size = [], 0
    async for raw in content:
        size += len(raw)
        if size > MAX_BYTES:
            raise ValueError("Upstream response exceeded limit")
        if not raw.endswith(b"\n"):
            raise ValueError("Truncated SSE line")
        line = raw.decode("utf-8").removesuffix("\n").removesuffix("\r")
        if not line:
            if lines:
                data = "\n".join(lines)
                lines = []
                if data != "[DONE]":
                    event = json.loads(data)
                    if not isinstance(event, dict):
                        raise ValueError("Invalid upstream event")
                    yield event
        elif line.startswith("data:"):
            lines.append(line[5:].removeprefix(" "))
        elif not line.startswith((":", "event:", "id:", "retry:")):
            raise ValueError("Invalid SSE field")
    if lines:
        raise ValueError("Truncated SSE frame")


def completed_text(response, model):
    if response.get("status") != "completed" or response.get("model") != model:
        raise ValueError("Invalid completed response")
    output = response.get("output")
    if not isinstance(output, list) or not output:
        raise ValueError("Missing upstream output")
    parts, messages = [], 0
    for item in output:
        if not isinstance(item, dict):
            raise ValueError("Invalid output item")
        if item.get("type", "message") == "reasoning":
            continue
        if item.get("type", "message") != "message":
            raise ValueError("Unsupported upstream output")
        if item.get("role", "assistant") != "assistant" or not isinstance(item.get("content"), list):
            raise ValueError("Invalid assistant output")
        messages += 1
        for part in item["content"]:
            if not isinstance(part, dict):
                raise ValueError("Invalid output part")
            if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                raise ValueError("Unsupported upstream output")
            parts.append(part["text"])
    if not messages:
        raise ValueError("Missing assistant output")
    return "".join(parts)


def usage(response):
    value = response.get("usage")
    if not isinstance(value, dict):
        raise ValueError("Missing upstream usage")
    keys = ("input_tokens", "output_tokens", "total_tokens")
    if any(type(value.get(k)) is not int or value[k] < 0 for k in keys):
        raise ValueError("Invalid upstream usage")
    return dict(zip(("prompt_tokens", "completion_tokens", "total_tokens"), (value[k] for k in keys)))


@asynccontextmanager
async def chat_app(adapter):
    # Fixed listener URL from setup, never from Host or any client header.
    async with listening(create_app(adapter)) as native_url, ClientSession(
            timeout=ClientTimeout(total=90, sock_connect=5, sock_read=30),
            # SSE iteration reads whole lines; allow any line within our total
            # response budget instead of aiohttp's smaller default line limit.
            read_bufsize=MAX_BYTES) as client:
        async def handle(request):
            try:
                if request.query_string:
                    raise ValueError("Query parameters are unsupported")
                raw = bytearray()
                async for piece in request.content.iter_chunked(65536):
                    raw.extend(piece)
                    if len(raw) > MAX_BYTES:
                        raise ValueError("Request exceeded limit")
                body = json.loads(raw)
                wire = translate(body)
            except (ValueError, TypeError, UnicodeError):
                return web.json_response(error_body("Invalid or unsupported chat request; use text messages and model only", "invalid_request_error"), status=400)
            stream = body.get("stream", False)
            result = None
            try:
                credential = await asyncio.to_thread(adapter.get_credential)
                headers = codex_cloudflare_headers(credential.bearer, base_url=credential.base_url)
                headers.update({"Accept": "text/event-stream", "OpenAI-Beta": "responses=experimental"})
            except Exception:
                return web.json_response(error_body("Current-profile credential resolver unavailable", "upstream_auth_failed"), status=401)
            meta = {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion.chunk" if stream else "chat.completion",
                    "created": int(time.time()), "model": wire["model"]}

            async def chunk(delta, finish=None):
                await result.write(("data: " + json.dumps({**meta, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n").encode())

            try:
                async with client.post(native_url + "/v1/responses", json=wire, headers=headers, allow_redirects=False) as upstream:
                    if upstream.status != 200:
                        return web.json_response(error_body("Native Responses upstream rejected request"), status=upstream.status if 400 <= upstream.status <= 599 else 502)
                    # The live Codex transport can label SSE as octet-stream.
                    # Still require valid framed events and a completed terminal below.
                    media_type = upstream.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if media_type not in {"text/event-stream", "application/octet-stream"}:
                        raise ValueError("Expected SSE")
                    if stream:
                        result = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
                        await result.prepare(request)
                        await chunk({"role": "assistant"})
                    terminal, text = None, ""
                    async for event in events(upstream.content):
                        kind = event.get("type")
                        if terminal is not None:
                            raise ValueError("Events after terminal")
                        if kind in ("error", "response.failed", "response.incomplete"):
                            raise ValueError("Upstream failed")
                        if kind == "response.output_text.delta":
                            delta = event.get("delta")
                            if not isinstance(delta, str):
                                raise ValueError("Invalid delta")
                            text += delta
                            if stream:
                                await chunk({"content": delta})
                        elif kind == "response.completed":
                            terminal = event.get("response")
                            if not isinstance(terminal, dict):
                                raise ValueError("Invalid terminal")
                    if terminal is None:
                        raise ValueError("Missing completed terminal")
                    final_text = completed_text(terminal, wire["model"])
                    token_usage = usage(terminal)
                    if text and text != final_text:
                        raise ValueError("Delta and terminal mismatch")
                    if stream:
                        if not text and final_text:
                            await chunk({"content": final_text})
                        await chunk({}, "stop")
                        await result.write(b"data: [DONE]\n\n")
                        await result.write_eof()
                        return result
                    return web.json_response({**meta, "choices": [{"index": 0, "message": {"role": "assistant", "content": final_text}, "finish_reason": "stop"}], "usage": token_usage})
            except asyncio.CancelledError:
                raise
            except Exception:
                if result is None:
                    return web.json_response(error_body("Responses transport or completion validation failed"), status=502)
                try:
                    await result.write(("data: " + json.dumps(error_body("Responses transport or completion validation failed")) + "\n\n").encode())
                    await result.write_eof()
                except ConnectionResetError:
                    pass
                return result

        @web.middleware
        async def chat_route(request, handler):
            if request.path == "/v1/chat/completions":
                if request.method != "POST":
                    return web.json_response(error_body("POST required", "invalid_request_error"), status=405)
                return await handle(request)
            return await handler(request)

        app = create_app(adapter)
        app.middlewares.append(chat_route)
        yield app
