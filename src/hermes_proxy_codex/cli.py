"""Start the local proxy without changing Hermes configuration."""
import argparse
import asyncio
import logging
import signal


def parser():
    result = argparse.ArgumentParser(description="Local trusted-use Hermes Codex proxy")
    result.add_argument("--port", type=int, default=8646, help="Loopback port (default: 8646)")
    return result


async def serve(port):
    from .adapter import CodexAdapter
    from .chat import chat_app
    from .server import listening

    adapter = CodexAdapter()
    await asyncio.to_thread(adapter.get_credential)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered = []
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
            registered.append(sig)
        async with chat_app(adapter) as app, listening(app, port=port) as url:
            print(url + "/v1", flush=True)
            await stop.wait()
    finally:
        for sig in registered:
            loop.remove_signal_handler(sig)


def main():
    args = parser().parse_args()
    if not 0 <= args.port <= 65535:
        parser().error("port must be between 0 and 65535")
    # Dependency exception records can include upstream values. Suppress these
    # loggers in the console process, without disabling the caller's logging API.
    for name in ("hermes_cli.proxy.server", "aiohttp.server", "aiohttp.access"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
    try:
        asyncio.run(serve(args.port))
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("Proxy unavailable: check the selected Hermes home, token expiry, dependencies and loopback port")
        return 1
    return 0
