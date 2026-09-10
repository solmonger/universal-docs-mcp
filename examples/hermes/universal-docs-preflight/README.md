# Hermes documentation context bridge

This local POSIX plugin candidate consumes the shared `universal-docs-context`
CLI. Source selection, request/receipt validation, freshness policy and packet
formatting have one owner in the core package; the plugin does not reinterpret
package versions or turn raw preflight dictionaries into a success claim.

## Explicit configuration (not installed automatically)

After review, copy this directory to the intended Hermes profile and configure:

```yaml
plugins:
  enabled: [universal-docs-preflight]
  hook_callback_timeout: 5
  entries:
    universal-docs-preflight:
      settings:
        executable: /absolute/path/to/universal-docs-context
        request_file: /absolute/path/to/docs-request.json
        timeout_ms: 3000
```

The trusted regular JSON request file selects the source and relevance; inbound
prompts, transcript paths, commands and working directories cannot select them.
An example package request is:

```json
{"ecosystem":"python","package":"requests","selection":"requested","requested_version":"2.32.3","query":"install","context_max_bytes":4000,"freshness_mode":"require_check","deadline_ms":2000}
```

Set the request deadline below the outer process timeout, which must be below the
host hook timeout. The plugin executes without a shell or `preexec_fn`, supplies
no stdin, excludes credentials/proxies, bounds stdout before parsing, discards
stderr, and cleans its owned process group. A concurrent invocation returns an
explicit busy marker rather than waiting in a second queue. This is resource
limiting, not an OS sandbox for arbitrary executable code.

Cache placement uses Hermes's `get_hermes_home()` (including context-local profile
overrides), not only the process environment. No source text is written into the
system prompt or durable user text. Hermes owns the current-user API sidecar.

## What the state means

- `DOCS_PREFLIGHT_STATUS=prepared` / `prepared_stale`: a validated context packet
  was prepared. It is not a claim that a model consumed or followed it.
- `DOCS_PREFLIGHT_STATUS=missing`: no documentation is injected, with a fixed
  error marker. Malformed frames and nonzero processes cannot inject text.
- **Hermes is fail-open at this hook.** Errors do not veto the turn. A missing,
  disabled or undiscovered plugin cannot attest anything. Do not advertise this
  as a fail-closed gate equivalent to a Claude/Codex blocking command hook.

The plugin invokes once per new Hermes turn identity. Duplicate invocations for
the same most-recent identity are ignored; native Hermes retains that turn's
context. The native verification exercises first, next and post-compaction turns.

## Verification

```bash
python -m pytest tests/test_hermes_hook.py tests/test_hermes_context_bridge.py -q
UNIVERSAL_DOCS_HERMES_NATIVE_TESTS=1 \
  UNIVERSAL_DOCS_HERMES_PYTHON=/absolute/path/to/hermes/python \
  UNIVERSAL_DOCS_HERMES_HOST_EVIDENCE_DIR=/absolute/path/to/evidence \
  python -m pytest tests/test_hermes_native_hook.py -q
```

The opt-in proof uses an isolated temporary Hermes home and a **fixture** context
CLI through installed Hermes lifecycle/message assembly. It does not invoke a
paid model or modify the active profile. A live-core integration proof is separate.
