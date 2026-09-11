# Slice 01 — benchmark harness

## Contract

Add a local benchmark runner that executes immutable task fixtures in isolated temporary workspaces, supports named control/manual/automatic arms, invokes an externally supplied agent command without storing credentials, runs the case oracle, and writes a reproducible JSON receipt.

## API seam

Own under `scripts/version_guard_benchmark.py` plus a small importable module if necessary. Input is a bounded manifest of cases and arm context files; output schema is `universal-docs.benchmark/v1` with case ID, arm, model/provider labels supplied by caller, generation identifier when supplied, context SHA-256/bytes, workspace diff hash, test command/status/output hash, elapsed times, and aggregate counts.

Never accept shell strings; command fields are argv arrays. Never copy environment wholesale. Never store prompt/model output secrets, hidden reasoning, absolute source paths, or context body in the aggregate receipt.

## Visible artifact

A dry fixture produces a JSON report and human-readable Markdown summary.

## Verification

Focused tests must cover fresh workspace per run, argv-only execution, timeout, missing executable, malformed cases, context hash binding, oracle pass/fail, no-context control, path/secret redaction, and deterministic aggregation. Run `ruff` on changed paths, `python -m compileall` on changed Python, and `git diff --check`.

## Stop rule

Stop and report instead of committing if the harness requires provider-specific SDKs, network access, shell execution, or modifications to production adapters.
