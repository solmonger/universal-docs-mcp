# Universal docs preflight Hermes hook candidate

This is one directory plugin candidate for Hermes `pre_llm_call`. It invokes a trusted,
absolute `universal-docs-preflight` executable once per turn and returns bounded documentation
context through Hermes's user-message injection channel. It never changes the system prompt or
the durable user text.

## Profile configuration

Copy this directory into the target profile only after review. The executable path is explicit
trusted configuration; it is not read from the inbound user message, transcript, current
working directory, or model output.

```yaml
plugins:
  enabled:
    - universal-docs-preflight
  entries:
    universal-docs-preflight:
      settings:
        executable: /absolute/path/to/universal-docs-preflight
        package: mcp
        ecosystem: python
        selection: latest
        requested_version: null
        query: install
        section_ids: [installation]
        context_max_bytes: 8000
        freshness_mode: require_check
        deadline_ms: 3000
```

`executable`, `package`, and a known matching `query` are required. `selection` is `latest` or
`requested`; use `requested_version` with `requested`. Use package-specific `section_ids` when
known; this adapter does not infer intent from inbound user text. The child receives only the documented preflight JSON
fields, a fixed credential/proxy-free environment, no shell, and a bounded wall-clock/CPU
budget. Child stderr is discarded and child output is capped before parsing.

The returned context starts with `DOCS_PREFLIGHT_STATUS=ok|missing|stale`, a source label, and
`DOCS_PREFLIGHT_RECEIPT_JSON=...`. Upstream text is labeled `UNTRUSTED DOCUMENTATION DATA`.
Fetch, timeout, malformed-output, and configuration failures return an explicit missing/stale
marker and do not veto the Hermes turn. Hermes itself remains fail-open: if this plugin is not
enabled, is missing, or is skipped by plugin discovery, no hook invocation is claimed.
