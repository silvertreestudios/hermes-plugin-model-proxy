import unittest
from unittest.mock import patch
from hermes_proxy_codex.chat import events


async def lines(raw):
    for line in raw.splitlines(keepends=True):
        yield line


class Parser(unittest.IsolatedAsyncioTestCase):
    async def test_frames_utf8_multiline_and_comments(self):
        raw = b': comment\r\nevent: test\r\ndata: {"type":\r\ndata: "test"}\r\n\r\n'
        self.assertEqual([e async for e in events(lines(raw))], [{'type': 'test'}])

    async def test_limits_and_invalid_frames(self):
        for raw in (b'data: {}', b'data: {}\n', b'data: []\n\n', b'data: \xff\n\n', b'junk\n\n'):
            with self.assertRaises((ValueError, UnicodeError)):
                _ = [e async for e in events(lines(raw))]
        with patch('hermes_proxy_codex.chat.MAX_BYTES', 10):
            with self.assertRaises(ValueError):
                _ = [e async for e in events(lines(b'data: {"type":"test"}\n\n'))]
