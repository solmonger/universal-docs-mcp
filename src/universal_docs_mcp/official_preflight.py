"""Bounded cache/retrieval for exact cataloged official source documents."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .cache import DEFAULT_TTL, DocsCache
from .docs_fetcher import FetchedDocument
from .official_sources import fetch_official_source
from .source_catalog import OfficialSource, source_for

OFFICIAL_CACHE_PREFIX = "official-doc-v1"
OFFICIAL_MAX_DOCUMENT_BYTES = 1024 * 1024
OFFICIAL_MAX_STALE_AGE_SECONDS = 7 * 24 * 60 * 60

FetchOfficial = Callable[[str, str], Awaitable[FetchedDocument | None]]


@dataclass(frozen=True)
class OfficialDocument:
    content: str
    source_id: str
    version: str
    source: str
    source_url: str
    version_binding: str
    fetched_at: float
    cached: bool


def official_cache_key(source_id: str, version: str) -> str:
    """Return the one canonical cache key for an exact official document."""
    return f"{OFFICIAL_CACHE_PREFIX}:{source_id}:{version}"


def _valid_record(
    record: object,
    source: OfficialSource,
    *,
    ttl: int | float,
    max_age: int | float | None = None,
) -> bool:
    if not isinstance(record, dict):
        return False
    content = record.get("content")
    fetched_at = record.get("fetched_at")
    if (
        not isinstance(content, str)
        or not content
        or len(content.encode("utf-8")) > OFFICIAL_MAX_DOCUMENT_BYTES
    ):
        return False
    if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
        return False
    if record.get("source_id") != source.source_id:
        return False
    if record.get("version") != source.version:
        return False
    if record.get("source") != "official_markdown":
        return False
    if record.get("source_url") != source.retrieval_url:
        return False
    if record.get("version_binding") != source.version_binding:
        return False
    try:
        age = time.time() - float(fetched_at)
    except (OverflowError, TypeError):
        return False
    if max_age is None:
        return 0 <= age < ttl
    return 0 <= age <= max_age


def _as_document(record: dict[str, Any], *, cached: bool) -> OfficialDocument:
    return OfficialDocument(
        content=record["content"],
        source_id=record["source_id"],
        version=record["version"],
        source=record["source"],
        source_url=record["source_url"],
        version_binding=record["version_binding"],
        fetched_at=float(record["fetched_at"]),
        cached=cached,
    )


def load_stale_official_document(
    cache: DocsCache,
    source_id: str,
    version: str,
    *,
    max_age: int = OFFICIAL_MAX_STALE_AGE_SECONDS,
) -> OfficialDocument | None:
    """Load only an exact, bounded snapshot for explicit stale fallback."""
    source = source_for(source_id, version)
    get_stale = getattr(cache, "get_stale", None)
    if not callable(get_stale):
        return None
    record = get_stale(official_cache_key(source_id, version), max_age=max_age)
    if _valid_record(record, source, ttl=DEFAULT_TTL, max_age=max_age):
        return _as_document(record, cached=True)
    return None


async def retrieve_official_document(
    source_id: str,
    version: str,
    *,
    cache: DocsCache,
    fetch_fn: FetchOfficial = fetch_official_source,
    use_cache: bool = True,
) -> OfficialDocument | None:
    """Retrieve one exact catalog entry, retaining no latest or URL fallback."""
    source = source_for(source_id, version)
    key = official_cache_key(source_id, version)

    if use_cache:
        candidate = cache.get(key)
        if isinstance(candidate, dict) and _valid_record(
            candidate, source, ttl=getattr(cache, "ttl", DEFAULT_TTL)
        ):
            return _as_document(candidate, cached=True)

    fetched = await fetch_fn(source_id, version)
    if fetched is None:
        return None
    if not isinstance(fetched.content, str) or not fetched.content:
        return None
    if (
        fetched.source != "official_markdown"
        or fetched.source_url != source.retrieval_url
        or len(fetched.content.encode("utf-8")) > OFFICIAL_MAX_DOCUMENT_BYTES
    ):
        raise ValueError("invalid_official_document")

    record = {
        "content": fetched.content,
        "source_id": source.source_id,
        "version": source.version,
        "source": fetched.source,
        "source_url": fetched.source_url,
        "version_binding": source.version_binding,
        "fetched_at": time.time(),
    }
    cache.set(key, record)
    return _as_document(record, cached=False)
