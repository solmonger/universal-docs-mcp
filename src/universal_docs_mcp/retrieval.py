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
from .source_identity import (
    package_source_matches,
    registry_version_for_source_url,
)
from .validation import ALIASES

FetchPackage = Callable[..., Awaitable[PackageInfo | None]]
FetchDocument = Callable[..., Awaitable[FetchedDocument | None]]
_MISSING = object()
_SUPPORTED_ECOSYSTEMS = frozenset({"python", "javascript", "rust"})


def valid_doc_record(
    record: object,
    *,
    ttl: int | float = DEFAULT_TTL,
    package: str | None = None,
    ecosystem: str | None = None,
    version: str | None = None,
) -> bool:
    """Validate cache shape and, when supplied, exact package-source identity."""
    if not isinstance(record, dict) or not isinstance(record.get("content"), str):
        return False
    timestamp = record.get("fetched_at")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return False
    try:
        age = time.time() - timestamp
    except (OverflowError, TypeError):
        return False
    if not record["content"] or not 0 <= age < ttl:
        return False
    if package is None:
        return True
    return (
        _validated_record(
            record,
            package=package,
            ecosystem=ecosystem,
            requested_version=version,
        )
        is not None
    )


def _record_info(record: dict[str, Any]) -> PackageInfo | None:
    package_info = record.get("package_info")
    if not isinstance(package_info, dict):
        return None
    try:
        return PackageInfo(**package_info)
    except (TypeError, ValueError):
        return None


def _canonical_ecosystem(ecosystem: str | None) -> str | None:
    if not isinstance(ecosystem, str):
        return None
    return ALIASES.get(ecosystem, ecosystem)


def _read_namespaces(ecosystem: str | None) -> list[str]:
    """Return canonical and legacy alias namespaces worth checking."""
    canonical = _canonical_ecosystem(ecosystem)
    if canonical is None:
        namespaces = ["auto"]
        namespaces.extend(ALIASES)
    else:
        namespaces = [canonical]
        namespaces.extend(
            alias for alias, target in ALIASES.items() if target == canonical
        )
        namespaces.append("auto")
        if ecosystem not in namespaces:
            namespaces.append(ecosystem or canonical)
    return list(dict.fromkeys(namespaces))


def _write_namespaces(ecosystem: str | None, resolved_ecosystem: str) -> list[str]:
    namespaces = [resolved_ecosystem]
    request_namespace = _canonical_ecosystem(ecosystem) or "auto"
    if request_namespace != resolved_ecosystem:
        namespaces.append(request_namespace)
    return list(dict.fromkeys(namespaces))


def _package_info_matches_request(
    info: PackageInfo, *, package: str, ecosystem: str | None
) -> bool:
    name = getattr(info, "name", None)
    if not isinstance(name, str) or name != package:
        return False
    resolved = _canonical_ecosystem(getattr(info, "ecosystem", None))
    expected = _canonical_ecosystem(ecosystem)
    return resolved in _SUPPORTED_ECOSYSTEMS and expected in (
        None,
        "auto",
        resolved,
    )


def _validated_record(
    record: dict[str, Any],
    *,
    package: str,
    ecosystem: str | None,
    requested_version: str | None,
    expected_repository: str | None = None,
) -> tuple[PackageInfo, str] | None:
    """Return parsed metadata and bound version, or reject the row."""
    info = _record_info(record)
    if info is None or info.name != package:
        return None
    record_ecosystem = _canonical_ecosystem(info.ecosystem)
    expected_ecosystem = _canonical_ecosystem(ecosystem)
    if record_ecosystem not in _SUPPORTED_ECOSYSTEMS or expected_ecosystem not in (
        None,
        "auto",
        record_ecosystem,
    ):
        return None

    stored_version = record.get("version", _MISSING)
    if stored_version is _MISSING:
        # Pre-identity cache writers did not persist version.  Only the exact
        # registry URL can recover that identity; a Git ref cannot.
        bound_version = registry_version_for_source_url(
            package=info.name,
            ecosystem=record_ecosystem,
            source=record.get("source"),
            source_url=record.get("source_url"),
        )
    else:
        if not isinstance(stored_version, str) or not stored_version:
            return None
        bound_version = stored_version

    if not bound_version or (
        requested_version is not None and bound_version != requested_version
    ):
        return None
    if not package_source_matches(
        package=info.name,
        ecosystem=record_ecosystem,
        version=bound_version,
        source=record.get("source"),
        source_url=record.get("source_url"),
        repository=info.repository,
    ):
        return None
    if expected_repository is not None and not package_source_matches(
        package=info.name,
        ecosystem=record_ecosystem,
        version=bound_version,
        source=record.get("source"),
        source_url=record.get("source_url"),
        repository=expected_repository,
    ):
        return None
    return info, bound_version


def _cached_record(
    candidate: object,
    *,
    package: str,
    ecosystem: str | None,
    requested_version: str | None,
    ttl: int | float,
    expected_repository: str | None = None,
) -> tuple[dict[str, Any], PackageInfo, str] | None:
    if not isinstance(candidate, dict) or not valid_doc_record(candidate, ttl=ttl):
        return None
    validated = _validated_record(
        candidate,
        package=package,
        ecosystem=ecosystem,
        requested_version=requested_version,
        expected_repository=expected_repository,
    )
    if validated is None:
        return None
    info, bound_version = validated
    return candidate, info, bound_version


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


def _cache_keys(package: str, ecosystem: str | None, version: str | None) -> list[str]:
    keys: list[str] = []
    for namespace in _read_namespaces(ecosystem):
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


def stale_record_keys(
    package: str, ecosystem: str | None, version: str | None
) -> list[str]:
    return _cache_keys(package, ecosystem, version)


def load_stale_record(
    cache: DocsCache,
    package: str,
    ecosystem: str | None,
    version: str | None,
    *,
    max_age: int = 7 * 24 * 60 * 60,
) -> tuple[dict[str, Any], PackageInfo] | None:
    """Return a bounded expired record with exact source identity validated."""
    get_stale = getattr(cache, "get_stale", None)
    if not callable(get_stale):
        return None
    for key in stale_record_keys(package, ecosystem, version):
        record = get_stale(key, max_age=max_age)
        if not isinstance(record, dict) or not isinstance(record.get("content"), str):
            continue
        validated = _validated_record(
            record,
            package=package,
            ecosystem=ecosystem,
            requested_version=version,
        )
        if validated is None:
            continue
        info, bound_version = validated
        # Keep the legacy row immutable in the cache while giving callers a
        # truthful version for a row whose old writer omitted the field.
        if "version" not in record:
            record = {**record, "version": bound_version}
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

    if requested and not force_refresh:
        for key in _cache_keys(package, ecosystem, requested):
            cached = _cached_record(
                cache.get(key),
                package=package,
                ecosystem=ecosystem,
                requested_version=requested,
                ttl=getattr(cache, "ttl", DEFAULT_TTL),
            )
            if cached is None:
                continue
            record, info, version = cached
            return _loaded_from_record(
                record,
                info,
                version=version,
                metadata_refreshed=False,
                cached=True,
            )

    metadata_refreshed = False
    was_cached = False
    info = await fetch_package_fn(package, ecosystem)
    if not info:
        return None
    if not _package_info_matches_request(
        info,
        package=package,
        ecosystem=ecosystem,
    ):
        raise ValueError("invalid_document_identity")
    version = requested or info.latest_stable
    if not version:
        raise ValueError("no_stable_release")

    cache_key = f"docsraw-v4:{info.ecosystem}:{info.name}:{version}"
    record: dict[str, Any] | None = None
    if not force_refresh:
        cached = _cached_record(
            cache.get(cache_key),
            package=info.name,
            ecosystem=info.ecosystem,
            requested_version=version,
            ttl=getattr(cache, "ttl", DEFAULT_TTL),
            expected_repository=info.repository,
        )
        if cached is not None:
            record = cached[0]
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
        if not isinstance(fetched.content, str) or not fetched.content:
            raise ValueError("invalid_document_identity")
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
        if (
            _validated_record(
                record,
                package=info.name,
                ecosystem=info.ecosystem,
                requested_version=version,
                expected_repository=info.repository,
            )
            is None
        ):
            raise ValueError("invalid_document_identity")
        cache.set(cache_key, record)
        was_cached = False
    else:
        # A record loaded after the metadata fetch must still be checked against
        # the fresh package metadata, not only against its own embedded row.
        if (
            _validated_record(
                record,
                package=info.name,
                ecosystem=info.ecosystem,
                requested_version=version,
                expected_repository=info.repository,
            )
            is None
        ):
            # This branch is defensive: _cached_record performs the same check,
            # but keeping the invariant immediately before return prevents a
            # future cache adapter from bypassing it.
            raise ValueError("invalid_document_identity")

    # Keep exact aliases and a latest fallback alias in the shared cache.  The
    # latest alias is only a stale fallback candidate; it is never a fresh hit.
    for write_namespace in _write_namespaces(ecosystem, info.ecosystem):
        cache.set(
            f"docrequest-v4:{write_namespace}:{info.name}:{version}",
            record,
        )
        if requested is None or version == info.latest_stable:
            cache.set(
                f"docrequest-v4:{write_namespace}:{info.name}:latest",
                record,
            )
    return _loaded_from_record(
        record,
        info,
        version=version,
        metadata_refreshed=metadata_refreshed,
        cached=was_cached,
    )
