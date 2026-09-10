"""Shared document retrieval used by MCP handlers and the preflight CLI.

This module owns the package metadata/document/cache sequence.  Consumers pass
in their cache and fetch functions so tests and protocol adapters can reuse the
same retrieval behavior without importing SDK wire types.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .cache import DEFAULT_TTL, DocsCache
from .docs_fetcher import FetchedDocument, fetch_docs_content_with_provenance
from .registries import PackageInfo, fetch_package
from .validation import ALIASES

FetchPackage = Callable[..., Awaitable[PackageInfo | None]]
FetchDocument = Callable[..., Awaitable[FetchedDocument | None]]


def valid_doc_record(record: object, *, ttl: int = DEFAULT_TTL) -> bool:
    if not isinstance(record, dict) or not isinstance(record.get("content"), str):
        return False
    timestamp = record.get("fetched_at")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return False
    try:
        age = time.time() - timestamp
    except (OverflowError, TypeError):
        return False
    return bool(record["content"]) and 0 <= age < ttl


def _record_info(record: dict[str, Any]) -> PackageInfo | None:
    package_info = record.get("package_info")
    if not isinstance(package_info, dict):
        return None
    try:
        return PackageInfo(**package_info)
    except (TypeError, ValueError):
        return None


def _loaded_from_record(
    record: dict[str, Any],
    info: PackageInfo,
    *,
    version: str,
    metadata_refreshed: bool,
    cached: bool,
) -> tuple[PackageInfo, str, str, bool, dict[str, Any]]:
    content = record["content"]
    source = record.get("source")
    source_url = record.get("source_url")
    fetched_at = record.get("fetched_at")
    provenance = {
        "document_version": version,
        "source": source,
        "source_url": source_url,
        "fetched_at": fetched_at,
        "metadata_refreshed": metadata_refreshed,
        "version_binding": (
            "unverified_git_ref" if source == "github_readme" else "registry_version"
        ),
        "content_trust": "untrusted_upstream",
    }
    header = (
        f"# {info.name} v{version} ({info.ecosystem})\n"
        f"Source: {source}\n"
        f"Version binding: {provenance['version_binding']}\n"
        f"Source URL: {source_url}\n\n---\n\n"
    )
    return info, header, content, cached, provenance


def stale_record_keys(package: str, ecosystem: str, version: str | None) -> list[str]:
    namespace = ALIASES.get(ecosystem, ecosystem)
    keys = []
    if version:
        keys.extend(
            [
                f"docrequest-v4:{namespace}:{package}:{version}",
                f"docsraw-v4:{namespace}:{package}:{version}",
            ]
        )
    else:
        keys.append(f"docrequest-v4:{namespace}:{package}:latest")
    return keys


def load_stale_record(
    cache: DocsCache,
    package: str,
    ecosystem: str,
    version: str | None,
    *,
    max_age: int = 7 * 24 * 60 * 60,
) -> tuple[dict[str, Any], PackageInfo] | None:
    """Return a bounded expired record when the caller explicitly permits it."""
    get_stale = getattr(cache, "get_stale", None)
    if not callable(get_stale):
        return None
    for key in stale_record_keys(package, ecosystem, version):
        record = get_stale(key, max_age=max_age)
        if not isinstance(record, dict):
            continue
        info = _record_info(record)
        if (
            info is not None
            and isinstance(record.get("content"), str)
            and record["content"]
        ):
            return record, info
    return None


async def retrieve_document(
    args: Mapping[str, Any],
    *,
    cache: DocsCache,
    fetch_package_fn: FetchPackage = fetch_package,
    fetch_document_fn: FetchDocument = fetch_docs_content_with_provenance,
) -> tuple[PackageInfo, str, str, bool, dict[str, Any]] | None:
    """Retrieve one exact document, preserving the legacy tuple contract.

    A requested exact version may use a valid exact cache entry.  A latest
    request always resolves package metadata upstream so a cached metadata row
    can never masquerade as a current latest check.  Stale records are not
    selected here; callers must opt into :func:`load_stale_record` after an
    upstream failure.
    """
    package = args["package"]
    ecosystem = args.get("ecosystem")
    requested = args.get("version")
    force_refresh = bool(args.get("force_refresh"))
    namespace = ALIASES.get(ecosystem, ecosystem) if ecosystem else "auto"
    request_key = f"docrequest-v4:{namespace}:{package}:{requested}"

    record: dict[str, Any] | None = None
    if requested and not force_refresh:
        for key in (request_key, f"docsraw-v4:{namespace}:{package}:{requested}"):
            candidate = cache.get(key)
            if isinstance(candidate, dict) and valid_doc_record(
                candidate, ttl=getattr(cache, "ttl", DEFAULT_TTL)
            ):
                if _record_info(candidate) is not None:
                    record = candidate
                    break

    metadata_refreshed = False
    was_cached = False
    if record is not None:
        info = _record_info(record)
        assert info is not None
        return _loaded_from_record(
            record, info, version=requested, metadata_refreshed=False, cached=True
        )

    info = await fetch_package_fn(package, ecosystem)
    if not info:
        return None
    version = requested or info.latest_stable
    if not version:
        raise ValueError("no_stable_release")

    cache_key = f"docsraw-v4:{info.ecosystem}:{info.name}:{version}"
    if not force_refresh:
        candidate = cache.get(cache_key)
        if isinstance(candidate, dict) and valid_doc_record(
            candidate, ttl=getattr(cache, "ttl", DEFAULT_TTL)
        ):
            record = candidate
            was_cached = True

    metadata_refreshed = True
    if record is None:
        fetched = await fetch_document_fn(
            package=info.name,
            ecosystem=info.ecosystem,
            docs_url=info.docs_url,
            repo_url=info.repository,
            version=version,
        )
        if not fetched:
            # Preserve the server's distinction between a known package and a
            # package whose version has no supported documentation source.
            return info, "", "", False, {}
        if len(fetched.content.encode("utf-8")) > 1024 * 1024:
            raise ValueError("document_too_large")
        record = {
            "content": fetched.content,
            "source": fetched.source,
            "source_url": fetched.source_url,
            "fetched_at": time.time(),
            "version": version,
            "package_info": {
                key: getattr(info, key, None)
                for key in PackageInfo.__dataclass_fields__
            },
        }
        cache.set(cache_key, record)
        was_cached = False

    # Keep exact aliases and a latest fallback alias in the shared cache.  The
    # latest alias is only a stale fallback candidate; it is never a fresh hit.
    cache.set(f"docrequest-v4:{namespace}:{package}:{version}", record)
    if requested is None or version == info.latest_stable:
        cache.set(f"docrequest-v4:{namespace}:{package}:latest", record)
    return _loaded_from_record(
        record,
        info,
        version=version,
        metadata_refreshed=metadata_refreshed,
        cached=was_cached,
    )
