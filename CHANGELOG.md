# Changelog

## 0.3.0rc2 — local review-repair candidate, not yet published

- Replace raw dependency/SDK diagnostics with attributed, severity-preserving redacted events in the standalone process; keep structured tool errors and sanitized application failures visible.
- Reject malformed URL/Git/path-like dependency identities and Cargo aliases before returning or storing `Pin` objects.
- Reuse canonical exact raw cache entries before registry lookup; latest lookups establish exact aliases without extending source freshness.
- Enforce the 128 KiB bound against the serialized MCP result, including text/structured duplication and escaping.
- Add runtime compatibility upper bounds and real stdio regressions for name privacy, stderr reflection and oversized responses.

## 0.3.0rc1 — local release candidate, not yet published

### Security and reliability

- Redact entire dependency URL/Git/path references rather than expose URL credentials.
- Disable manifest reads without an explicit project root; secure descriptor-relative POSIX reads reject path escapes, symlinks and special files.
- Enforce argument schemas before I/O without echoing sensitive invalid values in SDK validation errors.
- Use safe structured MCP errors with `isError`, timeouts, concurrency backpressure and payload limits.
- Bound HTTP reads, reject redirects/unexpected compression, reuse connections during server lifetime.
- Bound SQLite cache values/entries/logical bytes and degrade visibly to uncached retrieval if persistence fails.

### Correctness and usability

- Reject malformed manifests instead of reporting empty success; preserve Python extras/markers and correct Cargo/npm exact-pin semantics.
- Filter known yanked/prerelease releases; do not invent a stable release without eligible release records. Preserve npm's stable latest tag.
- Allow exact cached docs without a metadata network call; cache aliases cannot extend original documentation TTL.
- Mark GitHub refs as unverified package-version bindings.
- Paginate outlines and bound section metadata; preserve fenced examples and flag content clipping.
- Add a hash-locked verification environment, offline regression tests, real stdio tests, optional live smoke and wheel-based CI.

### Compatibility changes

- SDK requirement is `mcp>=1.26,<2`.
- Manifest tool requires `UNIVERSAL_DOCS_PROJECT_ROOT`; secure manifest access is currently POSIX-only.
- Nonregistry specs are intentionally lossy/redacted and not offered as public-registry pins.
- Invalid inputs, upstream failures and misses consistently use JSON/MCP error signaling; unavailable cache counters are null.
- No public push/publish or change to existing client/profile configuration is part of this candidate.
