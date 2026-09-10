# Local hardening acceptance contract

This branch is a release candidate, not a published release or a claim of complete API-documentation coverage.

## Required checks

- Manifest output omits entire direct URL, Git and local-path references; no synthetic credential canary appears in results, error text or stored `Pin` objects.
- Malformed/unsupported manifest data is an explicit error, never successful empty dependency discovery. Reads are bounded; Python requirement parsing uses `packaging`, not a prefix regex.
- Exact pins follow their ecosystem: Cargo's bare versions and npm's partial versions are ranges. Git/path/private-registry dependencies are not offered as public-registry pins. Python extras and environment markers remain visible, unevaluated.
- Public tool inputs are validated before I/O. Failures use MCP error signaling and safe structured data; upstream outages are unknown, not absence.
- Local file access has explicit scope; network requests are restricted to intended public registries/GitHub, with bounded resource use.
- Tests exercise adversarial inputs and a real stdio client. A fresh wheel installation can initialize, list tools, read an authorized synthetic manifest, retrieve pinned public documentation, reuse cache and report safe errors.
- An independent reviewer checks the exact final object. Public push, merge and publishing require separate approval.

## Product boundary

README/registry descriptions only, not symbol search or full API sites. Git-ref README provenance is weaker than published artifact identity. Version ranges are not resolved by this server; consult the project's supported lockfile or package manager rather than assuming latest satisfies the range. Compaction uses approximate content-only budgets and can clip examples.

## References

- [MCP tools, validation and error semantics](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- [Python requirement parser](https://packaging.pypa.io/en/stable/requirements.html)
- [Cargo version requirements](https://doc.rust-lang.org/cargo/reference/specifying-dependencies.html)
