# model-proxy for Hermes

A Hermes plugin providing `hermes model-proxy`: a localhost, non-streaming text
Chat Completions endpoint using only the public `ctx.llm.acomplete` API. Hermes
owns provider routing, authentication and refresh. The plugin registers one CLI
command and no tools, hooks, skills, or agent loop. The Python distribution is
`hermes-model-proxy`, with package `hermes_model_proxy`.

Requires Python 3.11–3.13, Linux, and a compatible Hermes installation. CI retains
Hermes revision `749220ef0007f8d87bd1531f1c24b0fe93816385` (0.21.5), which exposes
`PluginContext.llm`. Hermes is a host dependency; do not install a similarly named
PyPI package. See the [plugin distribution contract](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)
and [LLM access and trust gates](https://hermes-agent.nousresearch.com/docs/developer-guide/plugin-llm-access).

Install into the same Python environment as Hermes, then explicitly enable:

```sh
python -m pip install .                # or a built dist/*.whl
hermes plugins enable model-proxy
hermes model-proxy --port 8647 --timeout 90
```

For a directory installation, copy `plugin.yaml`, `__init__.py`, and the `src/`
tree into `<selected-Hermes-home>/plugins/model-proxy/`, install
`aiohttp==3.14.3` in Hermes' Python environment, then explicitly run
`hermes plugins enable model-proxy` for that profile. Copy only those project
files, never a development environment or auth/config files. Hermes loads the
root `__init__.py`; it exposes the same `register` as the wheel. When both are
present, Hermes' directory discovery takes precedence. Installation and enablement
are operator actions: this plugin never enables itself or grants permissions.

The foreground command prints `http://127.0.0.1:8647/v1`. Only the literal IPv4
loopback listener is supported; port 0 selects an available port. SIGINT/SIGTERM
stop the listener. `/health` reports listener health without inference or auth
checks. No daemon or persistent service is installed.

```sh
curl http://127.0.0.1:8647/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Hello"}],"stream":false,"max_tokens":128}'
```

Omitting `model` uses the active Hermes model/provider without override permission.
An explicit request `model` is passed as a host-gated model override, even if it
names the active model. It is never silently ignored. `--model MODEL` fixes the
selection for this listener; conflicting request models are rejected. `--provider
PROVIDER` optionally selects a provider through the host override gate; HTTP
clients cannot select providers, auth profiles, credentials, or base URLs.

The manifest and wheel declare `llm.model_override` and `llm.provider_override`.
Review consent with `hermes plugins capabilities model-proxy`; declaration alone
grants nothing. Hermes enforces per-plugin grants or legacy
`plugins.entries.model-proxy.llm.allow_model_override` and
`allow_provider_override` settings, with optional `allowed_models` and
`allowed_providers` allowlists. Grant only the capabilities needed for your use;
active-selection requests require neither. Explicit overrides fail with HTTP 403
when the host denies them. No permissions/configuration are changed by serving.
Host routing/fallback policy remains Hermes' responsibility.

Supported fields are `messages`, optional `model`, `stream=false`, `n=1`,
`max_tokens` (integer 1–32768, default 1024), `temperature` (finite 0–2), and
`timeout` (finite seconds, 0.1 through the configured ceiling). CLI `--timeout`
sets the ceiling/default, 0.1–300 seconds (default 90). There must be 1–1024
messages, each containing exactly `role` and string `content`; roles may be
system/developer/user/assistant and are passed through unchanged. Booleans are
not numeric values. Other fields, duplicate JSON keys, nonstandard JSON numbers,
query parameters, multimodal content, streaming, tools/functions, Responses and
structured output are unsupported. Requests are limited to 1,000,000 bytes.

Responses use the OpenAI text completion envelope, with host-returned text,
actual token usage supplied by the facade, and validated model/provider
attribution. Usage is not estimated by the plugin; Hermes may report zero when
the provider omits counts. The facade exposes no finish reason, so `stop` denotes
a returned text completion and cannot distinguish provider token truncation.
Errors contain fixed messages rather than exceptions, prompts or credentials.
No client credentials are forwarded; common credential headers are rejected.
No CORS is enabled. Host/Origin/Sec-Fetch checks protect the
listener: use native clients and the printed numeric URL. Other local processes
can still make requests and incur inference costs; there is no local client auth.

On disconnect or timeout the request directly awaiting `acomplete` is cancelled;
the plugin creates no background inference tasks or upstream sessions. Listener
and signal handlers are cleaned up on exit. Host/provider cancellation behavior
and remote work completion are outside this plugin's control. Embedded callers
must configure dependency log redaction; the CLI suppresses aiohttp exception and
access logs. Host audit/log policy remains operator-owned.

## Development

CI uses pinned public Hermes source and synthetic fixtures, with no credentials or
live inference. Its minimal environment installs only `requirements-ci.txt` and
`requirements-dev.txt`; Hermes remains a source host dependency. PyYAML supports
host plugin discovery. The pinned httpx dependency closure supports imports made
by the real host facade trust tests; it is not a plugin runtime dependency. No
provider SDK or native proxy transport is required.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-ci.txt -r requirements-dev.txt
python -m hatchling build
python tests/check_artifacts.py
python -m pip install --no-deps --no-build-isolation dist/*.tar.gz
# HERMES_SOURCE is the pinned public Hermes checkout.
PYTHONPATH="$HERMES_SOURCE:tests" python -W error -m unittest discover -s tests -v
python -m pip install --force-reinstall --no-deps dist/*.whl
PYTHONPATH="$HERMES_SOURCE:tests" python -W error -m unittest discover -s tests -v
ruff check src tests __init__.py
python -W error -m compileall -q src tests __init__.py
```

Tests cover the public host facade, default selection and trust gates, directory
registration/Plugin Doctor, wheel discovery, request validation, listener security,
cancellation and foreground lifecycle. CI runs Linux Python 3.11–3.13. This contract
is limited to the public host facade; offline checks do not establish live provider
or credential behavior. The repository remains
`silvertreestudios/hermes-proxy-codex`.
