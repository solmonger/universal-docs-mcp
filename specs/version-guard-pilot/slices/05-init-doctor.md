# Slice 05 — init and doctor

## Contract

Provide one explicit onboarding/diagnostic command for one supported harness and Python project. It may generate a proposed scoped configuration and apply it only with an explicit flag; dry-run is default. It reports installed artifact identity, module path, executable, cache scope, stdio discovery, source-bearing retrieval, harness seam status, and rollback instructions.

## API seam

Add `universal-docs doctor`/`init` entry points or equivalent subcommands without changing existing entry-point behavior. Configuration edits use the host's supported writer where available, preserve unrelated fields, require absolute paths, and create a scoped backup. The command must never enable another profile, auto-top-up, telemetry, home-wide manifest access, or public networking.

## Visible artifact

A clean isolated HOME dry run prints a concise checklist and writes a structured receipt. An opt-in fixture apply changes exactly one harness stanza; read-back matches the expected single-stanza delta.

## Verification

Test missing dependencies, wrong installed module, unreachable registry, malformed host config, existing stanza, rollback preimage, separate cache, and source-bearing live/fixture retrieval. A successful exit requires actual receipt content. Run a clean-install smoke outside the repository.

## Stop rule

Stop if root access, whole-file restore, trust bypass, or unrelated profile changes are needed.
