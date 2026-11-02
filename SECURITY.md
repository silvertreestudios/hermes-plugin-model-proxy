# Security

This bridge is for trusted local processes. Loopback binding is not client
authentication. Browser-origin/Fetch Metadata requests and non-matching Host
headers are rejected before route processing;
this does not restrict other local processes. Do not expose it through port
forwarding or a public reverse proxy.
Keep the selected Hermes home operator-controlled, including any symlink targets.
Never put credentials, auth stores, request logs, or account identifiers in this
repository or a public issue.

For a vulnerability, use GitHub's private vulnerability reporting facility if the
repository enables it. Otherwise open a public issue requesting a private reporting
channel without including exploit details or sensitive information. No private
reporting destination is promised by this repository.

The model-proxy plugin uses only `ctx.llm.acomplete`; Hermes owns credentials,
refresh and routing. Overrides require host grants/allowlists. Clients cannot
select profiles or base URLs, and credential headers are never forwarded. No
permissions, plugin enablement or host configuration are changed automatically.
All inference failures use fixed error messages. Only non-streaming text Chat
is supported; request bodies, fields, numeric ranges and model attribution are
validated. Disconnect/timeout cancellation releases plugin-owned request/listener
resources; it does not guarantee host transport cleanup or remote cancellation.
Host logs, routing/fallback and provider behavior remain operator responsibilities.

Dependency updates and publication require owner review. Offline tests do not
establish live model or credential behavior.
