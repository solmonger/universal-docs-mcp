# Slice 01 — Lockfile & Installed-Version Resolution

Wave: 1 (parallelizable)
Depends on: none (`feat/proactive-docs-v2` base)
Owned files:
- `src/universal_docs_mcp/lockfile.py`
- `src/universal_docs_mcp/contracts.py` (only if `DependencyPin` gains optional `resolution_kind` — preserve backwards compatibility)
- `tests/test_lockfile_resolution.py` (new)
- Existing tests in `tests/test_manifest_*.py` must stay green unchanged.

## Contract Unlocked

Turn real-world range declarations (`pydantic>=2.0`, `next: "^15.1.0"`, `tokio = "1.40"`) into exact, verifiable package versions by reading project lockfiles and locally installed package metadata — without executing a single line of target code or spawning a package manager.

## Problem in Current Code

`lockfile.py` only parses `requirements*.txt`, `constraints*.txt`, `pyproject.toml`, `package.json`, `Cargo.toml`, and `Cargo.lock`.
- Most real Python projects declare ranges in `pyproject.toml` and pin exact versions in `uv.lock`, `poetry.lock`, or `.venv/lib/python*/site-packages/*.dist-info/METADATA`.
- Most JS/TS projects declare `^`/`~` ranges in `package.json` and pin exact versions in `package-lock.json`, `pnpm-lock.yaml`, or `node_modules/<pkg>/package.json`.
- Because those sources are unreadable, `select_current_package()` abstains with `no_exact_registry_candidate` or `unsupported_manifest`.

## API Seam (`src/universal_docs_mcp/lockfile.py`)

Preserve every existing public function and field (`Pin`, `manifest_kind`, `read_pins`, `parse_*`). Add:

1. **New lockfile parsers (registered in `_PARSERS` and `manifest_kind()`):**
   - `parse_uv_lock(text: str, source: str) -> list[Pin]`
     Parse `[[package]]` entries from `uv.lock` (TOML). Mark `registry_lookup=True` only when `source` indicates PyPI (`source.registry` is public PyPI URL `https://pypi.org/simple` or `https://pypi.org/simple/`). Mark editable/local/git/custom-index entries `registry_lookup=False` (redacted).
   - `parse_poetry_lock(text: str, source: str) -> list[Pin]`
     Parse `[[package]]` entries from `poetry.lock` (TOML). `registry_lookup=True` when `source` is absent (default PyPI) or `source.type == "legacy"` is absent; non-PyPI `source` (`git`, `directory`, `file`, `url`, private repo) sets `registry_lookup=False`.
   - `parse_package_lock_json(text: str, source: str) -> list[Pin]`
     Parse npm `package-lock.json` (lockfileVersion 2 & 3 `packages` map, plus fallback to v1 `dependencies`). Extract exact semver `version` for `node_modules/<name>` and scoped `node_modules/@scope/name`. Mark `registry_lookup=True` only when `resolved` is absent or starts with `https://registry.npmjs.org/` and `link` is not true.
   - `parse_pnpm_lock_yaml(text: str, source: str) -> list[Pin]`
     **No PyYAML dependency** (keep runtime deps zero-addition). Implement a strict, bounded line parser for `pnpm-lock.yaml` v6/v9 `importers` + `packages` / `snapshots` keys (`/<name>@<semver>` or `'<name>@<semver>'` or `<name>@<semver>:`) extracting exact public semver versions; ignore non-semver/tarball/git/link specifiers (`registry_lookup=False`).

2. **Installed-environment metadata reader (read-only, zero execution):**
   ```python
   def read_installed_pins(
       root: Path,
       *,
       ecosystems: tuple[str, ...] = ("python", "javascript"),
       max_packages: int = 512,
   ) -> list[Pin]:
   ```
   - **Python:** Check only fixed relative venv directories inside `root`: `.venv`, `venv`. Walk `lib/python*/site-packages/*.dist-info/METADATA` (descriptor-relative, `O_NOFOLLOW`, max 64 KiB per `METADATA` file, max `max_packages` entries). Parse RFC-822 header lines `Name:` and `Version:` only (stop at blank line). Ignore editable installs whose `direct_url.json` (if present in the same `.dist-info`) specifies `url` with `file://` or `vcs_info` (`registry_lookup=False`). Set `source="venv_metadata"`.
   - **JavaScript:** Check `node_modules` directly inside `root` (top-level packages and `@scope/*` packages only — no recursive walk). Read `package.json` via `O_NOFOLLOW` (max 64 KiB). Extract `name` and exact semver `version`. Skip symlinks (e.g., workspace links). Set `source="node_modules"`.

3. **Unified project dependency resolver:**
   ```python
   @dataclass(frozen=True)
   class ResolvedDependency:
       name: str
       ecosystem: str
       declared_spec: str | None
       resolved_version: str | None
       resolution_source: str  # e.g. "uv.lock", "pyproject.toml", "venv_metadata", "package-lock.json", "Cargo.lock"
       registry_lookup: bool
       direct: bool            # True if declared in top-level manifest

   def resolve_project_dependencies(root: Path) -> list[ResolvedDependency]:
   ```
   - Precedence per ecosystem for resolving a declared range to an exact version:
     1. Exact pin in manifest (`pyproject.toml`, `requirements.txt`, `package.json`, `Cargo.toml`)
     2. Lockfile (`uv.lock`, `poetry.lock`, `package-lock.json`, `pnpm-lock.yaml`, `Cargo.lock`)
     3. Installed environment metadata (`venv_metadata`, `node_modules`)
   - **Multi-manifest resolution:** If both `pyproject.toml` and `requirements.txt` exist, combine direct declarations; if they disagree on an exact pin for the same package, keep `resolved_version=None` (explicit conflict — never guess).
   - **Never inflate transitive deps as direct:** Lockfiles have hundreds of transitive packages; mark `direct=True` only when the package appears in a top-level manifest (`pyproject.toml`, `requirements*.txt`, `package.json`, `Cargo.toml`), or when no top-level manifest exists and only a lockfile/venv exists (mark `direct=False`).
   - Raise `ValueError("unreadable_project_root")` if `root` does not exist or is unreadable.

## Bounds & Safety

- Raise `MAX_LOCKFILE_BYTES = 8 * 1024 * 1024` specifically for lockfiles (`uv.lock`, `poetry.lock`, `package-lock.json`, `pnpm-lock.yaml`, `Cargo.lock`), while keeping `MAX_MANIFEST_BYTES = 1024 * 1024` for manifests.
- Every file open inside `root` uses descriptor-relative `O_NOFOLLOW` (`_read_scoped` / `dir_fd`), rejecting symlinks, `..`, FIFOs, and directories.
- Non-registry URLs/paths/tokens remain redacted (`spec="[redacted]"`, `spec_redacted=True`, `registry_lookup=False`).
- Never raise unhandled parser exceptions from `resolve_project_dependencies` for one malformed optional file if another valid source exists; if a lockfile is malformed, skip it or record cleanly without crashing. (Direct `read_pins()` on a malformed file still raises `ValueError` as existing tests require.)

## Human / Checker Playable Verification

```bash
env -u PYTHONPATH -u VIRTUAL_ENV .venv/bin/python -c '
from pathlib import Path
from universal_docs_mcp.lockfile import resolve_project_dependencies
deps = resolve_project_dependencies(Path("."))
print(f"Resolved {len(deps)} deps; direct={sum(d.direct for d in deps)}")
for d in deps[:10]:
    print(" ", d)
'
```
Running this against the `universal-docs-mcp` repo itself must resolve `mcp`, `httpx`, `pydantic`, `packaging` from `pyproject.toml` + `.venv` metadata to their exact installed versions!

## RED→GREEN Test Cases (`tests/test_lockfile_resolution.py`)

Write failing tests first for every case:
1. `uv.lock`: extracts exact PyPI pins, redacts git/path/private-registry entries.
2. `poetry.lock`: extracts exact PyPI pins, redacts git/directory/private sources.
3. `package-lock.json` (v2/v3 + scoped `@types/node`): extracts exact npm versions, redacts `link: true` and non-npm URLs.
4. `pnpm-lock.yaml` (v6 and v9 formats, scoped packages): extracts exact versions without PyYAML, rejects git/file links.
5. `read_installed_pins` (Python `.venv` `METADATA`): reads `Name`/`Version`, skips editable `direct_url.json` (`file://`/`vcs_info`), refuses symlinked `.venv` or symlinked `METADATA`, respects 64 KiB cap.
6. `read_installed_pins` (JS `node_modules`): reads regular and `@scope/pkg` `package.json`, skips symlinks.
7. `resolve_project_dependencies`:
   - `pyproject.toml` with `pydantic>=2.0` + `uv.lock` with `pydantic 2.9.2` -> `direct=True, resolved_version="2.9.2", resolution_source="uv.lock"`.
   - `pyproject.toml` with `httpx>=0.27` + `.venv` metadata `0.28.1` (no lockfile) -> `direct=True, resolved_version="0.28.1", resolution_source="venv_metadata"`.
   - `package.json` with `"next": "^15.1.0"` + `package-lock.json` with `15.1.6` -> `direct=True, resolved_version="15.1.6"`.
   - `Cargo.toml` with `serde = "1.0"` + `Cargo.lock` with `1.0.215` -> `direct=True, resolved_version="1.0.215"`.
   - Conflicting exact pins between `pyproject.toml` and `requirements.txt` -> `resolved_version=None` (fail closed).
   - Empty directory -> returns `[]` cleanly.
   - `get_project_dependencies` MCP tool still works on `uv.lock`, `poetry.lock`, `package-lock.json`, `pnpm-lock.yaml` via `read_pins()`.

## What Must Stay Green

- All existing `tests/test_manifest_*.py` and full offline suite (717+ tests).
- `ruff check`, `ruff format --check`, `git diff --check`.
