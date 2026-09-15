import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from universal_docs_mcp import docs_fetcher
from universal_docs_mcp.cache import DocsCache
from universal_docs_mcp.compaction import compact


@pytest.mark.asyncio
async def test_github_fallback_uses_requested_version(monkeypatch):
    seen = {}

    async def no_registry(package, version=None):
        return None

    async def github(repo_url, version=None):
        seen["version"] = version
        return "# exact docs"

    monkeypatch.setattr(docs_fetcher, "fetch_pypi_description", no_registry)
    monkeypatch.setattr(docs_fetcher, "fetch_readme_from_github", github)

    result = await docs_fetcher.fetch_docs_content_with_provenance(
        "demo", "python", repo_url="https://github.com/acme/demo", version="1.2.3"
    )

    assert result is not None
    assert seen["version"] == "1.2.3"
    assert result.source == "github_readme"
    assert "1.2.3" in result.source_url


@pytest.mark.asyncio
async def test_registry_content_is_not_clipped_before_compaction(monkeypatch):
    long_text = "x" * 5000

    import httpx

    async def response(*args, **kwargs):
        return httpx.Response(200, json={"info": {"description": long_text}})

    monkeypatch.setattr(docs_fetcher, "get_response", response)

    result = await docs_fetcher.fetch_pypi_description("demo")

    assert result == long_text
    assert len(result) == 5000


def test_compaction_never_exceeds_budget_with_large_header():
    result = compact("# Install\n\nrun", budget_tokens=20, header="H" * 200)

    assert result["tokens_included"] <= result["budget_tokens"]


def test_version_ranges_are_not_reported_as_pins():
    from universal_docs_mcp.lockfile import parse_package_json

    pins = parse_package_json(
        '{"dependencies": {"demo": "^1.2.3", "exact": "1.2.3", "tag": "latest"}}',
        "package.json",
    )

    values = {pin.name: pin.pinned for pin in pins}
    assert values == {"demo": None, "exact": "1.2.3", "tag": None}


@pytest.mark.asyncio
async def test_force_refresh_bypasses_raw_cache(monkeypatch):
    from universal_docs_mcp import server
    from universal_docs_mcp.docs_fetcher import FetchedDocument
    from universal_docs_mcp.registries import PackageInfo

    class Cache:
        def __init__(self):
            self.get_calls = 0
            self.saved = None

        def get(self, key):
            self.get_calls += 1
            return {"content": "stale", "source": "old", "source_url": "old-url"}

        def set(self, key, value):
            self.saved = value

    cache = Cache()
    monkeypatch.setattr(server, "cache", cache)

    async def package(*args, **kwargs):
        return PackageInfo(
            "demo", "python", "1.0.0", "", repository="https://github.com/acme/demo"
        )

    monkeypatch.setattr(server, "fetch_package", package)

    async def fresh(*args, **kwargs):
        return FetchedDocument(
            "fresh", "pypi_description", "https://pypi.org/pypi/demo/1.0.0/json"
        )

    monkeypatch.setattr(server, "fetch_docs_content_with_provenance", fresh)

    loaded = await server._load_document(
        {"package": "demo", "ecosystem": "python", "force_refresh": True}
    )

    assert loaded is not None
    assert loaded[2] == "fresh"
    assert cache.get_calls == 0
    assert cache.saved["source"] == "pypi_description"


def test_cache_ttl_marks_entries_expired():
    with TemporaryDirectory() as tmp:
        cache = DocsCache(Path(tmp), ttl=0)
        cache.set("demo", {"content": "docs"})
        assert cache.get("demo") is None
        assert cache.stats() == {"total": 1, "valid": 0, "expired": 1}
        cache.close()


@pytest.mark.asyncio
async def test_invalid_budget_is_a_structured_miss(monkeypatch):
    from universal_docs_mcp import server
    from universal_docs_mcp.registries import PackageInfo

    info = PackageInfo("demo", "python", "1.0.0", "")

    async def loaded(*args, **kwargs):
        return (
            info,
            "header",
            "raw",
            False,
            {"source": "test", "source_url": "test-url"},
        )

    monkeypatch.setattr(server, "_load_document", loaded)
    result = await server._handle_get_docs({"package": "demo", "max_tokens": 100})
    payload = json.loads(result[0].text)
    assert payload["error"] == "invalid_max_tokens"
