"""Bound upstream I/O and reject unsafe endpoint components."""

import httpx
import pytest

from universal_docs_mcp import docs_fetcher


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/github.com/acme/demo",
        "https://github.com.evil.test/acme/demo",
        "https://user:SYNTHETIC_SECRET@github.com/acme/demo",
        "https://github.com/acme/../private",
    ],
)
def test_github_fallback_requires_real_uncredentialed_repo(url):
    assert docs_fetcher._github_readme_url(url, "1.2.3") is None


@pytest.mark.parametrize(
    "package,version",
    [("demo", "../other?token=SYNTHETIC_SECRET"), ("../other", "1.2.3")],
)
async def test_fetcher_rejects_endpoint_injection(monkeypatch, package, version):
    def no_client(*args, **kwargs):
        pytest.fail("invalid input opened a network client")

    monkeypatch.setattr(httpx, "AsyncClient", no_client)
    with pytest.raises(ValueError):
        await docs_fetcher.fetch_pypi_description(package, version)


async def test_upstream_stream_is_bounded_without_content_length(monkeypatch):
    import importlib.util

    assert importlib.util.find_spec("universal_docs_mcp.network") is not None, (
        "bounded transport missing"
    )
    from universal_docs_mcp import network

    chunks = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(10):
                chunks.append(1)
                yield b"x" * 1024

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Stream(), request=request)
    )
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw)
    )
    with pytest.raises(ValueError, match="upstream_response_too_large"):
        await network.get_response("https://pypi.org/pypi/demo/json", max_bytes=2048)
    assert len(chunks) <= 3


async def test_registry_uses_bounded_transport(monkeypatch):
    from universal_docs_mcp import registries

    assert hasattr(registries, "get_response"), "registry bypasses bounded transport"
    seen = []

    async def response(url, **kwargs):
        seen.append(url)
        return httpx.Response(404)

    monkeypatch.setattr(registries, "get_response", response)
    assert await registries.fetch_npm("@scope/demo") is None
    assert seen == ["https://registry.npmjs.org/%40scope%2Fdemo"]


async def test_scoped_npm_miss_is_not_an_invalid_python_request(monkeypatch):
    from universal_docs_mcp import registries

    transport = httpx.MockTransport(
        lambda request: httpx.Response(404, request=request)
    )
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw)
    )
    assert await registries.fetch_package("@scope/missing") is None


async def test_broad_github_token_cannot_read_private_repository(monkeypatch):
    calls = []
    monkeypatch.setenv("GITHUB_TOKEN", "SYNTHETIC_BROAD_TOKEN")

    async def response(url, **kwargs):
        calls.append(url)
        if url.endswith("/readme?ref=1.2.3"):
            return httpx.Response(200, text="SYNTHETIC_PRIVATE_README")
        return httpx.Response(200, json={"private": True})

    monkeypatch.setattr(docs_fetcher, "get_response", response)
    result = await docs_fetcher.fetch_readme_from_github(
        "https://github.com/acme/private", "1.2.3"
    )
    assert result is None
    assert not any("/readme" in url for url in calls)


async def test_authenticated_public_repo_named_readme_uses_correct_metadata_endpoint(
    monkeypatch,
):
    calls = []
    monkeypatch.setenv("GITHUB_TOKEN", "SYNTHETIC_PUBLIC_TOKEN")

    async def response(url, **kwargs):
        calls.append(url)
        if url == "https://api.github.com/repos/acme/readme":
            return httpx.Response(200, json={"private": False})
        if url == "https://api.github.com/repos/acme/readme/readme?ref=1.2.3":
            return httpx.Response(200, text="# Public docs")
        return httpx.Response(404)

    monkeypatch.setattr(docs_fetcher, "get_response", response)
    result = await docs_fetcher.fetch_readme_from_github(
        "https://github.com/acme/readme", "1.2.3"
    )
    assert result == "# Public docs"
    assert calls[0] == "https://api.github.com/repos/acme/readme"
