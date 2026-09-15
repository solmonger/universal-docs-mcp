# Claude Code and Codex command hook

`universal-docs-command-hook` is one small adapter for both documented
`UserPromptSubmit` command-hook seams. It runs a trusted, fixed
`universal-docs-preflight` request and returns a bounded `additionalContext`
packet. The harness event is a trigger only: prompt text, `cwd`, transcript
paths, and any event command fields are ignored.

This is a source-tree candidate. It does not install a global hook or modify
any user profile.

## Install in an isolated environment

```sh
uv pip install --python .venv/bin/python --no-deps -e .
```

Use absolute paths in hook configuration. Copy and edit the isolated examples
under `examples/command-hooks/`:

- `request.json` is the fixed preflight request.
- `adapter.json` names the fixed preflight executable and request.
- `claude/settings.json` is a project-local Claude Code hook plus `.mcp.json`.
- `codex/hooks.json` is a Codex hook definition.

The adapter config is deliberately not a general plugin manifest:

```json
{
  "preflight_command": ["/absolute/path/to/.venv/bin/universal-docs-preflight"],
  "request": {
    "package": "requests",
    "ecosystem": "python",
    "selection": "requested",
    "requested_version": "2.32.3",
    "query": "timeouts retries",
    "section_ids": ["usage"],
    "context_max_bytes": 12000,
    "freshness_mode": "require_check",
    "deadline_ms": 25000
  },
  "timeout_ms": 25000
}
```

The request can instead be supplied as a bounded regular request file with
explicit arguments:

```sh
.venv/bin/universal-docs-command-hook \
  --harness claude \
  --request-file /absolute/path/to/request.json \
  --preflight-executable /absolute/path/to/.venv/bin/universal-docs-preflight
```

For explicit argv targets, use `--package`, `--ecosystem`, `--selection`,
`--requested-version` (only with `requested`), `--query`, `--section-id`,
`--context-max-bytes`, `--freshness-mode`, and `--deadline-ms` alongside
`--preflight-executable`.

## Neutral context boundary

Adapters that have their own lifecycle seam can invoke the same boundary
without nesting a command hook or spawning another preflight process:

```sh
.venv/bin/universal-docs-context \
  --request-file /absolute/trusted/request.json
```

The request file is one bounded absolute regular file. The CLI does not read
stdin, prompts, manifests, project configuration, or model settings. It calls
`preflight.parse_request` and the async `_run_cli` in-process, then applies the
same receipt validator and packet formatter used by the Claude/Codex adapter.
It emits exactly one compact JSON line:

```json
{"schema":"universal-docs.context/v1","status":"prepared","context":"...","error":null}
```

`context` is capped at 8 KiB and the complete line at 16 KiB. `prepared` and
`prepared_stale` exit 0; `unavailable` has an empty context, an enumerated
error, and exits 1. This boundary only delivers documentation data; it never
makes model calls or claims that a model consumed the packet.

## Host configuration

Claude Code project settings:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/.venv/bin/universal-docs-command-hook --harness claude --config /absolute/path/to/adapter.json",
            "timeout": 25
          }
        ]
      }
    ]
  }
}
```

Codex `hooks.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/.venv/bin/universal-docs-command-hook --harness codex --config /absolute/path/to/adapter.json",
            "timeout": 25,
            "additionalContextLimit": 2000
          }
        ]
      }
    ]
  }
}
```

Both hosts document the same `UserPromptSubmit` JSON shape, so the adapter
keeps separate named formatters while returning the host-specific event name
and `additionalContext` field explicitly. Do not set the Codex hook to
`async`: background hooks cannot block and are not a pre-model gate.

## Behavior and safety

On a valid preflight result, the adapter emits one JSON object containing a
packet with:

- package, ecosystem, selection, requested and selected versions;
- source kind and URL;
- freshness policy and state;
- deterministic `UDCTX:<content-hash-prefix>` receipt;
- clearly delimited, untrusted documentation data.

The packet says `prepared`; it never claims that the model used the context.
A stale cache result is labeled `prepared_stale`, not fresh. Installed-version
fields must remain unknown; `latest` is never treated as installed evidence.

The adapter blocks with a documented `{"decision":"block","reason":...}`
response for malformed hook input, missing/empty/no-match documentation,
nonzero or malformed preflight output, timeout, oversized stdout, invalid
receipts, and oversized adapter output. It does not reflect event data,
exception text, preflight stderr, or upstream content in a failure reason.

The fixed config/request and executable must be absolute regular files; final
symlinks and special files are rejected. The subprocess uses `shell=False`, a
new process group, bounded streaming stdout/stderr reads, a deadline, and an
allowlist environment containing process basics only. API keys, tokens,
secrets, and proxy variables are not passed to preflight. The event's supplied
command, `cwd`, and transcript path are never executed or opened. Queries stay
inside the local preflight request and are not sent by this adapter to a
separate service.

The isolated Codex consumer fixture uses
`--dangerously-bypass-hook-trust` only because that fixture deliberately tests
a vetted temporary hook configuration. Its evidence records that trust mode.
Do not use that bypass for production or for hooks that have not been reviewed.

## Verification

Unit tests cover the public adapter seam and negative paths:

```sh
.venv/bin/python -m pytest tests/test_command_hook.py -q
```

Actual-host tests are opt-in. They use temporary `HOME`, `CLAUDE_CONFIG_DIR`,
and `CODEX_HOME`, a fixture workspace, a fixture preflight executable, and a
loopback HTTP provider that captures the model request. The provider response
is labeled fixture output; no paid or live model request is made:

```sh
UNIVERSAL_DOCS_HOST_TESTS=1 .venv/bin/python -m pytest tests/test_command_hook_hosts.py -q
```

The test writes host receipts to
the private `cross-harness-design/command-hooks/` evidence directory
when `UNIVERSAL_DOCS_HOST_EVIDENCE_DIR` is set. A skipped or failed host test
is an unverified gate, never compatibility proof.
