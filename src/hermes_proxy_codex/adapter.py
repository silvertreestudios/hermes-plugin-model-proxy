"""Codex adapter using Hermes' existing read-only credential resolver."""
import os
from pathlib import Path

from hermes_constants import get_hermes_home
from hermes_cli.auth import resolve_codex_runtime_credentials
from hermes_cli.auth_codex import _codex_access_token_is_expiring
from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential

BASE = "https://chatgpt.com/backend-api/codex"


class CodexAdapter(UpstreamAdapter):
    """Use the explicitly selected Hermes home, without login or token refresh.

    Hermes owns auth-store format and its configured shared-root inheritance.
    The bridge does not discover credentials in other named profiles or import
    Codex CLI logins. Upstream read-only resolver behavior remains Hermes-owned.
    """

    name = "codex"
    display_name = "Codex local proxy"
    allowed_paths = frozenset({"/responses"})

    def __init__(self):
        selected = os.environ.get("HERMES_HOME", "").strip()
        if not selected or not Path(selected).is_absolute():
            raise RuntimeError("Set HERMES_HOME to an explicit absolute directory")
        self.home = Path(selected).resolve()
        if get_hermes_home().resolve() != self.home:
            raise RuntimeError("Hermes home differs from explicit HERMES_HOME")

    def get_credential(self):
        """Resolve an existing credential; fail closed and redact resolver errors."""
        try:
            selected = os.environ.get("HERMES_HOME", "").strip()
            if (not selected or not Path(selected).is_absolute()
                    or Path(selected).resolve() != self.home
                    or get_hermes_home().resolve() != self.home):
                raise ValueError()
            if os.environ.get("HERMES_CODEX_BASE_URL", BASE).rstrip("/") != BASE:
                raise ValueError()
            # read_only returns before CLI adoption/refresh paths in the pinned
            # resolver. Do not call the normal mutating resolver as a fallback.
            result = resolve_codex_runtime_credentials(read_only=True)
            token = result.get("api_key")
            if (result.get("base_url", "").rstrip("/") != BASE
                    or not isinstance(token, str) or not token
                    or any(not 33 <= ord(c) <= 126 for c in token)):
                raise ValueError()
            # The pinned resolver's read-only path skips expiry checks. Reuse
            # Hermes' token helper without invoking refresh or parsing the store.
            if _codex_access_token_is_expiring(token, skew_seconds=30):
                raise ValueError()
            return UpstreamCredential(bearer=token, base_url=BASE)
        except Exception:
            raise RuntimeError("Selected Hermes credential unavailable; renew authentication separately") from None

    def is_authenticated(self):
        try:
            self.get_credential()
            return True
        except RuntimeError:
            return False
