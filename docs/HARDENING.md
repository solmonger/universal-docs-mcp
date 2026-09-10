# Local hardening acceptance contract

This branch is a release candidate, not a published release or a claim of complete API-documentation coverage.

## Required checks

- Manifest output omits entire direct URL, Git and local-path references; synthetic credential canaries in references or malformed URL/Git/path-like names must not appear in results, error text or stored `Pin` objects. Otherwise valid-looking names, markers and upstream prose are not a general-purpose DLP guarantee.
- Malformed/unsupported manifest data is an explicit error, never successful empty dependency discovery. Reads are bounded; Python requirement parsing uses `packaging`, not a prefix regex.
- Exact pins follow their ecosystem: Cargo's bare versions and npm's partial versions are ranges. Git/path/private-registry dependencies are not offered as public-registry pins. Python extras and environment markers remain visible, unevaluated.
- Public tool inputs are validated before I/O. Tool execution failures use `isError: true` with safe structured data; malformed tool requests such as an unknown tool use a JSON-RPC protocol error (`-32602`) with a fixed message that does not reflect names or arguments. Upstream outages are unknown, not absence. The real stdio process must not reflect rejected input in SDK stderr, and the 128 KiB bound must cover the serialized MCP result, including both representations and escaping.
- Local file access has explicit scope; network requests are restricted to intended public registries/GitHub, with bounded resource use.
- Tests exercise adversarial inputs and a real stdio client. A fresh wheel installation can initialize, list tools, read an authorized synthetic manifest, retrieve pinned public documentation, reuse cache and report safe errors.
- An independent reviewer checks the exact final object. Public push, merge and publishing require separate approval.

## Product boundary

README/registry descriptions only, not symbol search or full API sites. Git-ref README provenance is weaker than published artifact identity. Version ranges are not resolved by this server; consult the project's supported lockfile or package manager rather than assuming latest satisfies the range. Compaction uses approximate content-only budgets and can clip examples.

## Current-client stdio gate

Run `scripts/protocol_compat_probe.py` from a disposable environment containing the current SDK (for example, `mcp==2.2.0`) and pass `--server-python` from the separately locked v1 server environment. The probe uses the SDK's `mode="auto"` client, records the raw modern-discovery response, checks legacy fallback, stable tool schemas, cache/manifest/docs calls, fixed wire errors, cancellation coverage in the test suite, and stdout/stderr canary absence. A legacy fallback proves interoperability only; it is not 2026-07-28 server conformance. Keep the production `mcp<2` bound unless a measured client requirement justifies a separately reviewed port.

## References

- [MCP tools, validation and error semantics](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- [Python requirement parser](https://packaging.pypa.io/en/stable/requirements.html)
- [Cargo version requirements](https://doc.rust-lang.org/cargo/reference/specifying-dependencies.html)
