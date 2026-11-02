# Security

This bridge is for trusted local processes. Loopback binding is not client
authentication. Browser-origin/Fetch Metadata requests and non-matching Host
headers are rejected before route processing, including on the private listener;
this does not restrict other local processes. Do not expose it through port
forwarding or a public reverse proxy.
Keep the selected Hermes home operator-controlled, including any symlink targets.
Never put credentials, auth stores, request logs, or account identifiers in this
repository or a public issue.

For a vulnerability, use GitHub's private vulnerability reporting facility if the
repository enables it. Otherwise open a public issue requesting a private reporting
channel without including exploit details or sensitive information. No private
reporting destination is promised by this repository.

The Chat route sanitizes failures and isolates provider headers. Native Responses
is raw upstream passthrough, including errors and headers; see README limitations.
Dependency updates and publication require owner review. Offline tests do not
establish live model or credential behavior.
