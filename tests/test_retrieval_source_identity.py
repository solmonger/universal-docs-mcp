"""Exact cache rows must bind package, ecosystem, version, and source."""

import time
from dataclasses import asdict

import httpx
import pytest

from universal_docs_mcp.cache import DocsCache
from universal_docs_mcp.docs_fetcher import FetchedDocument
from universal_docs_mcp.registries import PackageInfo
from universal_docs_mcp.retrieval import (
    load_stale_record,
    retrieve_document,
)
from universal_docs_mcp.source_identity import package_source_matches


def _info(*, package="demo", ecosystem="python", repository=None):
    return PackageInfo(
        package,
        ecosystem,
        "2.0.0",
        "Demo",
        repository=repository,
    )


def _record(
    *,
    package="demo",
    ecosystem="python",
    version="1.2.3",
    source="pypi_description",
    source_url=None,
    include_version=True,
    repository=None,
    content="exact document",
    age=0,
):
    if source_url is None:
        source_url = "https://pypi.org/pypi/demo/1.2.3/json"
    value = {
        "content": content,
        "source": source,
        "source_url": source_url,
        "fetched_at": time.time() - age,
        "package_info": asdict(
            _info(package=package, ecosystem=ecosystem, repository=repository)
        ),
    }
    if include_version:
        value["version"] = version
    return value


@pytest.mark.parametrize(
    "key",
    [
        "docrequest-v4:python:demo:1.2.3",
        "docsraw-v4:python:demo:1.2.3",
        "docrequest-v4:pypi:demo:1.2.3",
        "docsraw-v4:pip:demo:1.2.3",
    ],
)
async def test_conflicting_exact_alias_is_refetched_not_relabelled(tmp_path, key):
    cache = DocsCache(tmp_path)
    cache.set(
        key,
        _record(
            version="9.9.9",
            source_url="https://pypi.org/pypi/demo/9.9.9/json",
            content="wrong 9.9.9 document",
        ),
    )
    calls = []

    async def package(*args, **kwargs):
        calls.append("metadata")
        return _info()

    async def document(*args, **kwargs):
        calls.append("document")
        return FetchedDocument(
            "right 1.2.3 document",
            "pypi_description",
            "https://pypi.org/pypi/demo/1.2.3/json",
        )

    loaded = await retrieve_document(
        {"package": "demo", "ecosystem": "pypi", "version": "1.2.3"},
        cache=cache,
        fetch_package_fn=package,
        fetch_document_fn=document,
    )

    assert loaded is not None
    assert loaded[2] == "right 1.2.3 document"
    assert calls == ["metadata", "document"]
    cache.close()


@pytest.mark.parametrize(
    "key,ecosystem",
    [
        ("docrequest-v4:python:demo:1.2.3", "python"),
        ("docsraw-v4:python:demo:1.2.3", "pypi"),
        ("docrequest-v4:pypi:demo:1.2.3", "pypi"),
        ("docsraw-v4:pip:demo:1.2.3", "pip"),
    ],
)
async def test_valid_legacy_exact_alias_is_offline_hit(tmp_path, key, ecosystem):
    cache = DocsCache(tmp_path)
    cache.set(key, _record(include_version=False))

    async def offline(*args, **kwargs):
        raise httpx.ConnectError("offline")

    loaded = await retrieve_document(
        {"package": "demo", "ecosystem": ecosystem, "version": "1.2.3"},
        cache=cache,
        fetch_package_fn=offline,
    )

    assert loaded is not None
    assert loaded[2] == "exact document"
    assert loaded[4]["document_version"] == "1.2.3"
    assert loaded[3] is True
    cache.close()


async def test_latest_uses_valid_canonical_raw_hit_after_metadata_refresh(tmp_path):
    cache = DocsCache(tmp_path)
    cache.set(
        "docsraw-v4:python:demo:2.0.0",
        _record(
            version="2.0.0",
            source_url="https://pypi.org/pypi/demo/2.0.0/json",
        ),
    )
    calls = []

    async def package(*args, **kwargs):
        calls.append("metadata")
        return _info()

    async def document(*args, **kwargs):
        calls.append("document")
        raise AssertionError("valid canonical raw cache must avoid document fetch")

    loaded = await retrieve_document(
        {"package": "demo", "ecosystem": "python"},
        cache=cache,
        fetch_package_fn=package,
        fetch_document_fn=document,
    )

    assert loaded is not None
    assert loaded[2] == "exact document"
    assert loaded[3] is True
    assert loaded[4]["metadata_refreshed"] is True
    assert calls == ["metadata"]
    cache.close()


async def test_latest_rejects_conflicting_canonical_raw_before_return(tmp_path):
    cache = DocsCache(tmp_path)
    cache.set(
        "docsraw-v4:python:demo:1.2.3",
        _record(
            version="9.9.9",
            source_url="https://pypi.org/pypi/demo/9.9.9/json",
            content="wrong latest document",
        ),
    )

    async def package(*args, **kwargs):
        return _info()

    async def document(*args, **kwargs):
        assert kwargs["version"] == "2.0.0"
        return FetchedDocument(
            "fresh latest document",
            "pypi_description",
            "https://pypi.org/pypi/demo/2.0.0/json",
        )

    loaded = await retrieve_document(
        {"package": "demo", "ecosystem": "python"},
        cache=cache,
        fetch_package_fn=package,
        fetch_document_fn=document,
    )

    assert loaded is not None
    assert loaded[2] == "fresh latest document"
    assert loaded[3] is False
    cache.close()


async def test_fetched_document_with_conflicting_source_is_rejected_and_not_cached(
    tmp_path,
):
    cache = DocsCache(tmp_path)

    async def package(*args, **kwargs):
        return _info()

    async def document(*args, **kwargs):
        return FetchedDocument(
            "secret source document",
            "pypi_description",
            "file:///private/secret/source.md",
        )

    with pytest.raises(ValueError, match="invalid_document_identity"):
        await retrieve_document(
            {"package": "demo", "ecosystem": "python", "version": "1.2.3"},
            cache=cache,
            fetch_package_fn=package,
            fetch_document_fn=document,
        )

    assert cache.get("docsraw-v4:python:demo:1.2.3") is None
    cache.close()


@pytest.mark.parametrize(
    "source,source_url,repository",
    [
        (
            "pypi_description",
            "https://pypi.org/pypi/demo/1.2.3/json",
            None,
        ),
        (
            "github_readme",
            "https://api.github.com/repos/acme/demo/readme?ref=1.2.3",
            "https://github.com/acme/demo",
        ),
    ],
)
def test_package_source_matches_safe_exact_sources(source, source_url, repository):
    assert (
        package_source_matches(
            package="demo",
            ecosystem="python",
            version="1.2.3",
            source=source,
            source_url=source_url,
            repository=repository,
        )
        is True
    )


@pytest.mark.parametrize(
    "source,source_url,repository",
    [
        (
            "pypi_description",
            "https://pypi.org/pypi/demo/9.9.9/json",
            None,
        ),
        (
            "pypi_description",
            "file:///private/secret/source.md",
            None,
        ),
        (
            "pypi_description",
            "https://evil.test/pypi/demo/1.2.3/json",
            None,
        ),
        (
            "github_readme",
            "https://api.github.com/repos/other/demo/readme?ref=1.2.3",
            "https://github.com/acme/demo",
        ),
        (
            "github_readme",
            "https://api.github.com/repos/acme/demo/readme?ref=main",
            "https://github.com/acme/demo",
        ),
        (
            "github_readme",
            "file:///private/secret/source.md",
            "https://github.com/acme/demo",
        ),
        (
            "npm_readme",
            "https://pypi.org/pypi/demo/1.2.3/json",
            None,
        ),
    ],
)
def test_package_source_matches_rejects_conflicts_and_unsafe_urls(
    source, source_url, repository
):
    assert (
        package_source_matches(
            package="demo",
            ecosystem="python",
            version="1.2.3",
            source=source,
            source_url=source_url,
            repository=repository,
        )
        is False
    )


async def test_stale_latest_fallback_rejects_wrong_source_identity(tmp_path):
    cache = DocsCache(tmp_path)
    cache.set(
        "docrequest-v4:python:demo:latest",
        _record(
            version="9.9.9",
            source_url="file:///private/secret/source.md",
            content="wrong stale document",
            age=20,
        ),
    )

    assert (
        load_stale_record(
            cache,
            "demo",
            "python",
            None,
            max_age=3600,
        )
        is None
    )
    cache.close()


async def test_stale_exact_legacy_registry_url_is_bound_to_requested_version(tmp_path):
    cache = DocsCache(tmp_path)
    cache.set(
        "docsraw-v4:python:demo:1.2.3",
        _record(include_version=False, age=20),
    )

    loaded = load_stale_record(
        cache,
        "demo",
        "python",
        "1.2.3",
        max_age=3600,
    )

    assert loaded is not None
    record, _info_value = loaded
    assert record["version"] == "1.2.3"
    cache.close()
