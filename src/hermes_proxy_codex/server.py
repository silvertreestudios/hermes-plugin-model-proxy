"""Loopback listener shared by the public and private proxy applications."""
from contextlib import asynccontextmanager
from aiohttp import web


@web.middleware
async def local_process_only(request, handler):
    """Reject browser and DNS-rebinding requests before any route can use credentials.

    Compare Host with the actual bound socket, not client-controlled forwarded
    headers. This deliberately supports only native clients using the printed URL;
    it does not authenticate other processes on the same machine.
    """
    socket = request.transport.get_extra_info("sockname") if request.transport else None
    hosts = request.headers.getall("Host", [])
    if (
        not socket
        or hosts != [f"127.0.0.1:{socket[1]}"]
        or "Origin" in request.headers
        or any(name.lower().startswith("sec-fetch-") for name in request.headers)
    ):
        return web.json_response({"error": {"message": "Only local native clients are supported"}}, status=403)
    return await handler(request)


@asynccontextmanager
async def listening(app, *, host="127.0.0.1", port=0):
    if host != "127.0.0.1":
        raise ValueError("Only 127.0.0.1 is supported")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Invalid port")
    # Enforce the same boundary on the public bridge and private native hop.
    app.middlewares.insert(0, local_process_only)
    runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host, port)
        await site.start()
        actual_port = site._server.sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{actual_port}"
    finally:
        await runner.cleanup()
