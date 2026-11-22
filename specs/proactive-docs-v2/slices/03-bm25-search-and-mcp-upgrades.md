# Slice 03 — Unified BM25 Query Search & MCP Tool Upgrades

Wave: 1 (parallelizable)
Depends on: none (`feat/proactive-docs-v2` base)
Owned files:
- `src/universal_docs_mcp/compaction.py`
- `src/universal_docs_mcp/preflight.py` (replace `_terms`/`_score_section` with `compaction.rank_sections`)
- `src/universal_docs_mcp/validation.py`
- `src/universal_docs_mcp/contracts.py`
- `src/universal_docs_mcp/server.py`
- `tests/test_query_search_and_mcp.py` (new)
- Existing `tests/test_output_schemas.py`, `tests/test_stdio_protocol.py`, `tests/test_tool_boundary.py`, `tests/test_preflight*.py` must stay green.

## Contract Unlocked

1. **One real ranker everywhere:** Replace naive substring `.count()` with word-boundary BM25 + identifier splitting (`model_validator` → `model`, `validator`, `model_validator`; `useEffect` → `use`, `effect`, `useeffect`) in `compaction.py`, shared by both `preflight.py` and `server.py`.
2. **One-shot MCP doc search:** Agents call `get_package_docs(package=..., version=..., query="...")` and get the top relevant sections in 1 round-trip instead of 2–3.
3. **Official docs via MCP:** Expose `get_official_docs` in `server.py` so MCP clients (not just the preflight CLI) can query cataloged official specs/docs (`mcp-tools`, and any catalog entries added in Slice 04).
4. **Dynamic workspace roots via MCP `roots/list`:** When `UNIVERSAL_DOCS_PROJECT_ROOT` is unset, `get_project_dependencies` asks the connected MCP client for its workspace roots via `roots/list` before falling back to `manifest_access_disabled`.

## Single-Owner Design

- `compaction.py` owns ALL query tokenization and section ranking. Delete `_terms()` and `_score_section()` from `preflight.py` (or delegate cleanly if tests import them; check tests first!) so there is never a second ranker.

## API Seam

### 1. `src/universal_docs_mcp/compaction.py`

```python
@dataclass(frozen=True)
class RankedSection:
    section: Section
    score: float
    matched_terms: tuple[str, ...]
    reason: str  # "title and body term match" | "title term match" | "body term match"

def tokenize_query(text: str | None, *, max_terms: int = 32) -> list[str]: ...

def rank_sections(
    sections: list[Section],
    query: str | None,
    *,
    k1: float = 1.5,
    b: float = 0.75,
    title_weight: float = 3.0,
) -> list[RankedSection]: ...

def select_by_query(
    markdown: str,
    query: str,
    *,
    budget_tokens: int = 1500,
    header: str = "",
) -> dict: ...
```

Tokenization rules:
- Extract word-boundary tokens (`[A-Za-z0-9_]+`, length >= 2).
- Split `snake_case` and `camelCase`/`PascalCase` identifiers into subtokens AND retain the joined lowercased identifier (so `model_validator` emits `model_validator`, `model`, `validator`; `ModelValidator` emits `modelvalidator`, `model`, `validator`).
- Light singular normalization for common plurals (`validators` → `validator`, `timeouts` → `timeout`, `retries` → `retry`, only when length >= 4) so singular/plural queries match reliably without external NLP deps.
- Remove stopwords (`a`, `an`, `the`, `and`, `or`, `of`, `to`, `in`, `is`, `are`, `for`, `with`, `how`, `on`, `at`, `from`, `use`, `using`, `docs`, `documentation`, `about`, `please`).
- Word-boundary matching only — `"set"` must **never** match `"offset"` or `"reset"`.
- Deterministic tie-breaking: `(-round(score, 6), original_index)`.

### 2. `src/universal_docs_mcp/validation.py` & `contracts.py`

- **`DocsArgs`:** Add `query: Optional[str] = Field(default=None, min_length=1, max_length=512)`. Validate that `section` and `query` are **not both supplied** (raise `ValueError("section_and_query_mutually_exclusive")` → server returns `invalid_arguments`).
- **`OfficialDocsArgs`:**
  ```python
  class OfficialDocsArgs(EmptyArgs):
      source_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
      version: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
      query: Optional[str] = Field(default=None, min_length=1, max_length=512)
      section: Optional[str] = Field(default=None, min_length=1, max_length=256)
      max_tokens: int = Field(default=1500, ge=200, le=6000)
      force_refresh: bool = False
  ```
  (Mutually exclusive `section` and `query`.)
- **Contracts (`contracts.py`):**
  - Add `QueryDocument(DocumentResult)` (or extend `CompactDocument` cleanly) with `query: str`, `matched_sections: list[str]`, `no_match: bool`, `section_map: list[SectionInfo]`, `section_map_total: Count`, `section_map_truncated: bool`.
  - Add `OfficialDocumentResult(ResultModel)` with `found: Literal[True]`, `source_id: str`, `version: str`, `source: Literal["official_markdown"]`, `source_url: str`, `version_binding: str`, `fetched_at: float`, `content_trust: Literal["untrusted_upstream"]`, `cached: bool`, `content: str`, `truncated: bool`, `tokens_included: Count`, `budget_tokens: int`, `matched_sections: list[str]`, `no_match: bool`, `section_map: list[SectionInfo]`, `section_map_total: Count`, `section_map_truncated: bool`.
  - Register `"get_official_docs"` in `TOOL_ARGS` and `RESULT_TYPES`.

### 3. `src/universal_docs_mcp/server.py`

- **`get_package_docs`:** When `query` is provided, call `compaction.select_by_query(raw, query, budget_tokens=requested_budget, header=header)`. If zero sections match, return `found=True`, `no_match=True`, `content=""` (or header only), `matched_sections=[]`, and `section_map` so the agent sees available headings instead of a blind dump.
- **`get_official_docs`:** Uses `official_preflight.retrieve_official_document` (or `official_sources.fetch_official_source` + `cache`) with strict `outputSchema` validation and `isError: true` on `unsupported_official_source` / `documentation_not_found`.
- **Dynamic MCP `roots/list` fallback in `_handle_project_dependencies`:**
  1. If `UNIVERSAL_DOCS_PROJECT_ROOT` env var is set, use it (100% existing behavior).
  2. Otherwise, inspect `server.request_context.session`. If the client supports `roots` (`list_roots`), request roots with a short timeout (2s). Accept the first valid `file://` URI whose path is absolute, exists, is a real non-symlink directory, and is NOT `/` or `$HOME` (preserve the README rule: never root at home directory).
  3. If multiple valid roots are returned, check if `manifest_path` exists inside exactly one of them; if ambiguous or none match, return `manifest_access_disabled` or `manifest_error` cleanly.
  4. If outside an MCP request context (e.g., direct unit tests calling `call_tool()`), preserve `manifest_access_disabled` when env var is unset.

### 4. `src/universal_docs_mcp/preflight.py`

- Wire `_select_context()` to use `compaction.rank_sections()` instead of the old substring `.count()` scorer.
- Preserve every existing `preflight` receipt field (`matched_sections`, `matches`, `no_match`, `reason`, `context_bytes`, `budget_bytes`, `truncated`) so `context_delivery.py` and all existing preflight tests stay 100% green.

## Human / Checker Playable Verification

```bash
env -u PYTHONPATH -u VIRTUAL_ENV .venv/bin/python -c '
from universal_docs_mcp.compaction import parse_sections, rank_sections
md = "# Pkg\n## Offset Settings\nConfigure offset.\n## Model Validators\nUse @field_validator and model_validator.\n"
secs = parse_sections(md)
print("query=set:", [(r.section.slug, round(r.score, 2)) for r in rank_sections(secs, "set")])
print("query=model_validator:", [(r.section.slug, round(r.score, 2)) for r in rank_sections(secs, "model_validator")])
'
```
Expected: `query=set` returns `[]` (no false match on `offset`), and `query=model_validator` ranks `model-validators` first!

## RED→GREEN Test Cases (`tests/test_query_search_and_mcp.py`)

1. Word-boundary precision: query `"set"` does NOT match `"offset"` or `"reset"`.
2. Identifier splitting: query `"model_validator"` matches section mentioning `"Model Validators"` / `"validators"`; `"useRouter"` matches `"router"`.
3. BM25 weighting: title hit + rarer term beats repeated boilerplate word; deterministic tie-break on equal scores.
4. `get_package_docs` with `query`: returns matched section within `max_tokens`, passes `RESULT_TYPES["get_package_docs"]` schema validation.
5. `get_package_docs` with `query` and no match: returns `no_match=True` + `section_map` (does not dump intro blindly).
6. `get_package_docs` with both `section` and `query`: returns `isError=True`, `error="invalid_arguments"`.
7. `get_official_docs` for `("mcp-tools", "2026-07-28")` with `query="tools/list"`: returns targeted section + strict provenance; unknown `source_id` returns `isError=True`.
8. `get_project_dependencies` without `UNIVERSAL_DOCS_PROJECT_ROOT` uses mocked MCP `list_roots()` returning `file://<tmp_project>` and succeeds; rejects `file:///` and `file://$HOME` (fails closed).
9. `preflight.py` uses `rank_sections` and all existing preflight + output-schema + stdio tests pass.

## What Must Stay Green

- `tests/test_output_schemas.py`, `tests/test_stdio_protocol.py`, `tests/test_tool_boundary.py`, `tests/test_preflight*.py`, and the full offline suite.
- `ruff check`, `ruff format --check`, `git diff --check`.
