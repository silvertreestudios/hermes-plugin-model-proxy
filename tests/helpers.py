"""Synthetic fixtures; transport uses the actual installed Hermes proxy."""
from hermes_cli.proxy.adapters.base import UpstreamCredential
from hermes_proxy_codex.adapter import CodexAdapter
from hermes_proxy_codex.server import listening as listening

MODEL = "synthetic-test-model"


class Fake(CodexAdapter):
    def __init__(self, url, fail=False):
        self.url, self.fail, self.calls = url, fail, 0

    def get_credential(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("Sanitized auth failure")
        return UpstreamCredential("mock-bearer", self.url)

    def is_authenticated(self):
        return not self.fail
