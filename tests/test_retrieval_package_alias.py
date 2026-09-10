"""Registry spelling aliases do not weaken exact version/source binding."""

import pytest

from universal_docs_mcp.cache import DocsCache
from universal_docs_mcp.docs_fetcher import FetchedDocument
from universal_docs_mcp.registries import PackageInfo
from universal_docs_mcp.retrieval import retrieve_document


@pytest.mark.parametrize("requested", ["Demo.Lib", "demo_lib", "DEMO-LIB"])
async def test_pypi_metadata_alias_is_served_and_cached_exactly(tmp_path, requested):
    calls = []

    async def package(*args):
        calls.append("metadata")
        return PackageInfo(
            name="demo-lib",
            ecosystem="python",
            latest_stable="1.2.3",
            description="Fixture",
        )

    async def docs(**kwargs):
        assert kwargs["package"] == "demo-lib"
        assert kwargs["version"] == "1.2.3"
        calls.append("docs")
        return FetchedDocument(
            "# Demo library\n## Usage\nExact package documentation",
            "pypi_description",
            "https://pypi.org/pypi/demo-lib/1.2.3/json",
        )

    cache = DocsCache(tmp_path)
    args = {"package": requested, "ecosystem": "python", "version": "1.2.3"}
    first = await retrieve_document(
        args, cache=cache, fetch_package_fn=package, fetch_document_fn=docs
    )
    second = await retrieve_document(
        args, cache=cache, fetch_package_fn=package, fetch_document_fn=docs
    )
    assert first is not None and second is not None
    assert first[0].name == "demo-lib"
    assert first[1] == second[1]
    assert first[1].startswith("# demo-lib v1.2.3 (python)")
    assert first[4]["document_version"] == second[4]["document_version"] == "1.2.3"
    assert second[3] is True
    assert calls == ["metadata", "docs"]
    cache.close()
