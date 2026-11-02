import unittest
from aiohttp import web
from hermes_proxy_codex.cli import parser
from hermes_proxy_codex.server import listening


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
        self.assertEqual({a.dest for a in parser()._actions}, {'help', 'port'})
