# Slice 05 — init and doctor

## Contract

Provide one explicit onboarding/diagnostic command for Claude Code and one Python project. `init` accepts explicit trusted before/after manifest paths plus optional `--package`, runs the accepted planner, and converts one selected plan into the existing fixed preflight/hook contract. It is a one-command migration guard for that dependency-change event, not continuous repository discovery; an abstained plan writes no hook. It may generate a proposed scoped configuration and apply it only with an explicit flag; dry-run is default. `doctor` reports installed artifact identity, module path, executable, cache scope, stdio discovery, source-bearing retrieval, harness seam status, and rollback instructions.

## API seam

Extend the single `product_cli.py` dispatcher with `init` and `doctor`; do not add competing entry points or change the existing server/preflight/context commands. `init --harness claude-code` requires an absolute project root, project-relative bounded regular before/after manifests, and executable paths from the current installation. A selected plan becomes a fixed `adapter.json` request plus one project-local `.claude/settings.json` `UserPromptSubmit` hook. Configuration edits preserve unrelated fields, reject duplicate/conflicting Universal Docs hooks, use shell-safe absolute command paths, require `--apply`, write atomically, and create a byte-identical scoped backup before replacing an existing settings file. Dry-run writes no configuration. `doctor` consumes the generated paths explicitly; it never searches HOME or other profiles.

## Visible artifact

A clean isolated HOME dry run emits exactly one bounded `universal-docs.init/v1` JSON receipt and writes no project files. An opt-in fixture apply writes `.universal-docs/adapter.json` and changes exactly one Claude `UserPromptSubmit` stanza; read-back matches the expected delta and the backup hash matches the preimage. `doctor` emits exactly one bounded `universal-docs.doctor/v1` receipt with per-check `pass`/`fail`/`unknown`, source-bearing probe metadata, and an explicit rollback command; success requires every mandatory check to be `pass`.

## Verification

Test missing dependencies, wrong installed module, unreachable registry, malformed host config, existing stanza, rollback preimage, separate cache, and source-bearing live/fixture retrieval. A successful exit requires actual receipt content. Run a clean-install smoke outside the repository.

## Stop rule

Stop if root access, whole-file restore, trust bypass, or unrelated profile changes are needed.
