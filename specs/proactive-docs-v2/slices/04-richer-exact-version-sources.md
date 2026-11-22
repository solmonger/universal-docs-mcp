# Slice 04 — Richer Exact-Version Sources (`v`-Tags, Changelogs, Local Stubs, Expanded Catalog)

Wave: 1 (parallelizable)
Depends on: none (`feat/proactive-docs-v2` base)
Owned files:
- `src/universal_docs_mcp/docs_fetcher.py`
- `src/universal_docs_mcp/local_stubs.py` (new)
- `src/universal_docs_mcp/source_catalog.py`
- `src/universal_docs_mcp/source_catalog.json` (new, loaded via `importlib.resources`)
- `src/universal_docs_mcp/official_sources.py`
- `src/universal_docs_mcp/source_identity.py`
- `tests/test_rich_sources.py` (new)
- Existing `tests/test_docs_contract.py`, `tests/test_official_*.py`, `tests/test_source_identity.py` must stay green.

## Contract Unlocked

Move beyond README-only coverage (which Gate A proved misses breaking migration symbols in Pydantic and SQLAlchemy) while keeping every source exact-version-bound, bounded in bytes, and confined to reviewed hosts or local project files.

## Problems in Current Code

1. `docs_fetcher.py` fetches GitHub README only at `?ref={version}` literally, missing `v{version}` tags used by most Python/JS/Rust repos.
2. There is no changelog or GitHub Release-notes fetcher for upgrades/migrations.
3. There is no local `.pyi` / `.d.ts` stub reader — even though exact installed type definitions sit right inside `.venv/` and `node_modules/` on disk.
4. `source_catalog.py` hardcodes only 1 entry (`("mcp-tools", "2026-07-28")`), with no `llms.txt` or framework docs entries.

## API Seam

### 1. GitHub `v`-tag fallback & exact-tag Release / Changelog (`docs_fetcher.py` + `source_identity.py`)

- In `docs_fetcher.py`:
  - Update `fetch_readme_from_github(repo_url, version)` (and `_github_readme_url`) to try `?ref={version}` first; if 404 and `version` does not already start with `"v"`, try `?ref=v{version}`. Never fall back to default branch (`main`/`master`). Return the **exact URL that succeeded** in `FetchedDocument.source_url`.
  - Add:
    ```python
    async def fetch_release_notes_from_github(
        repo_url: str,
        version: str,
    ) -> Optional[FetchedDocument]:
    ```
    Fetches `https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}` trying `version` then `v{version}` (same public-repo visibility check when `GITHUB_TOKEN` is set, max 512 KiB). Extracts release `name`, `tag_name`, `published_at`, and markdown `body`. Returns `FetchedDocument(content=..., source="github_release", source_url=exact_api_url)`.
  - Add:
    ```python
    async def fetch_version_diff(
        package: str,
        ecosystem: str,
        from_version: str,
        to_version: str,
        *,
        repo_url: Optional[str] = None,
    ) -> Optional[FetchedDocument]:
    ```
    Fetches release notes for `to_version` (and if `repo_url` is a public GitHub repo, lists bounded recent releases up to 10 entries between `from_version` and `to_version` using `/releases?per_page=20`, filtering strictly by parsed SemVer / PEP 440 version `from_version < v <= to_version`, never scraping arbitrary URLs). Formats concise version-ordered release notes capped at 256 KiB with `source="github_release_diff"`.

- In `source_identity.py`:
  - Update `_github_source_parts(source_url, version)` to accept BOTH `ref={quote(version, safe='')}` and `ref={quote('v' + version, safe='')}` (when `version` does not already start with `v`).
  - Update `package_source_matches()` to also validate `source="github_release"` against `https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}` (where `tag` is `version` or `v{version}`), still preserving `version_binding="unverified_git_ref"` unless proven by a registry artifact.

### 2. Local Installed Stub Reader (`src/universal_docs_mcp/local_stubs.py` — NEW)

```python
@dataclass(frozen=True)
class LocalStubExcerpt:
    package: str
    ecosystem: str
    version: str
    relative_path: str       # relative to project_root; never absolute
    content: str             # bounded signatures/docstrings only
    content_bytes: int
    source: Literal["local_stub"] = "local_stub"
    version_binding: Literal["local_installed_artifact"] = "local_installed_artifact"

def extract_local_stubs(
    project_root: Path,
    package: str,
    ecosystem: str,
    version: str,
    *,
    symbols: Iterable[str] = (),
    max_bytes: int = 4096,
) -> Optional[LocalStubExcerpt]:
```

Rules:
- **Zero code execution.** Never `import` the package.
- **Confined reads:** Descriptor-relative `os.open(..., dir_fd=..., O_NOFOLLOW)`. Reject symlinks, `..` escapes, special files. Cap at 8 files inspected, 64 KiB per file, `max_bytes` output.
- **Python:** Look inside `.venv/lib/python*/site-packages/` or `venv/lib/python*/site-packages/` for `<import_name>/` (or `<import_name>-stubs/`). Prefer `.pyi` files first, then `.py` files matching requested `symbols`. Parse with `ast.parse()` locally and emit **signatures + class/function/method headers + short docstrings** (first 3 lines of docstring) matching `symbols` (or public top-level exports from `__init__.pyi`/`__init__.py` when `symbols` is empty). Never dump full implementation bodies.
- **JavaScript/TypeScript:** Look inside `node_modules/<package>/` (including `@scope/pkg`) for `.d.ts` files (`types`/`typings` field from `package.json`, or `index.d.ts`). Extract matching `export interface` / `export type` / `export function` / `export class` / `export declare` blocks for `symbols`, bounded to `max_bytes`.
- Mark output clearly with `UNTRUSTED LOCAL STUB DATA` header, exact `version`, and `relative_path`.

### 3. Reviewed Official-Source Catalog (`source_catalog.json` + `source_catalog.py` + `official_sources.py`)

- Create `src/universal_docs_mcp/source_catalog.json` (loaded via `importlib.resources.files("universal_docs_mcp").joinpath("source_catalog.json")`) so catalog entries are data-driven, reviewed, and packaged inside wheels.
- Keep `("mcp-tools", "2026-07-28")` identical so all existing official-source tests pass untouched.
- Add reviewed `llms.txt` / official markdown entries for high-leverage coding frameworks (each pinned to an exact official HTTPS URL and explicit `content_types` allowlist, e.g. `text/markdown`, `text/plain`):
  - `("mcp-tools", "2026-07-28")` → `https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md`
  - `("mcp-tools", "2025-11-25")` → `https://modelcontextprotocol.io/specification/2025-11-25/server/tools.md`
  - `("pydantic", "v2-llms")` → `https://docs.pydantic.dev/latest/llms.txt`
  - `("fastapi", "llms")` → `https://fastapi.tiangolo.com/llms.txt`
  - `("httpx", "llms")` → `https://www.python-httpx.org/llms.txt` (or reviewed official markdown URL; add `list_sources() -> list[OfficialSource]` helper)
  - `("anthropic-sdk", "llms")` → `https://docs.anthropic.com/llms.txt`
  - `("openai-sdk", "llms")` → `https://platform.openai.com/llms.txt`
  - `("nextjs", "llms")` → `https://nextjs.org/docs/llms.txt`
  - `("hermes-agent", "llms")` → `https://hermes-agent.nousresearch.com/docs/llms.txt`
- Update `official_sources.py` to accept `text/markdown` AND `text/plain` (many `llms.txt` endpoints serve `text/plain; charset=utf-8`), still rejecting HTML (`text/html`), JSON, and non-UTF-8 payloads.
- Add `list_sources() -> tuple[OfficialSource, ...]` to `source_catalog.py` (immutable).
- Note: `network.py` stays untouched in this slice (`get_catalog_response` already calls `source_for(source_id, version)` and fetches `source.retrieval_url` via `_get_allowed_response`!). Make sure `source_catalog.json` rejects non-HTTPS URLs, credentials, or query strings at load time.

## Human / Checker Playable Verification

```bash
env -u PYTHONPATH -u VIRTUAL_ENV .venv/bin/python -c '
from pathlib import Path
from universal_docs_mcp.local_stubs import extract_local_stubs
from universal_docs_mcp.source_catalog import list_sources
print("Catalog count:", len(list_sources()))
stub = extract_local_stubs(Path("."), "pydantic", "python", "2.11", symbols=["BaseModel", "field_validator"])
print("Stub found:", stub is not None, "bytes:", stub.content_bytes if stub else 0)
if stub:
    print(stub.content[:500])
'
```
Must print local `pydantic` signatures extracted from `.venv` with zero imports and zero network calls!

## RED→GREEN Test Cases (`tests/test_rich_sources.py`)

1. `fetch_readme_from_github`: when `?ref=2.3.0` returns 404 and `?ref=v2.3.0` returns 200, succeeds with `source_url` containing `ref=v2.3.0`; `source_identity.package_source_matches()` accepts both `ref=2.3.0` and `ref=v2.3.0` and rejects `ref=main`.
2. `fetch_release_notes_from_github`: fetches exact tag `2.3.0` or `v2.3.0`, returns `source="github_release"`, passes `package_source_matches()`.
3. `fetch_version_diff`: filters releases strictly to `from_version < v <= to_version`, ignores prereleases, caps output size.
4. `extract_local_stubs` (Python): extracts class/def signatures + docstrings for requested symbols from `.pyi` and `.py` inside fake `.venv`, omits function bodies, refuses symlinks and `..` escapes, respects `max_bytes`.
5. `extract_local_stubs` (JS/TS): extracts matching `export interface` / `export function` declarations from `node_modules/<pkg>/index.d.ts`, refuses symlinks.
6. `source_catalog.json`: loads via `importlib.resources`, preserves `("mcp-tools", "2026-07-28")` identically, rejects malformed/non-HTTPS catalog entries, `official_sources.fetch_official_source` accepts `text/plain` for `llms.txt` and rejects `text/html`.

## What Must Stay Green

- `tests/test_docs_contract.py`, `tests/test_official_*.py`, `tests/test_source_identity.py`, and full offline suite.
- `ruff check`, `ruff format --check`, `git diff --check`.
