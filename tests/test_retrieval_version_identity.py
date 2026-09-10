"""Exact document identity survives legacy cache rows and latest aliases."""

import time
from dataclasses import asdict

import httpx

from universal_docs_mcp.cache import DocsCache
from universal_docs_mcp.registries import PackageInfo
from universal_docs_mcp.retrieval import retrieve_document


async def test_legacy_cache_row_cannot_relabel_exact_document_as_metadata_latest(
    tmp_path,
):
    cache = DocsCache(tmp_path)
    cache.set(
        "docsraw-v4:python:demo:1.0.0",
        {
            "content": "# Usage\nVersion one usage only",
            "source": "pypi_description",
            "source_url": "https://pypi.org/pypi/demo/1.0.0/json",
            "fetched_at": time.time(),
            # The accepted rc2 writer did not persist a version field.
            "package_info": asdict(PackageInfo("demo", "python", "2.0.0", "Demo")),
        },
    )

    async def offline(*args, **kwargs):
        raise httpx.ConnectError("fixture offline")

    loaded = await retrieve_document(
        {"package": "demo", "ecosystem": "python", "version": "1.0.0"},
        cache=cache,
        fetch_package_fn=offline,
    )
    assert loaded is not None
    assert "# demo v1.0.0" in loaded[1]
    assert loaded[4]["document_version"] == "1.0.0"
    assert loaded[4]["metadata_refreshed"] is False
    cache.close()


async def test_old_exact_lookup_does_not_replace_last_latest_fallback(tmp_path):
    from universal_docs_mcp.docs_fetcher import FetchedDocument

    cache = DocsCache(tmp_path)

    async def package(*args, **kwargs):
        return PackageInfo("demo", "python", "2.0.0", "Demo")

    async def document(*args, **kwargs):
        return FetchedDocument(
            "# Usage\nVersion " + kwargs["version"],
            "pypi_description",
            "https://pypi.org/pypi/demo/" + kwargs["version"] + "/json",
        )

    for version in [None, "1.0.0"]:
        await retrieve_document(
            {"package": "demo", "ecosystem": "python", "version": version},
            cache=cache,
            fetch_package_fn=package,
            fetch_document_fn=document,
        )
    fallback = cache.get_stale("docrequest-v4:python:demo:latest")
    assert fallback["version"] == "2.0.0"
    assert "Version 2.0.0" in fallback["content"]
    cache.close()
