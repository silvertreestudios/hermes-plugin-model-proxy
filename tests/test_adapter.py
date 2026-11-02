"""Auth tests patch the resolver and never read real credential stores."""
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hermes_proxy_codex.adapter import BASE, CodexAdapter


class Auth(unittest.TestCase):
    def test_explicit_home_required(self):
        for env in ({}, {'HERMES_HOME': ''}, {'HERMES_HOME': 'relative'}):
            with patch.dict(os.environ, env, clear=True), self.assertRaises(RuntimeError):
                CodexAdapter()

    def test_read_only_resolver_contract(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'HERMES_HOME': root}, clear=True):
            with patch('hermes_proxy_codex.adapter.resolve_codex_runtime_credentials', return_value={
                    'api_key': 'synthetic-bearer', 'base_url': BASE}) as resolve:
                adapter = CodexAdapter()
                credential = adapter.get_credential()
                self.assertEqual(credential.bearer, 'synthetic-bearer')
                self.assertEqual(credential.base_url, BASE)
                resolve.assert_called_once_with(read_only=True)
                self.assertTrue(adapter.is_authenticated())
                self.assertEqual(list(Path(root).iterdir()), [])

    def test_invalid_results_fail_closed(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'HERMES_HOME': root}, clear=True):
            for result in ({}, None, {'api_key': '', 'base_url': BASE},
                           {'api_key': 'synthetic\nheader', 'base_url': BASE},
                           {'api_key': 'synthetic-bearer', 'base_url': 'https://example.invalid'}):
                with patch('hermes_proxy_codex.adapter.resolve_codex_runtime_credentials', return_value=result):
                    self.assertFalse(CodexAdapter().is_authenticated())

    def test_resolver_failure_sanitized_and_not_retried(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'HERMES_HOME': root}, clear=True):
            with patch('hermes_proxy_codex.adapter.resolve_codex_runtime_credentials',
                       side_effect=RuntimeError('synthetic-private-detail')) as resolve:
                with self.assertRaises(RuntimeError) as caught:
                    CodexAdapter().get_credential()
                self.assertNotIn('synthetic-private-detail', str(caught.exception))
                resolve.assert_called_once_with(read_only=True)

    def test_endpoint_and_home_changes_before_resolution(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'HERMES_HOME': root}, clear=True):
            with patch('hermes_proxy_codex.adapter.resolve_codex_runtime_credentials') as resolve:
                adapter = CodexAdapter()
                for env in ({'HERMES_CODEX_BASE_URL': 'https://example.invalid'}, {'HERMES_HOME': root+'/different'}):
                    with patch.dict(os.environ, env):
                        self.assertFalse(adapter.is_authenticated())
                with patch('hermes_proxy_codex.adapter.get_hermes_home', return_value=Path(root)/'different'):
                    self.assertFalse(adapter.is_authenticated())
                    with self.assertRaises(RuntimeError):
                        CodexAdapter()
                resolve.assert_not_called()

    def test_expired_jwt_fails_authentication_without_refresh(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'HERMES_HOME': root}, clear=True):
            for expiry, expected in ((1, False), (4_000_000_000, True)):
                payload = base64.urlsafe_b64encode(json.dumps({'exp': expiry}).encode()).decode().rstrip('=')
                with patch('hermes_proxy_codex.adapter.resolve_codex_runtime_credentials', return_value={
                        'api_key': 'synthetic.' + payload + '.signature', 'base_url': BASE}) as resolve:
                    self.assertEqual(CodexAdapter().is_authenticated(), expected)
                    resolve.assert_called_once_with(read_only=True)

    def test_actual_upstream_header_helper(self):
        from agent.codex_headers import codex_cloudflare_headers
        payload = {'https://api.openai.com/auth': {
            'chatgpt_account_id': 'synthetic-account', 'chatgpt_data_residency': 'synthetic-region'}}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
        headers = codex_cloudflare_headers('synthetic.'+encoded+'.signature', base_url=BASE)
        self.assertEqual(headers['originator'], 'hermes-agent')
        self.assertEqual(headers['ChatGPT-Account-ID'], 'synthetic-account')
        self.assertEqual(headers['x-openai-internal-codex-residency'], 'synthetic-region')
        self.assertNotIn('Authorization', headers)
