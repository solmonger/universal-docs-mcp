# Task-Aware Documentation Selection — Slice 1

Status: frozen implementation contract
Base: `570417c34acf82de91f41707f22e2899fc6d25e7`
Branch: `feat/task-aware-doc-selection`
Scope: Universal Docs product library and `universal-docs plan` CLI only

## Product problem

The current dependency-change planner always emits:

`migration upgrade breaking changes quick start`

That identifies a package/version but not the documentation section needed by the agent's actual code. In the Rich 13.7.1 efficacy replay, the exact-version packet therefore omitted the behavior required by the oracle. Checker-curated context passed, so retrieval mechanics were not the binding failure; selection was.

## User-visible behavior

Add an optional source-backed task-signal mode to `universal-docs plan` and the underlying planner API.

Inputs:

- zero or one bounded task string;
- one or more explicit project-relative Python source paths;
- the existing before/after manifest pair and optional package.

When source-backed mode is requested, the planner must:

1. read only explicitly supplied regular `.py` files inside `project_root`;
2. parse them with Python AST locally;
3. find imports attributable to the selected package and package-relevant referenced symbols/call keywords;
4. create a deterministic query containing the exact package/version migration intent plus bounded task and symbol terms;
5. expose bounded provenance explaining that source-backed symbols were used;
6. feed that exact query into `to_preflight_request`.

If task signal is absent, invalid, ambiguous, or unrelated to the selected package, abstain. Never fall back to the generic migration query after the caller explicitly requested source-backed mode.

If no task/source mode is requested, preserve the existing generic query and all existing planner behavior for compatibility.

## Proposed public seam

Library:

```python
plan_dependency_changes(
    before,
    after,
    *,
    project_root,
    package=None,
    task=None,
    source_paths=(),
) -> Plan
```

CLI:

```text
universal-docs plan ... [--task TEXT] [--source RELATIVE.py ...]
```

`--source` is repeatable. Duplicate scalar options remain rejected.

The exact field names may be adjusted only if existing project conventions require it, but the behavior and fail-closed contract may not be weakened.

## Bounds and safety

- At most 8 source files.
- At most 32 KiB per source file and 128 KiB total.
- Python `.py` files only.
- Project-relative paths only; reject absolute paths, escapes, symlinks, directories, FIFOs, devices, sockets, and read races.
- Task text: at most 512 UTF-8 bytes after normalization; reject NUL/control characters.
- Final query: non-empty, deterministic, at most 512 characters.
- At most 24 emitted symbol terms; each term bounded and identifier-shaped.
- Do not emit absolute paths, raw source, comments, strings, or credentials in the plan receipt.
- Dynamic imports, star imports for the selected package, syntax errors, unsupported package-to-import identity, and conflicting aliases cause explicit abstention.
- Distribution-to-import matching is conservative in this slice: PEP 503 package name with hyphens mapped to underscores plus an exact top-level import match. Do not invent a global alias database.

## Receipt contract

A selected source-backed plan must preserve existing fields and add bounded machine-readable selection provenance sufficient to answer:

- whether generic or source-backed selection was used;
- how many source files were inspected;
- which normalized package-relevant symbols were selected;
- whether task text contributed to the query.

An abstention returns `query: null`, no symbols, and one finite reason from the product-owned vocabulary. Required new reasons include equivalents of:

- `task_signal_source_required`;
- `task_signal_not_found`;
- `task_signal_ambiguous`;
- `task_signal_invalid`.

Do not expose exception text.

## RED→GREEN acceptance cases

Write and run each failing behavior test before production code.

1. `from rich.console import Console`; `Console(highlight=False)` plus a bounded task produces a selected Rich plan whose query contains `rich`, `Console`, and `highlight`, and whose preflight request carries the same query.
2. `import rich as r`; `r.print(..., markup=False)` records package-relevant `print`, `markup`, and alias provenance deterministically.
3. A source file unrelated to the selected dependency abstains with `task_signal_not_found`; the generic query is absent.
4. Task-only source-backed invocation abstains with `task_signal_source_required`.
5. Selected-package star import, conflicting aliases, or dynamic import usage abstains as ambiguous.
6. Syntax error, absolute/escaping/symlink/special/oversized/non-Python source, too many files, or invalid task abstains as invalid without leaking host paths.
7. Reordered equivalent source inputs produce byte-equivalent plan payloads.
8. Symbol count and query length are bounded under adversarial source.
9. CLI `--task`/repeated `--source` reaches the library seam and emits the same contract.
10. Existing no-source callers preserve the current generic query and existing test behavior.

## Verification

Maker:

- focused planner and product-CLI tests;
- `python -m ruff check` for changed Python files;
- `python -m ruff format --check` for changed Python files;
- `git diff --check`;
- commit the exact slice.

Checker:

- inspect full diff and RED evidence;
- rerun affected planner, CLI, init, context-delivery, and receipt tests with the repository's existing environment;
- run the full offline suite if focused checks pass;
- obtain an independent exact-object review before any integration into the protected candidate.

## Hard exclusions

- No benchmark-runner work.
- No paid/model/network calls.
- No Hermes changes.
- No active profile, MCP installation, or harness adapter activation.
- No selector model calls, embeddings, or model-generated task signals.
- No npm/Rust extension in this slice.
- No merge, push, publication, or rollout.
- Do not modify the protected checkout.
