# Proactive Fresh Documentation v2 (`proactive-docs-v2`)

Status: spec frozen; ready for Wave 1 maker execution
Last updated: 2026-11-22
Base commit: `f5ece32` (`main`)
Branch: `feat/proactive-docs-v2`
Role split: **Checker/Orchestrator** = attended frontier model (Hermes); **Makers** = Codex CLI workers in isolated git worktrees (one slice per worktree, never self-reviewing).

## Next Agent Prompt

You are implementing **one assigned slice** of `specs/proactive-docs-v2/`.

1. Read this `README.md` and your assigned slice file under `specs/proactive-docs-v2/slices/`.
2. Read every source file named in your slice before editing anything.
3. Follow RED→GREEN: write failing behavior tests first, verify they fail for the right reason, then implement the production change.
4. Stay strictly inside your slice's owned files. Do not modify another slice's files, activate user profiles, publish packages, merge to `main`, or weaken any security boundary.
5. Run your slice's focused tests, the full offline suite, `ruff check`, `ruff format --check`, and `git diff --check` using the exact scrubbed-environment commands below.
6. Commit cleanly on your worktree branch and return the exact report requested in **Maker Report Contract**. Do not update this `README.md` TODO checklist — the checker updates it after verifying and integrating your commit.

### Global TODO

- [x] Slice 01 — Lockfile & installed-version resolution (`slices/01-lockfile-and-installed-versions.md`) [Wave 1 — integrated + checker-repaired]
- [x] Slice 03 — Unified BM25 query search & MCP tool upgrades (`slices/03-bm25-search-and-mcp-upgrades.md`) [Wave 1 — integrated + checker-repaired]
- [ ] Slice 04 — Richer exact-version sources: `v`-tags, changelogs, local stubs, expanded catalog (`slices/04-richer-exact-version-sources.md`) [Wave 1B — running]
- [ ] Slice 05 — Fast freshness: immutable exact-version cache & HTTP ETag (`slices/05-fast-freshness-and-etag.md`) [Wave 1B — running]
- [ ] Slice 02 — Multi-ecosystem, multi-package & error-aware proactive planner (`slices/02-proactive-multi-ecosystem-planner.md`) [Wave 2; depends on 01, 03, 04]
- [ ] Slice 06 — Proactive harness hooks: Hermes + Claude/Codex dynamic mode + post-tool error trigger (`slices/06-proactive-harness-hooks.md`) [Wave 3; depends on 02, 04, 05]

---

## Problem Statement (Measured Evidence)

Universal Docs MCP has a strong safety and provenance core, but proactive fetching fails on real coding sessions for five measured reasons:

1. **0.24% real-world hook hit rate:** Live receipts in `~/.hermes/receipts/universal-docs/` record **1 retrieval in 421 runs** (378 `no_manifest_found`, 42 `unsupported_manifest`). Real projects declare ranges (`>=`, `^`) in `pyproject.toml`/`package.json`, lock exact versions in lockfiles or `.venv`/`node_modules`, and use JS/TS/Rust as well as Python.
2. **Brittle trigger and single-package abstention:** `hermes_hook.py` skips any prompt missing 14 hardcoded words, scans only the first 8 alphabetical `.py` files, and abstains whenever >1 pinned dependency is used. `command_hook.py` (Claude/Codex) only runs a fixed static `request.json`.
3. **README-only coverage misses real APIs:** Gate A (`benchmarks/version_guard/results/2026-09-10-gate-a-five-case/REPORT.md`) proved exact-version docs doubled pass rate (4/10 → 8/10) **only** when breaking symbols were present; registry READMEs missed Pydantic v2 validators and SQLAlchemy 2.0 `select`. GitHub fallback also fails on `v{version}` tags, and `source_catalog.py` holds only 1 hardcoded entry.
4. **Clunky MCP pull surface and substring search:** Agents need 2–3 calls (`get_project_dependencies` → `get_docs_outline` → `get_package_docs`), `get_package_docs` has no `query` search, `preflight.py` uses raw substring `.count()`, and `get_project_dependencies` breaks across repos because it requires one static `UNIVERSAL_DOCS_PROJECT_ROOT`.
5. **High repeat-turn latency:** `planner.to_preflight_request()` hardcodes `freshness_mode="require_check"`, forcing a live network round-trip even for immutable exact releases.

---

## Target End-State & Single-Owner Architecture

Every concept has **one** owner. No slice may bolt on a parallel helper or duplicate parser/ranker/validator.

| Concept | Single Owner | Architectural Rule |
|---|---|---|
| Manifests, lockfiles, installed metadata | `src/universal_docs_mcp/lockfile.py` | Reads manifests, lockfiles (`uv.lock`, `poetry.lock`, `package-lock.json`, `pnpm-lock.yaml`, `Cargo.lock`), and `.venv/.../*.dist-info/METADATA` + `node_modules/*/package.json`. Descriptor-relative `O_NOFOLLOW`, zero code execution, strict byte caps. |
| Package & error selection | `src/universal_docs_mcp/planner.py` | Owns `plan_dependency_changes()`, `select_current_package()`, `select_current_packages()` (up to 3 targets), and `select_for_error()`. Supports Python, JS/TS, Rust. Never fetches network data. |
| Section parsing & BM25 ranking | `src/universal_docs_mcp/compaction.py` | Owns `parse_sections()`, `rank_sections()` (word-boundary BM25 + identifier splitting), and `compact()`. Shared by `preflight.py` and `server.py`; removes duplicate substring scorer from `preflight.py`. |
| Upstream & local doc sources | `docs_fetcher.py`, `local_stubs.py`, `source_catalog.py`, `official_sources.py`, `source_identity.py` | Exact-version README + `v{version}` tag fallback + release/changelog notes + read-only local `.pyi`/`.d.ts` stubs + JSON-backed official docs catalog (`llms.txt` / versioned markdown). |
| Cache & conditional HTTP | `cache.py`, `network.py`, `retrieval.py` | Stores `etag`/`last_modified` per cache row; sends `If-None-Match` on refresh and promotes `304 Not Modified` to fresh `upstream_checked` without re-downloading. |
| MCP server surface | `server.py`, `validation.py`, `contracts.py` | Adds `query` to `get_package_docs`, adds `get_official_docs` and `get_version_diff`, supports MCP `roots/list` fallback. Every tool enforces strict `outputSchema`. |
| Harness delivery & hooks | `context_delivery.py`, `command_hook.py`, `examples/hermes/universal-docs-preflight/hermes_hook.py` | Validates receipts and formats bounded packets (including multi-package bundles). Hermes adds dep-name + traceback triggers and `post_tool_call` error hook; Claude/Codex gain `--dynamic` repo-aware mode. |

---

## Slice Dependency Graph

```text
Wave 1 (parallel worktrees, zero file overlap):
  [01 lockfile.py]   [03 compaction/server/preflight-rank]   [04 docs_fetcher/stubs/catalog]   [05 cache/network/retrieval]
         │                             │                                    │                                │
         └──────────────┬──────────────┴────────────────────────────────────┤                                │
                        ▼                                                   │                                │
Wave 2:          [02 planner.py + product_cli.py]                           │                                │
                        │                                                   │                                │
                        └───────────────────────────┬───────────────────────┴────────────────────────────────┘
                                                    ▼
Wave 3:                    [06 hermes_hook.py + command_hook.py + context_delivery.py]
```

*Note on plan drafting:* This spec was authored directly by the checker after full codebase inspection and live receipt forensics (`~/.hermes/receipts/universal-docs/`), followed by a single-owner architecture pass; no multi-vendor parallel spec draft was run because the operator assigned the checker role directly.

---

## Sacred Security & Correctness Invariants (Never Weaken)

1. **Zero code execution against target projects:** Never `import` target modules or spawn `pip`/`npm`/`cargo`/`uv`. Read lockfiles, `METADATA`, `package.json`, `.pyi`, and `.d.ts` as plain files only.
2. **POSIX confined reads:** Every local file read under `project_root` uses descriptor-relative `os.open(..., dir_fd=..., O_NOFOLLOW)` rejecting symlinks, `..` escapes, special files, and oversized files.
3. **Closed network allowlist:** Generic package fetches stay restricted to `pypi.org`, `registry.npmjs.org`, `crates.io`, and `api.github.com`. Official docs/llms.txt hosts live only in the reviewed `source_catalog.json` and enter exclusively through `get_catalog_response()`. No arbitrary URL fetcher.
4. **Untrusted-data framing:** Every upstream doc and local stub is explicitly labeled untrusted data, never agent instructions.
5. **Exact version truth:** Declared ranges (`>=2.0`, `^1.4`) are never reported as exact pins. Exact versions come only from explicit pins, lockfiles, or installed metadata, with `resolution_source` recording which one proved the version.
6. **Strict schemas & no reflection:** Every MCP tool validates outbound payloads against `RESULT_TYPES` before SDK handling; invalid inputs never echo raw client strings or credentials in errors or logs.
7. **Offline-by-default tests:** The default `pytest` suite blocks all network sockets. Live tests stay `@pytest.mark.live` and opt-in (`UNIVERSAL_DOCS_LIVE_TESTS=1`).
8. **Profile opt-out boundary:** Restricted Hermes profiles (including `frenchbot`) stay excluded from hook activation.

---

## Exact Verification Commands

Always scrub outer `VIRTUAL_ENV` and pin `PYTHONPATH="$PWD/src"` so (a) Hermes's outer Python/MCP v2 install cannot shadow the repo's `.venv` (`mcp>=1.26,<2`), and (b) isolated git worktrees test their own `src/` instead of the main checkout's editable install:

```bash
PY=/Users/operator/Desktop/research/universal-docs-mcp/.venv/bin/python

# Focused slice tests
env -u VIRTUAL_ENV PYTHONPATH="$PWD/src" "$PY" -m pytest <test_files> -q

# Full offline suite (baseline on f5ece32: 717 passed, 12 skipped)
env -u VIRTUAL_ENV PYTHONPATH="$PWD/src" "$PY" -m pytest -q

# Lint, format, whitespace
env -u VIRTUAL_ENV PYTHONPATH="$PWD/src" "$PY" -m ruff check src tests scripts examples
env -u VIRTUAL_ENV PYTHONPATH="$PWD/src" "$PY" -m ruff format --check src tests scripts examples
git diff --check
```

---

## Checker / Maker Execution Protocol

### Worktree & Codex Launch Pattern (for Checker)

1. **Route probe first:** Before launching makers, verify the Codex model slug with:
   `codex exec -m <slug> -s read-only "No tools. Reply exactly ROUTE_OK."`
2. **One worktree per slice:**
   `git worktree add -b feat/proactive-docs-s<NN> ~/.hermes/cache/scratch/ud-s<NN> feat/proactive-docs-v2`
3. **Symlink `.venv` or reuse repo venv** via absolute path `/Users/operator/Desktop/research/universal-docs-mcp/.venv/bin/python` (never mutate the main repo `.venv`).
4. **Run Codex non-interactively:**
   ```bash
   codex exec -m <slug> -c 'model_reasoning_effort="medium"' \
     -s workspace-write \
     -C "$(python3 -c 'from pathlib import Path; print(Path("<worktree>").resolve(strict=True))')" \
     -o ~/.hermes/cache/scratch/ud-s<NN>-report.txt \
     "Read specs/proactive-docs-v2/README.md and specs/proactive-docs-v2/slices/<slice>.md. Read every referenced source file first. Implement RED->GREEN, run all required verification commands, commit on your branch, and emit the Maker Report Contract." \
     > ~/.hermes/cache/scratch/ud-s<NN>.log 2>&1
   ```
5. **Checker acceptance gate (per slice):**
   - Inspect full `git show <commit>` — reject out-of-scope edits or weakened invariants.
   - Re-run focused tests, full offline suite, `ruff check`, `ruff format --check`, and `git diff --check` first-hand (maker report is a self-report, never proof).
   - Integrate slice into `feat/proactive-docs-v2` (one at a time), re-run full suite, and check off the TODO item in this `README.md`.
   - After Wave 3 integrates, build a fresh wheel (`.venv/bin/python -m build --no-isolation`), run live smoke + canary verification, and run an independent review before merging to `main`.

### Maker Report Contract

Every Codex maker's final message must contain:
1. `COMMIT`: exact commit SHA and branch name.
2. `FILES_CHANGED`: list of modified/added files.
3. `RED_EVIDENCE`: exact failing test command + failure summary observed before production edits.
4. `GREEN_EVIDENCE`: exact passing commands + pass/skip counts for focused tests, full offline suite, `ruff check`, `ruff format --check`, and `git diff --check`.
5. `INVARIANTS_CHECKED`: confirmation of security/bounds rules touched by the slice.
6. `OPEN_RISKS`: any edge cases or follow-ups for the checker.
