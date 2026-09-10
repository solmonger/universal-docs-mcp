"""Integrity covers the emitted selection, not publisher authentication."""

import hashlib

import pytest

from universal_docs_mcp.cache import DocsCache
from universal_docs_mcp.docs_fetcher import FetchedDocument
from universal_docs_mcp.preflight import PreflightRequest, run_preflight
from universal_docs_mcp.registries import PackageInfo


async def produced_result(tmp_path):
    request = PreflightRequest(
        package="demo",
        ecosystem="python",
        selection="requested",
        requested_version="1.2.3",
        query="usage",
        freshness_mode="require_check",
    )

    async def package(*_args):
        return PackageInfo(
            name="demo",
            ecosystem="python",
            latest_stable="1.2.3",
            description="Fixture",
        )

    async def document(**_kwargs):
        return FetchedDocument(
            "# Demo\n## Usage\nOriginal documentation bytes.",
            "pypi_description",
            "https://pypi.org/pypi/demo/1.2.3/json",
        )

    cache = DocsCache(tmp_path)
    try:
        return request, await run_preflight(
            request, cache=cache, fetch_package_fn=package, fetch_document_fn=document
        )
    finally:
        cache.close()


async def test_producer_binds_the_actual_selected_context(tmp_path):
    _, result = await produced_result(tmp_path)
    assert result["found"] is True
    assert "integrity" in result["receipt"]
    integrity = result["receipt"]["integrity"]
    assert (
        integrity["context_sha256"]
        == hashlib.sha256(result["context"].encode()).hexdigest()
    )
    assert (
        integrity["source_authentication"]
        == "trusted_retriever_not_publisher_authenticated"
    )


async def test_delivery_rejects_changed_context_with_the_original_receipt(tmp_path):
    from universal_docs_mcp.context_delivery import deliver_result

    req, result = await produced_result(tmp_path)
    assert deliver_result(req, result).status == "prepared"
    result["context"] = result["context"].replace("Original", "Tampered")
    outcome = deliver_result(req, result)
    assert outcome.status == "unavailable"
    assert outcome.context == ""


async def test_delivery_rejects_changed_source_digest(tmp_path):
    from universal_docs_mcp.context_delivery import deliver_result

    req, result = await produced_result(tmp_path)
    result["receipt"]["source"]["content_sha256"] = "0" * 64
    assert deliver_result(req, result).status == "unavailable"


@pytest.mark.parametrize(
    "url",
    [
        "file:///private/secret/source.md",
        "https://user:secret@pypi.org/pypi/demo/1.2.3/json",
        "https://other.invalid/pypi/demo/1.2.3/json",
        "https://pypi.org/pypi/demo/9.9.9/json",
    ],
)
async def test_consistently_bound_but_wrong_source_url_is_not_delivered(tmp_path, url):
    from universal_docs_mcp.context_delivery import deliver_result
    from universal_docs_mcp.context_integrity import context_integrity

    req, result = await produced_result(tmp_path)
    result["receipt"]["source"]["url"] = url
    result["receipt"]["integrity"] = context_integrity(
        result["context"], result["receipt"]
    )
    outcome = deliver_result(req, result)
    assert outcome.status == "unavailable"
    assert outcome.context == ""
