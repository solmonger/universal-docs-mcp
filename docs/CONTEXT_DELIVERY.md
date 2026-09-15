# Per-run context delivery

One retrieval core serves both MCP tools and the preflight CLI. One delivery core
validates receipts and formats bounded context. Harness plugins only trigger it
and deliver its output. MCP discovery alone does not make a model call a tool.

## Neutral interface

```text
universal-docs-context --request-file /absolute/trusted/request.json
```

The request uses the same strict package/official-source schema as
`universal-docs-preflight`. The neutral command runs that core in-process and emits
one UTF-8 JSON line (`universal-docs.context/v1`):

- `prepared` or `prepared_stale`: exit 0, a nonempty bounded context packet;
- `unavailable`: exit 1, empty context and a stable error code.

The status is based on the bytes actually emitted, including serialization
expansion. The complete frame is limited to 16 KiB including its newline; the
context packet is limited to 8 KiB. Version binding, full source hash, fetch/check
times and installed-state uncertainty remain visible to the model. An unverified
Git ref is explicitly not proof of matching a requested package release.

A package request can select an exact requested version or latest observed stable
version. An official request selects a cataloged source/version, for example:

```json
{"source_id":"mcp-tools","selection":"requested","requested_version":"2026-07-28","query":"tools/list","freshness_mode":"require_check","context_max_bytes":4000,"deadline_ms":2000}
```

Only the reviewed MCP tools Markdown source is in the official-source catalog.
This is not unrestricted URL retrieval or comprehensive official API coverage for
all packages. Installed package/manifest inference is not performed by preflight.
No-match results are visible in raw preflight receipts and are not successful
context delivery. Source/network failures remain unavailable rather than invented
freshness. Stale fallback requires the explicit `allow_stale` policy.

## Integrity and authority

The producer hashes the exact selected UTF-8 context and binds its digest to the
reported target and source metadata. Delivery recomputes this binding and the
context byte count and rejects inconsistent envelopes or request-budget overruns.
This detects corruption; it is **not publisher authentication**. The fixed
retriever and local cache are trusted. The full-source digest is retriever-derived;
the receiver recomputes the selected-context digest without pretending to hold or
authenticate the whole upstream document. Coherent fabrication by a compromised
trusted executable is outside this single-user contract.

## Harness seams

- **Claude Code / Codex:** command hooks use the same request parser and receipt
  formatter. See [COMMAND_HOOKS.md](COMMAND_HOOKS.md). The Codex example allows the
  already-bounded full packet; a smaller host limit can preserve header/footer
  markers while dropping text in between. Native checks compare the entire
  expected packet, not just a success marker. The Codex test's trust override is
  restricted to vetted, isolated fixture configuration—not normal installation.
- **Hermes:** [plugin example](../examples/hermes/universal-docs-preflight/README.md).
  `pre_llm_call` supplies current-user context and is explicitly fail-open.
  Native checks cover first/next/post-compaction turns; an opt-in live check
  retrieves public PyPI documentation through the actual context CLI.
- **OpenClaw / Pi:** the shared POSIX Node boundary is in
  [adapters/node](../adapters/node/README.md). Native OpenClaw and Pi lifecycle gates now also exercise live public documentation
  through the real context CLI on first/next prompts; no active installation is implied. Pi's
  extension path is not native MCP support.

All executables, request files and cache scopes are trusted fixed configuration,
not inbound prompts or transcript paths. Upstream documents remain untrusted data.
No plugin is installed automatically; no user profile is changed by building.

## Live Hermes verification

```bash
UNIVERSAL_DOCS_LIVE_TESTS=1 UNIVERSAL_DOCS_HERMES_LIVE_TESTS=1 \
  UNIVERSAL_DOCS_HERMES_PYTHON=/absolute/path/to/hermes/python \
  UNIVERSAL_DOCS_HERMES_HOST_EVIDENCE_DIR=/absolute/path/to/evidence \
  python -m pytest tests/test_hermes_live_context.py -m live -q
```

This verifies native message assembly with real public retrieval, not a paid model
response or activation of the user's desktop plugin. Fresh installed-wheel and
independent exact-object acceptance are separate release gates.
