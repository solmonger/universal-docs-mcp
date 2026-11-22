# Slice 05 — Fast Freshness (HTTP ETag Conditional Revalidation & Immutable Exact-Version Cache)

Wave: 1 (parallelizable)
Depends on: none (`feat/proactive-docs-v2` base)
Owned files:
- `src/universal_docs_mcp/cache.py`
- `src/universal_docs_mcp/network.py`
- `src/universal_docs_mcp/retrieval.py`
- `tests/test_conditional_freshness.py` (new)
- Existing `tests/test_cache*.py`, `tests/test_retrieval*.py`, `tests/test_network*.py`, `tests/test_preflight*.py` must stay green.

## Contract Unlocked

Cut repeat-turn freshness checks from full multi-hundred-KiB downloads to lightweight conditional checks (`If-None-Match` / `If-Modified-Since` → `304 Not Modified`), and persist HTTP validators cleanly in SQLite without breaking legacy cache rows.

## Problems in Current Code

1. `network.py` never sends `If-None-Match` / `If-Modified-Since` and raises on `304 Not Modified` (Wait: `httpx.Response.raise_for_status()` treats `3xx` as non-2xx or passes 304 without body — `_read` currently has no explicit 304 support and returns an empty body).
2. `cache.py` and `retrieval.py` do not store `etag` or `last_modified` from upstream responses, so `force_refresh=True` or `freshness_mode="require_check"` always re-downloads and re-parses the full document.
3. Exact registry versions (`pypi.org/pypi/<pkg>/<ver>/json`, `registry.npmjs.org/<pkg>/<ver>`) are immutable once published, yet `cache.py` expires them on the same short 24h TTL as mutable `"latest"` metadata.

## API Seam

### 1. `src/universal_docs_mcp/network.py`

- Update `_read()` to allow `response.status_code in (200, 304, 404)` without raising (only non-200/304/404 call `raise_for_status()`). On `304`, return `httpx.Response(304, headers=response.headers, content=b"", request=response.request)` immediately without reading a body or failing on `content-encoding`.
- Add optional conditional headers helper:
  ```python
  async def get_conditional_response(
      url: str,
      *,
      etag: str | None = None,
      last_modified: str | None = None,
      headers: dict | None = None,
      max_bytes: int = MAX_RESPONSE_BYTES,
  ) -> httpx.Response:
  ```
  - Validates `etag` and `last_modified` (ASCII, no CR/LF, max 256 chars; silently drop invalid validator strings rather than injecting bad headers).
  - Attaches `If-None-Match` / `If-Modified-Since` and delegates to `get_response()`.
- Also allow `get_catalog_response(source_id, version, *, etag=None, last_modified=None)` to pass conditional headers cleanly for official docs/`llms.txt`.

### 2. `src/universal_docs_mcp/cache.py`

- Preserve 100% SQLite schema compatibility (`docs_cache` columns unchanged — store `etag` and `last_modified` inside the JSON `value` dict so zero schema migration risk and older readers still work!).
- Add a clean helper:
  ```python
  def touch(self, key: str, *, fetched_at: float | None = None, etag: str | None = None, last_modified: str | None = None) -> bool:
  ```
  Updates a retained row's `fetched_at` (both column and embedded JSON `fetched_at` + optional `etag`/`last_modified`) atomically after a `304 Not Modified` confirmation, without rewriting the whole document if untouched or violating size quotas.
- Support optional per-call `ttl` override in `get(key, *, ttl: int | None = None)` (defaults to `self.ttl`, keeping existing callers 100% unchanged).
- Define `IMMUTABLE_VERSION_TTL = 7 * 24 * 60 * 60` (7 days) as a constant for verified `registry_version` exact-version documents (while keeping `DEFAULT_TTL = 86400` for `unverified_git_ref`, `latest`, and `info:*` metadata).

### 3. `src/universal_docs_mcp/retrieval.py`

- When `retrieve_document()` loads an exact requested version (`requested and not force_refresh`), use `IMMUTABLE_VERSION_TTL` **only** when the cached row's `source` is a registry exact-version endpoint (`pypi_description`, `npm_readme`) — keep `DEFAULT_TTL` (24h) for `github_readme` (since Git refs can move!).
- Add conditional revalidation helper:
  ```python
  async def revalidate_cached_document(
      record: dict[str, Any],
      *,
      cache: DocsCache,
      cache_key: str,
      fetch_conditional_fn: Callable[..., Awaitable[httpx.Response]] = get_conditional_response,
  ) -> tuple[bool, dict[str, Any]]:
  ```
  If `record` has a valid `source_url` (on `ALLOWED_HOSTS`) and an `etag` or `last_modified`:
  - Sends conditional GET.
  - On `304 Not Modified`: updates `record["fetched_at"]` (and updated `etag` if returned) via `cache.touch()` (or `cache.set()`) and returns `(True, updated_record)` so the caller gets `cached=False`, `state="upstream_checked"`, fresh `checked_at`, with zero body download!
  - On `200` or miss: returns `(False, record)` so caller proceeds with normal full fetch.
- In `retrieve_document()`:
  - When `force_refresh=True` and a valid prior cached record exists with `etag`/`last_modified` matching the exact requested package+version+source identity, attempt `revalidate_cached_document()` first. If `304`, return the refreshed record with `cached=False`, `metadata_refreshed=True`, and updated `fetched_at`!
  - When fetching fresh from upstream (if `fetched` exposes optional `etag`/`last_modified` or via response metadata), store `etag`/`last_modified` on the cached record when present. (Keep `FetchedDocument` backward-compatible — default `etag: str | None = None, last_modified: str | None = None` or handle `getattr(fetched, "etag", None)` so Slice 04 and Slice 05 never conflict!)
  - **CRITICAL:** Do NOT edit `docs_fetcher.py` in Slice 05 (Slice 04 owns `docs_fetcher.py`). Use `getattr(fetched, "etag", None)` and `getattr(fetched, "last_modified", None)` in `retrieval.py` so Slice 05 compiles and passes independently!

## Bounds & Safety

- Conditional headers must reject `\r`, `\n`, `\x00`, non-ASCII, and values > 256 bytes (header injection defense).
- `304 Not Modified` must NEVER bypass `package_source_matches()` identity verification: revalidate ONLY a row that already passed `_validated_record()`.
- `IMMUTABLE_VERSION_TTL` (7 days) applies ONLY to `registry_version` rows (`pypi_description`, `npm_readme`), NEVER to `github_readme` (`unverified_git_ref`) or `latest` metadata.
- All existing `force_refresh=True` tests without `etag` on cached records continue to trigger full upstream fetches as before.

## Human / Checker Playable Verification

```bash
env -u PYTHONPATH -u VIRTUAL_ENV .venv/bin/python -m pytest tests/test_conditional_freshness.py -v
```

## RED→GREEN Test Cases (`tests/test_conditional_freshness.py`)

1. `get_conditional_response` sends `If-None-Match` and `If-Modified-Since`, returns `304` cleanly without raising or requiring body/content-encoding.
2. Malicious/CRLF `etag` (`"abc\r\nX-Evil: 1"`) is rejected/stripped and never sent on the wire.
3. `cache.touch()` refreshes `fetched_at` on a retained row without corrupting `value_bytes` or breaking quotas.
4. `retrieve_document(..., force_refresh=True)` with a cached record containing `etag`: receives `304` → returns fresh `fetched_at`, `cached=False`, does NOT invoke full `fetch_document_fn`.
5. `retrieve_document(..., force_refresh=True)` when upstream returns `200` (etag changed): falls through to full `fetch_document_fn` and updates cache.
6. Immutable TTL boundary: a 48-hour-old `pypi_description` exact-version cache row is a valid cache hit (under 7d), while a 48-hour-old `github_readme` row is expired (over 24h) and refetches.
7. Tampered cached row with mismatched `source_url` is NEVER revalidated via 304 (fails identity check and refetches cleanly).

## What Must Stay Green

- `tests/test_cache*.py`, `tests/test_retrieval*.py`, `tests/test_network*.py`, `tests/test_preflight*.py`, and full offline suite.
- `ruff check`, `ruff format --check`, `git diff --check`.
