# Experimental Rust read-only API client

This independent example is not the planned M6 SDK. It implements only the
`getNode` and `listDeployments` GET operations in the current
[OpenAPI document](../../docs/contracts/openapi.json); no other operation is
available. `NodeStatus` is a small typed projection, and deployment items are
returned as raw JSON. This is not a full OpenAPI schema validator. The crate is
a workspace member, using the repository's existing dependency lockfile and CI.

Construct `XeLabClient` with an exact `https://host[:port]` origin, a bearer
credential, a PEM CA certificate, and a positive timeout. Only the explicitly
supplied CA is trusted; certificate and hostname checks remain enabled. The
client refuses redirects, limits response bodies to 4 MiB, and never retries.
`list_deployments` returns one page; pass `next_cursor` back with the same
account and query scope, before it expires. Limits are 1–200 and cursors are
1–2048 characters, as in OpenAPI. An explicit null cursor ends pagination; a
missing `next_cursor` is an invalid response rather than an end-of-list marker.

HTTP errors retain the status and, when valid JSON is returned, the standard
`ErrorEnvelope`. Non-JSON or malformed error bodies are not exposed. Transport,
timeout, invalid response, and oversized response are distinct. Formatting a
`ClientError` omits server-controlled error details; callers that explicitly
inspect those details should handle them as potentially sensitive data.

To try `getNode`, set `XELAB_API_ORIGIN`, `XELAB_API_CA_FILE` (a PEM CA file),
and `XELAB_API_TOKEN` through a secure local mechanism, then run:

```sh
cargo run --manifest-path examples/rust-api-client/Cargo.toml --locked --example read_node
```

The example prints only the numeric revision and admission state. Do not put
credentials in command arguments, source files, logs, or shell history. The
credential still needs permission for `getNode`; this example does not obtain,
refresh, or persist credentials.

From the repository root, component checks are:

```sh
cargo test --manifest-path examples/rust-api-client/Cargo.toml --locked --all-targets
cargo fmt --manifest-path examples/rust-api-client/Cargo.toml -- --check
cargo clippy --manifest-path examples/rust-api-client/Cargo.toml --locked --all-targets -- -D warnings
```

The tests use local HTTP/TLS fixtures and test credentials. They do not start a
real XeLab kernel, prove production authentication or complete M6 acceptance.
