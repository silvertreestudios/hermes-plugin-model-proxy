# model-proxy for Hermes

A Hermes plugin providing `hermes model-proxy`: a localhost, non-streaming text
Chat Completions endpoint using the public `ctx.llm.acomplete` and optional
`ctx.llm.acomplete_structured` APIs. Hermes
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
files, never a development environment or auth/config files. The root `__init__.py`
loads its own source tree within Hermes's isolated plugin
namespace, so an installed wheel or earlier directory load cannot shadow it.
When both are present, Hermes' directory discovery takes precedence. Installation
and enablement are operator actions: this plugin never enables itself or grants permissions.

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

Optional `reasoning_effort` accepts Hermes' public string levels: `minimal`,
`low`, `medium`, `high`, `xhigh`, `max`, `ultra`, and `none` to request disabled
reasoning. Normalization follows the host parser: surrounding whitespace and case
are normalized; string aliases `false` and `disabled` mean `none`. Non-string
values (including JSON booleans and objects), unknown levels, and client task or
extra-body selectors are rejected before inference. Omitting the field preserves
the ordinary host route and adds no reasoning configuration.

Each canonical effort uses a finite plugin-owned auxiliary task named
`model_proxy_reflection_<effort>` with matching `reasoning_effort` defaults,
registered using the host public API. Requests use the public LLM facade; active
selection needs no model/provider override grants. There is no Sol or Codex
restriction. Explicit selections remain subject to the existing host permission
gates. Conflicting task effort, extra body, routing or fallback configuration fails
closed with HTTP 403. Returned attribution must match the selected model/provider
where the public selection/config APIs identify them. An unresolved `auto`
provider or unspecified active model cannot be pinned before host resolution.
No profile settings or capability grants are written.

Hermes owns provider capability handling, clamping, disabled semantics, and
recovery. Accepting a host effort does not promise that every provider supports
that literal tier, or that disabled thinking is available on every model. Offline
coverage runs the real PluginLlm ownership gate and auxiliary planner through the
Codex adapter and SDK to an httpx mock transport for all host levels and disabled
reasoning. Assertions follow the host's clamped wire output; high remains high on
the tested route. No live provider claims are made.

The response does not advertise an effective effort: Hermes can strip reasoning
after a provider rejects that field. Thus a successful response is not an
attestation that the provider honored the requested reasoning control. A strict
guarantee across recovery attempts requires a host API that disables such
recovery or attests each attempt. The installed auxiliary call path does not
consume task `max_retries`; SDK `max_retries=0` does not disable the host recovery
ladder. No proxy workaround changes that behavior. Config is checked before
dispatch; concurrent configuration edits are not an atomic snapshot with the host
planner. Live provider verification remains an operator step.

For an isolated source launch, with the intended profile already active in the
shell, an operator can create a temporary project discovery directory (no install
or profile edits). Choose an unused port and leave the existing listener running:

```sh
proxy_source="$PWD"
proxy_project=$(mktemp -d "${TMPDIR:?Set TMPDIR to profile scratch}/model-proxy.XXXXXX")
mkdir -p "$proxy_project/.hermes/plugins"
ln -s "$proxy_source" "$proxy_project/.hermes/plugins/model-proxy"
(cd "$proxy_project" && HERMES_ENABLE_PROJECT_PLUGINS=1 hermes model-proxy \
  --port 18647 --model gpt-6.1-sol --timeout 300)
```

Supported fields are `messages`, optional `model`, `stream=false`, `n=1`,
`max_tokens` or its alias `max_completion_tokens` (integer 1–32768, default 1024;
providing both is rejected), `temperature` (finite 0–2), and
`timeout` (finite seconds, 0.1 through the configured ceiling). CLI `--timeout`
sets the ceiling/default, 0.1–300 seconds (default 90). There must be 1–1024
messages, each containing exactly `role` and string `content`; roles may be
system/developer/user/assistant and are passed through unchanged for plain requests. Booleans are
not numeric values. Other fields, duplicate JSON keys, nonstandard JSON numbers,
query parameters, multimodal content, streaming, tools/functions and Responses
are unsupported. Requests are limited to 1,000,000 bytes.

Optional `response_format: {"type":"json_object"}` uses the host's
`acomplete_structured` method with JSON mode and native text input. This requires
a compatible Hermes exposing that method and a provider supporting the host's
JSON response format. There is no plain-text fallback or guarantee of constrained
decoding. JSON requests accept only one nonempty user message, optionally preceded
by one system message; other layouts are rejected because the facade rebuilds
structured messages. The host adds JSON instructions. Plain requests retain all
message roles and ordering. Extra response-format keys, `text`, and `json_schema`
formats are rejected; schema support is outside this scoped JSON judge contract.
For example, ValleyTalk can send `messages: [{"role":"user","content":"..."}]`,
`response_format: {"type":"json_object"}`, and `max_completion_tokens: 2048`.

Responses use the OpenAI text completion envelope, with host-returned text,
actual token usage supplied by the facade, and validated model/provider
attribution. JSON responses serialize the facade's validated object as assistant
content; missing/invalid parsed objects or non-JSON results fail with HTTP 502.
A host lacking the structured method also returns 502. Usage is not estimated by
the plugin; Hermes may report zero when the provider omits counts. The facade
exposes no finish reason, so `stop` denotes
a returned text completion and cannot distinguish provider token truncation.
Errors contain fixed messages rather than exceptions, prompts or credentials.
No client credentials are forwarded; common credential headers are rejected.
No CORS is enabled. Host/Origin/Sec-Fetch checks protect the
listener: use native clients and the printed numeric URL. Other local processes
can still make requests and incur inference costs; there is no local client auth.

On disconnect or timeout the request directly awaiting the facade is cancelled;
the plugin creates no background inference tasks or upstream sessions. Listener
and signal handlers are cleaned up on exit. Host/provider cancellation behavior
and remote work completion are outside this plugin's control. Embedded callers
must configure dependency log redaction; the CLI suppresses aiohttp exception and
access logs. Host audit/log policy remains operator-owned.

## GitHub release wheels

After human review and merge, every push to `main` automatically runs the full
Python 3.11–3.13 CI matrix and publishes its exact tested artifacts as a public
GitHub Release. A direct push to `main` also qualifies. PR and feature CI exercise
the same stamp, build, metadata, checksum and upload path without publishing.
No dispatch or pre-existing version tag is required.

The tracked base version remains `0.1.0`. Each ephemeral build checkout stamps
project and plugin versions as `0.1.0+g<12hexSHA>`; the release and tag are
`0.1.0-<12hexSHA>`, derived from the full event commit. The conventional `g`
prefix makes the PEP 440 local identifier alphanumeric, preserving leading zeros
even for numeric-only SHAs. There are no committed version bumps or backwrites.
The current package has no embedded `[tool.hermes]` manifest; its wheel plugin
entry point uses distribution metadata, and the sdist includes `plugin.yaml`.

Publication downloads the current run's Python 3.11 artifacts only after every
matrix job succeeds. It never rebuilds. A paginated lookup rejects any existing
release (including drafts); lookup errors, malformed responses, checksum failures,
or an existing tag pointing to a different full commit stop publication. A missing
tag is created with explicit `--target FULLSHA`. Tags must remain immutable during
publication. Releases are never overwritten; failed or ambiguous retries require
inspection, including any temporary draft left by the standard `gh release create`
upload-then-publish flow. There is no manual draft publication gate.

Concurrency is per full commit, so successive main pushes each retain their own
release run, and publication is not cancelled midway. Whole-run retries replace
that run's CI artifact; failed-job retries reuse the successful upload from that
same run. Artifacts expire in seven days. Releases explicitly use `--latest=false`.

`SHA256SUMS` also records the full tested commit, release and package versions;
publication checks these against the event commit.

Assets are `hermes_model_proxy-0.1.0+g<12hexSHA>-py3-none-any.whl`,
`hermes_model_proxy-0.1.0+g<12hexSHA>.tar.gz`, and `SHA256SUMS`. Download using
the explicit `releases/download/0.1.0-<12hexSHA>/` URL, verify SHA256SUMS, then
install the wheel with pip. Never use a floating `latest` URL.

For reproducible Docker or ops installation, record the reviewed wheel URL **and
its exact SHA256** in the consuming build configuration and verify that pinned
hash before installation. `SHA256SUMS` checks transfer integrity; downloading it
alongside the wheel alone is not an independent trust anchor. Dependencies still
need their own pins in that environment. Install in Hermes' environment and
enable the plugin explicitly as described above. No package index publication or
ops configuration change is part of this workflow.

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
ruff check src tests scripts __init__.py
python -W error -m compileall -q src tests scripts __init__.py
```

Tests cover the public host facade, default selection and trust gates, directory
registration/Plugin Doctor, wheel discovery, request validation, listener security,
cancellation and foreground lifecycle. CI runs Linux Python 3.11–3.13. This contract
is limited to the public host facade; offline checks do not establish live provider
or credential behavior. The repository remains
`silvertreestudios/hermes-plugin-model-proxy`.
