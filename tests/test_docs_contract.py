"""Regression contracts found during the controller's integration check."""

import json
from types import SimpleNamespace

import httpx
import pytest

from universal_docs_mcp import server
from universal_docs_mcp.cache import DocsCache
from universal_docs_mcp.compaction import compact, parse_sections
from universal_docs_mcp.docs_fetcher import FetchedDocument


def test_fenced_code_and_html_prose_survive():
    text = "# Usage\n<p>Do not call twice.</p>\n```python\n# not a heading\n---\nprint(1)\n```\n## Next\nDone"
    sections = parse_sections(text)
    assert [s.title for s in sections] == ["Usage", "Next"]
    assert "# not a heading\n---\nprint(1)" in sections[0].body
    assert "Do not call twice." in sections[0].body
    assert "## Usage" in compact(text)["content"]


@pytest.mark.asyncio
async def test_latest_cache_does_not_relabel_old_docs(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "cache", DocsCache(tmp_path))
    release = ["1.0.0"]
    requested = []

    async def package(*a, **kw):
        return SimpleNamespace(
            name="demo",
            ecosystem="python",
            latest_stable=release[0],
            license=None,
            docs_url=None,
            repository=None,
        )

    async def fetch(*a, **kw):
        requested.append(kw["version"])
        return FetchedDocument(
            "docs for " + str(kw["version"]),
            "pypi_description",
            "https://pypi.org/example",
        )

    monkeypatch.setattr(server, "fetch_package", package)
    monkeypatch.setattr(server, "fetch_docs_content_with_provenance", fetch)
    first = await server._load_document({"package": "demo"})
    release[0] = "2.0.0"
    second = await server._load_document({"package": "demo"})
    assert requested == ["1.0.0", "2.0.0"]
    assert first is not None and second is not None
    assert first[2] != second[2]
    assert second[4]["fetched_at"] > 0


@pytest.mark.asyncio
async def test_targeted_section_obeys_content_budget(monkeypatch):
    info = SimpleNamespace(name="demo", ecosystem="python", latest_stable="1.0.0")

    async def load(*a, **kw):
        return (
            info,
            "header",
            "# Usage\n" + "x" * 5000,
            False,
            {"source": "test", "source_url": "https://example.org"},
        )

    monkeypatch.setattr(server, "_load_document", load)
    result = json.loads(
        (
            await server._handle_get_docs(
                {"package": "demo", "section": "usage", "max_tokens": 200}
            )
        )[0].text
    )
    assert len(result["content"]) <= 800
    assert result["truncated"] is True
    assert result["found"] is True


@pytest.mark.asyncio
async def test_invalid_budget_never_fetches(monkeypatch):
    async def load(*a, **kw):
        pytest.fail("invalid request must not fetch")

    monkeypatch.setattr(server, "_load_document", load)
    result = json.loads(
        (
            await server._handle_get_docs(
                {"package": "demo", "section": "usage", "max_tokens": False}
            )
        )[0].text
    )
    assert result["error"] == "invalid_max_tokens"


@pytest.mark.asyncio
async def test_network_error_is_unknown_not_not_found(monkeypatch):
    async def load(*a, **kw):
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(server, "_load_document", load)
    result = json.loads(
        (await server.call_tool("get_package_docs", {"package": "demo"}))[0].text
    )
    assert result["found"] is None
    assert result["error"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_registry_rate_limit_is_not_a_missing_package(monkeypatch):
    from universal_docs_mcp import registries

    transport = httpx.MockTransport(lambda req: httpx.Response(429, request=req))
    original = httpx.AsyncClient
    monkeypatch.setattr(
        registries.httpx,
        "AsyncClient",
        lambda **kw: original(transport=transport, **kw),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await registries.fetch_package("demo")


def test_blank_lines_in_code_preserved():
    section = parse_sections('# Usage\n```python\ns = """a\n\n\nb"""\n```')[0]
    assert "a\n\n\nb" in section.body
