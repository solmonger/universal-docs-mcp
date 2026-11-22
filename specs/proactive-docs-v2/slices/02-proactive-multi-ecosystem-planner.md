# Slice 02 — Multi-Ecosystem, Multi-Package & Error-Aware Proactive Planner

Wave: 2 (starts after Slices 01, 03, and 04 are integrated into `feat/proactive-docs-v2`)
Depends on: Slice 01 (`lockfile.resolve_project_dependencies`), Slice 03 (query token compatibility), Slice 04 (`local_stubs` awareness)
Owned files:
- `src/universal_docs_mcp/planner.py`
- `src/universal_docs_mcp/product_cli.py` (optional `--multi` / `plan-current` / `plan-error` subcommand wiring; keep existing `plan`, `init`, `doctor`, `rollback` contracts 100% green)
- `tests/test_proactive_planner.py` (new)
- Existing `tests/test_planner*.py` and `tests/test_task_aware_*.py` must stay green.

## Contract Unlocked

Make automatic package selection work on real multi-language, multi-dependency repos and runtime errors — instead of abstaining whenever a project uses ranges, JS/TS/Rust, or more than one package.

## Problems in Current Code (`src/universal_docs_mcp/planner.py`)

1. `select_current_package()` only accepts `pyproject.toml` OR `requirements.txt`, abstaining (`ambiguous_manifest`) if both exist and abstaining (`unsupported_manifest`) on `package.json`/`Cargo.toml`.
2. It requires exact `==` pins inside the manifest itself, ignoring lockfiles and `.venv`/`node_modules`.
3. It walks `os.walk(root)` alphabetically capped at 8 `.py` files, missing the file the agent is actually editing.
4. It abstains (`ambiguous_candidates`) whenever >1 dependency is imported or named in the task.
5. It has no error/traceback selector (`ImportError`, `AttributeError`, `TypeError` → package + symbol).
6. `to_preflight_request()` hardcodes `freshness_mode="require_check"`, forcing a slow live check on every turn even for immutable exact versions.

## API Seam (`src/universal_docs_mcp/planner.py`)

Keep `Plan`, `plan_dependency_changes()`, `select_current_package()`, and `to_preflight_request()` backward-compatible for existing callers/tests. Add:

1. **Multi-target bundle type:**
   ```python
   PLAN_BUNDLE_SCHEMA = "universal-docs.plan-bundle/v1"

   @dataclass(frozen=True)
   class PlanBundle:
       status: Literal["selected", "abstained"]
       reason: str
       plans: tuple[Plan, ...]          # 1..3 selected plans, ordered by relevance
       resolution_root_has_manifest: bool

       def as_dict(self) -> dict[str, Any]: ...
   ```

2. **Multi-ecosystem, multi-package current selector:**
   ```python
   def select_current_packages(
       project_root: Path,
       *,
       task: str | None = None,
       active_files: Iterable[str | Path] = (),
       max_packages: int = 3,
   ) -> PlanBundle:
   ```
   - Uses `lockfile.resolve_project_dependencies(project_root)` (from Slice 01) so Python (`pyproject.toml` + `uv.lock`/`poetry.lock`/`.venv`), JS/TS (`package.json` + `package-lock.json`/`pnpm-lock.yaml`/`node_modules`), and Rust (`Cargo.toml` + `Cargo.lock`) all resolve exact versions automatically.
   - **File discovery priority (bounded to 12 source files max, 32 KiB each, 256 KiB total):**
     1. Explicitly passed `active_files` (validated project-relative, `O_NOFOLLOW`, `.py`, `.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.rs`).
     2. Recently modified source files in `root` (bounded `os.walk`, max 64 dirs, skip `.git`/`.venv`/`venv`/`node_modules`/`target`/`dist`/`build`/`__pycache__`, sorted by `(mtime_ns desc, relpath asc)` so actively edited files win over alphabetical `__init__.py`!).
   - **Lightweight multi-language import & symbol extraction (bounded, safe):**
     - Python: existing `ast` parser (keep strict safety; relax global crash-on-one-file so one unrelated file with a star import or dynamic import does not poison selection of another cleanly imported package).
     - JS/TS: bounded regex extraction of `import ... from "<pkg>"`, `require("<pkg>")`, and named imports (`import { Foo, bar } from "pkg"` -> package + symbols). Normalize scoped packages (`@scope/pkg/subpath` -> `@scope/pkg`) and bare packages (`pkg/subpath` -> `pkg`).
     - Rust: bounded regex extraction of `use <crate>::...` (mapping `_` <-> `-` against resolved Cargo crate names) and referenced path segments as symbols.
   - **Ranking & selection (up to `max_packages`, clamped 1..3):**
     Score each exact-resolved direct dependency (plus any non-direct locked dep explicitly named in `task` or imported in `active_files`):
     - +10 if package name (or normalized import name) is explicitly named in `task`
     - +6 if imported in `active_files`
     - +4 if imported in discovered recently-modified source files
     - +2 per symbol referenced in source that also appears in `task`
     Select top `1..max_packages` with score > 0. Abstain cleanly with explicit reason (`no_manifest_found`, `unreadable_project_root`, `no_exact_registry_candidate`, `task_package_not_locally_proven`) when none qualify.
   - Update `select_current_package()` to remain 100% compatible with existing single-package tests while benefiting from Slice 01 lockfile/`.venv`/JS/Rust resolution when unambiguous (preserve exact abstention tokens expected by existing tests in `tests/test_always_on_consumption.py`).

3. **Traceback / error-driven selector:**
   ```python
   def select_for_error(
       project_root: Path,
       *,
       error_text: str,
       active_files: Iterable[str | Path] = (),
       max_packages: int = 2,
   ) -> PlanBundle:
   ```
   - Bounded `error_text` (max 16 KiB, strip control chars).
   - Extracts:
     - Error class (`ImportError`, `ModuleNotFoundError`, `AttributeError`, `TypeError`, `DeprecationWarning`, `ValidationError`, Rust `error[E0...]`, JS `TypeError`/`Cannot find module`/`ERR_MODULE_NOT_FOUND`).
     - Mentioned package/module names (from `No module named 'X'`, `cannot import name 'Y' from 'X'`, `Cannot find module 'X'`, `site-packages/<pkg>/`, `node_modules/<pkg>/`, `has no attribute 'Y'`, `unexpected keyword argument 'Y'`).
     - Referenced symbols (`Y`).
   - Matches ONLY against packages proven in `resolve_project_dependencies(project_root)` (never invents an unverified package or version from arbitrary error text).
   - Builds a focused query containing the package, exact version, error class, and extracted symbol names (e.g., `"pydantic 2.9.2 AttributeError model_validator validator"`).

4. **Freshness default in `to_preflight_request()`:**
   ```python
   def to_preflight_request(
       plan: Plan,
       *,
       freshness_mode: Literal["require_check", "allow_cache", "allow_stale"] = "allow_cache",
       context_max_bytes: int = 4096,
   ) -> PreflightRequest:
   ```
   - Exact pinned versions from a registry are immutable once published. Default `freshness_mode` to `"allow_cache"` (while allowing callers to pass `"require_check"` explicitly when desired; check if any existing test asserts `"require_check"` on `to_preflight_request(plan)` — if so, add an explicit parameter and keep `plan_dependency_changes` contract or update cleanly with clear rationale noted in report).
   - **Check existing tests first!** Look at `tests/test_planner*.py` and `tests/test_task_aware_*.py` before changing the default of `to_preflight_request`. If existing tests pin `freshness_mode == "require_check"` on `to_preflight_request(plan)`, keep `to_preflight_request`'s default `"require_check"` and add `freshness_mode="allow_cache"` on `to_preflight_requests(bundle, total_budget_bytes=6144, freshness_mode="allow_cache")` so Slice 06 hooks get fast caching without breaking any Slice 04 contract test!

5. **Budget splitter helper:**
   ```python
   def to_preflight_requests(
       bundle: PlanBundle,
       *,
       total_budget_bytes: int = 6144,
       freshness_mode: Literal["require_check", "allow_cache", "allow_stale"] = "allow_cache",
   ) -> list[PreflightRequest]:
   ```
   Splits `total_budget_bytes` proportionally (minimum 1500 bytes per selected package, total <= `total_budget_bytes`).

## Human / Checker Playable Verification

```bash
env -u PYTHONPATH -u VIRTUAL_ENV .venv/bin/python -c '
from pathlib import Path
from universal_docs_mcp.planner import select_current_packages, select_for_error
print(select_current_packages(Path("."), task="fix pydantic ValidationError and httpx timeout").as_dict())
print(select_for_error(Path("."), error_text="ImportError: cannot import name McpError from mcp.shared.exceptions").as_dict())
'
```
Both calls must select exact installed versions (`pydantic`, `httpx`, `mcp`) with focused queries on the repo itself!

## RED→GREEN Test Cases (`tests/test_proactive_planner.py`)

1. Python project with `pyproject.toml` range (`pydantic>=2.0`, `httpx>=0.27`) + `uv.lock`: selects both `pydantic` and `httpx` when both are imported/mentioned, with exact locked versions and split byte budgets.
2. JS/TS project with `package.json` (`^` ranges) + `package-lock.json` + `.ts` file importing `zod` and `next/navigation`: selects exact versions and extracts imported symbols (`z`, `useRouter`).
3. Rust project with `Cargo.toml` + `Cargo.lock` + `src/main.rs` (`use serde::Deserialize;`): selects `serde` at exact `Cargo.lock` version with symbol `Deserialize`.
4. Recently modified file wins over alphabetical `__init__.py` when >12 source files exist.
5. `select_for_error` on Python `ImportError: cannot import name 'validator' from 'pydantic'` selects `pydantic` with query containing `validator` and `ImportError`.
6. `select_for_error` on JS `TypeError: ...` referencing `node_modules/zod/` selects `zod`.
7. `select_for_error` on an error mentioning an unpinned/unknown package abstains cleanly (never fabricates a package).
8. `to_preflight_requests` enforces `sum(req.context_max_bytes) <= total_budget_bytes` and defaults to `allow_cache`.
9. Existing `tests/test_always_on_consumption.py` and `tests/test_planner*.py` stay 100% green.

## What Must Stay Green

- Full offline suite + `ruff check` + `ruff format --check` + `git diff --check`.
