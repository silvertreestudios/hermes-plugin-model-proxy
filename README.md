# model-proxy for Hermes

A Hermes plugin providing `hermes model-proxy`: a localhost, non-streaming text
Chat Completions endpoint using only the public `ctx.llm.acomplete` API. Hermes
owns provider routing, authentication and refresh. The plugin registers one CLI
command and no tools, hooks, skills, or agent loop. The existing standalone Codex
application remains available below.

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
No CORS is enabled. Host/Origin/Sec-Fetch checks are shared with the standalone
listener: use native clients and the printed numeric URL. Other local processes
can still make requests and incur inference costs; there is no local client auth.

On disconnect or timeout the request directly awaiting `acomplete` is cancelled;
the plugin creates no background inference tasks or upstream sessions. Listener
and signal handlers are cleaned up on exit. Host/provider cancellation behavior
and remote work completion are outside this plugin's control. Embedded callers
must configure dependency log redaction; the CLI suppresses aiohttp exception and
access logs. Host audit/log policy remains operator-owned.

# Standalone Codex application

A small local bridge from text Chat Completions to Codex Responses using the
Hermes native proxy. It runs no agent loop, persona, tools, memory, or skills.
This is a narrow compatibility adapter, not a production-readiness claim.

Requires Linux, Python 3.11–3.13, and an existing compatible installation of
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).
The supported contract is Hermes 0.21.5, source revision
`749220ef0007f8d87bd1531f1c24b0fe93816385`. Hermes is a host dependency,
not an automatically installed dependency: do not install a similarly named PyPI
package. Install this package into the same environment as Hermes:

```sh
python -m pip install .
# Explicitly select your existing Hermes home before starting:
export HERMES_HOME="$HOME/.hermes"
hermes-proxy-codex --port 8646
```

The command prints `http://127.0.0.1:8646/v1`. Only IPv4 loopback is supported;
there is no public-bind option. Port 0 selects an available port. Stop with
SIGINT or SIGTERM. Startup checks credentials without a model call.

```sh
curl http://127.0.0.1:8646/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"YOUR_CODEX_MODEL","messages":[{"role":"user","content":"Hello"}],"stream":false}'
```

Supply a model you are entitled to use. There is no default model, model discovery,
provider fallback, or retry. Generic clients use the local base URL and need no
subscriber credential. Never give them the upstream token.

Supported Chat request fields: `model`, `messages`, `stream` (boolean), and `n=1`.
Messages contain only `role` and string `content`. Leading system/developer messages
are flattened into one instructions string joined by two newlines: their distinct
role priority is lost. Conversation roles user/assistant must alternate. At least
one conversation message is required. Token limits, temperature, tools/functions,
images, named messages, response formats, stream options, and all other fields
are rejected, as are query parameters. Requests and upstream SSE are bounded to
1,000,000 bytes; the internal HTTP hop has a 90-second total timeout.

JSON responses include assistant text, `stop`, and mapped token counts. Streaming
returns text deltas; `stop` and `[DONE]` appear only after a valid completed terminal
with matching model, text, and usage. Both SSE and octet-stream media types are
accepted, but framed UTF-8 SSE and completed-terminal validation are required.
A stream can deliver partial text before an error; errors never imply success and
do not include `[DONE]`. Reasoning output is skipped; other non-text output fails.
Streaming usage chunks are not supported.

Authentication delegates to Hermes' existing credential resolver with
`read_only=True`. An absolute `HERMES_HOME` must be explicitly selected and must
match Hermes' `get_hermes_home()` helper. Hermes owns store formats and its configured
shared-root authentication inheritance. Because its read-only resolver skips expiry
checks, the bridge also uses Hermes' non-mutating token helper to reject expired
or expiring-within-30-seconds JWTs. It does not search other named profiles or parse
auth stores itself. Missing or expired credentials fail closed. Renew authentication
separately through Hermes.
There is no login, token refresh, Codex CLI adoption or mutating-resolver fallback.
Upstream read-only resolution is not a blanket filesystem-write guarantee:
Hermes' corrupt-store recovery can preserve a backup. Keep auth stores valid
and operator-controlled. No host configuration or credentials are provisioned by
this package. Concurrent external renewal can race the two reads used for Chat
provider headers and native bearer attachment; a request may fail and is never
retried automatically.

Chat composition builds provider identity/account headers using Hermes' helper and
sends them to a fixed private loopback listener. Caller headers and Host cannot
select that destination or override those provider headers. The native server
attaches the bearer; no extra credential fields extend its adapter contract.

The exposed `/v1/responses` route remains native passthrough. Its body, query,
upstream errors and response headers are not Chat-sanitized or terminal-validated;
its native request limit is 10 MB and timeout behavior is Hermes' own. It requires
caller-supplied provider identity/account headers where the upstream needs them.
Do not assume it has Chat's supported-subset checks or header isolation. `/health`
is local. Other native paths are denied. This service has no client authentication;
use it only with trusted local processes, never behind a public forwarder.

The public and private listeners reject any Origin or Sec-Fetch header and require
Host to match the bound `127.0.0.1:PORT` authority before reading bodies or resolving
credentials. Use the printed numeric loopback URL; browser clients and localhost
aliases are intentionally unsupported. These checks reduce browser/DNS-rebinding
exposure; they do not authenticate another local process.
The console suppresses dependency exception/access logs; embedding applications must
configure their own log redaction before running the library.

## Development

CI uses the real pinned upstream proxy and helper modules with synthetic local
HTTP fixtures. No credentials, live inference, or secrets are needed. It does not
exercise real authentication, token refresh, or model compatibility.

A minimal standalone test environment can use a read-only upstream source checkout
rather than installing all Hermes dependencies or extras:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-ci.txt -r requirements-dev.txt
python -m pip install --no-deps --no-build-isolation .
# HERMES_SOURCE is a checkout of the supported public upstream revision.
PYTHONPATH="$HERMES_SOURCE:tests" python -m unittest discover -s tests -v
ruff check src tests
python -m compileall -q src tests
python -m hatchling build
python tests/check_artifacts.py
```

`requirements-ci.txt` pins aiohttp plus PyYAML and httpx with their dependency closure.
The latter two are required because the upstream adapter registry imports auth
modules at initialization; these tests do not call their external services. No Hermes tool extras are needed. The same suite can optionally check an
existing Hermes source installation by setting `HERMES_SOURCE` accordingly; a fake
adapter supplies credentials, but the proxy implementation and header helper are
upstream code. There are no fake replacements for Hermes modules. CI runs Linux
Python 3.11–3.13 and builds/installs from both wheel and source distribution.
