import asyncio
import unittest
from aiohttp import ClientSession, web
import argparse
from hermes_model_proxy.model_proxy import setup_cli
from hermes_model_proxy.server import listening


class Listener(unittest.IsolatedAsyncioTestCase):
    async def test_bind_constraints(self):
        for host in ('0.0.0.0', '::', 'example.invalid', 'localhost'):
            with self.assertRaises(ValueError):
                async with listening(web.Application(), host=host):
                    self.fail('Bound invalid host')
        for port in (-1, 65536, True):
            with self.assertRaises(ValueError):
                async with listening(web.Application(), port=port):
                    self.fail('Bound invalid port')

    async def test_cleanup(self):
        app = web.Application()
        cleaned = []

        async def cleanup(app):
            cleaned.append(True)

        app.on_cleanup.append(cleanup)
        with self.assertRaisesRegex(RuntimeError, 'synthetic'):
            async with listening(app):
                raise RuntimeError('synthetic')
        self.assertEqual(cleaned, [True])

    def test_cli_has_no_host_option(self):
        parser = argparse.ArgumentParser()
        setup_cli(parser)
        self.assertEqual({a.dest for a in parser._actions},
                         {'help', 'port', 'timeout', 'model', 'provider'})

    async def test_default_http_port_authority(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from multidict import CIMultiDict
        from hermes_model_proxy.server import local_process_only

        async def handler(request):
            return web.Response(status=200)

        cases = [
            (80, [("Host", "127.0.0.1")], 200),
            (80, [("Host", "127.0.0.1:80")], 200),
            (8647, [("Host", "127.0.0.1")], 403),
            (80, [("Host", "127.0.0.1:8647")], 403),
            (80, [("Host", "localhost")], 403),
            (80, [], 403),
            (80, [("Host", "127.0.0.1"), ("Host", "127.0.0.1")], 403),
            (80, [("Host", "127.0.0.1"), ("Origin", "null")], 403),
            (80, [("Host", "127.0.0.1"), ("Sec-Fetch-Site", "none")], 403),
        ]
        for port, headers, status in cases:
            with self.subTest(port=port, headers=headers):
                request = SimpleNamespace(
                    transport=Mock(get_extra_info=Mock(return_value=("127.0.0.1", port))),
                    headers=CIMultiDict(headers),
                )
                self.assertEqual((await local_process_only(request, handler)).status, status)

    async def test_socket_authority_guard_before_handler(self):
        calls = []

        async def handler(request):
            calls.append(True)
            return web.Response(text="ok")

        app = web.Application()
        app.router.add_post("/", handler)
        async with listening(app) as url, ClientSession() as client:
            authority = url.removeprefix("http://")
            cases = [
                [("Host", "127.0.0.1:1"), ("X-Forwarded-Host", authority)],
                [("Host", "example.invalid"), ("Forwarded", "host=" + authority)],
                [("Origin", "")], [("Sec-Fetch-Mode", "")],
                [("sEc-FeTcH-Custom", "native")],
            ]
            for headers in cases:
                with self.subTest(headers=headers):
                    async with client.post(url, headers=headers, data=b"invalid") as response:
                        self.assertEqual(response.status, 403)
            reader, writer = await asyncio.open_connection("127.0.0.1", int(authority.split(":")[1]))
            try:
                writer.write((f"POST / HTTP/1.1\r\nHost: {authority}\r\n"
                              f"Host: {authority}\r\nContent-Length: 0\r\n"
                              "Connection: close\r\n\r\n").encode())
                await writer.drain()
                # aiohttp may reject duplicate Host headers before middleware.
                self.assertIn((await reader.readline()).split()[1], (b"400", b"403"))
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
            self.assertEqual(calls, [])
            async with client.post(url) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(calls, [True])
