"""Conditional HTTP freshness and exact-version cache behavior."""

import time
from dataclasses import asdict

import httpx

from universal_docs_mcp.cache import DocsCache
from universal_docs_mcp.network import network_lifespan
from universal_docs_mcp.registries import PackageInfo
from universal_docs_mcp.retrieval import retrieve_document


def _info(repository=None):
    return PackageInfo("demo", "python", "2.0.0", "Demo", repository=repository)


def _record(source="pypi_description", age=0):
    return {
        "content": "exact document",
        "source": source,
        "source_url": "https://pypi.org/pypi/demo/1.2.3/json"
        if source == "pypi_description"
        else "https://api.github.com/repos/acme/demo/readme?ref=1.2.3",
        "fetched_at": time.time() - age,
        "version": "1.2.3",
        "package_info": asdict(
            _info("https://github.com/acme/demo" if source == "github_readme" else None)
        ),
        "etag": '"old"',
    }


async def test_conditional_response_sends_validators_and_accepts_304(monkeypatch):
    from universal_docs_mcp import network

    seen = []

    async def handler(request):
        seen.append(request.headers)
        return httpx.Response(
            304, headers={"etag": '"same"', "content-encoding": "br"}, request=request
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        network.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(handler), **kw),
    )
    async with network_lifespan():
        response = await network.get_conditional_response(
            "https://pypi.org/pypi/demo/1.2.3/json",
            etag='"old"',
            last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
        )
    assert response.status_code == 304
    assert response.content == b""
    assert seen[0]["if-none-match"] == '"old"'
    assert seen[0]["if-modified-since"] == "Wed, 21 Oct 2015 07:28:00 GMT"


async def test_conditional_response_drops_crlf_validator(monkeypatch):
    from universal_docs_mcp import network

    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(200, request=request)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        network.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(handler), **kw),
    )
    async with network_lifespan():
        await network.get_conditional_response(
            "https://pypi.org/pypi/demo/1.2.3/json", etag='"abc\r\nX-Evil: 1"'
        )
    assert "if-none-match" not in seen[0].headers


def test_cache_touch_updates_json_and_sqlite_timestamp_without_size_change(tmp_path):
    cache = DocsCache(tmp_path)
    value = _record()
    cache.set("k", value)
    fetched = time.time() + 10
    assert cache.touch("k", fetched_at=fetched, etag='"new"')
    loaded = cache.get("k", ttl=1000)
    after = (
        cache._get_conn()
        .execute("SELECT value_bytes FROM docs_cache WHERE key='k'")
        .fetchone()[0]
    )
    assert loaded["fetched_at"] == fetched
    assert loaded["etag"] == '"new"'
    assert after > 0
    assert (
        after
        == cache._get_conn()
        .execute("SELECT length(CAST(value AS BLOB)) FROM docs_cache WHERE key='k'")
        .fetchone()[0]
    )
    cache.close()


async def test_force_refresh_304_avoids_full_document_fetch(tmp_path):
    cache = DocsCache(tmp_path)
    key = "docsraw-v4:python:demo:1.2.3"
    cache.set(key, _record())
    calls = []

    async def package(*args, **kwargs):
        return _info()

    async def conditional(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(304, headers={"etag": '"old"'})

    async def document(*args, **kwargs):
        raise AssertionError("304 must not download document")

    from universal_docs_mcp import retrieval

    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(retrieval, "get_conditional_response", conditional)
    loaded = await retrieve_document(
        {
            "package": "demo",
            "ecosystem": "python",
            "version": "1.2.3",
            "force_refresh": True,
        },
        cache=cache,
        fetch_package_fn=package,
        fetch_document_fn=document,
    )
    assert loaded[2] == "exact document"
    assert loaded[3] is False
    assert loaded[4]["metadata_refreshed"] is True
    assert calls
    monkeypatch.undo()
    cache.close()


async def test_immutable_registry_ttl_and_short_github_ttl(tmp_path):
    cache = DocsCache(tmp_path)
    cache.set("docrequest-v4:python:demo:1.2.3", _record(age=48 * 3600))

    async def offline(*args, **kwargs):
        raise AssertionError("registry exact version within immutable TTL should hit")

    loaded = await retrieve_document(
        {"package": "demo", "ecosystem": "python", "version": "1.2.3"},
        cache=cache,
        fetch_package_fn=offline,
    )
    assert loaded[3] is True
    cache.close()
