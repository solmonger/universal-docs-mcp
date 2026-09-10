"""Negative policy checks use fixture responses, never external execution."""

import httpx
import pytest

from universal_docs_mcp import network
from universal_docs_mcp.official_sources import fetch_official_source


@pytest.mark.parametrize(
    "source,version",
    [
        ("https://evil.test/secret", "2026-07-28"),
        ("mcp-tools", "latest"),
        ("mcp-tools", "2025-11-25"),
        ("mcp-tools", "../../private?token=SYNTHETIC_SECRET"),
        ("mcp-tools/../other", "2026-07-28"),
    ],
)
async def test_unsupported_catalog_entry_fails_before_network(
    monkeypatch, source, version
):
    def deny(**kwargs):
        pytest.fail("invalid catalog entry opened a client")

    monkeypatch.setattr(httpx, "AsyncClient", deny)
    with pytest.raises(ValueError, match="^unsupported_official_source$"):
        await fetch_official_source(source, version)


async def test_official_catalog_does_not_expand_generic_url_allowlist():
    with pytest.raises(ValueError, match="unsupported_upstream_url"):
        await network.get_response(
            "https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md"
        )


def fixture_client(monkeypatch, response):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.MockTransport(lambda request: response), **kw
        ),
    )


@pytest.mark.parametrize("status", [302, 429, 500])
async def test_redirect_and_unavailable_are_errors_not_missing(monkeypatch, status):
    fixture_client(
        monkeypatch, httpx.Response(status, headers={"location": "https://evil.test"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_official_source("mcp-tools", "2026-07-28")


async def test_official_missing_is_none(monkeypatch):
    fixture_client(monkeypatch, httpx.Response(404))
    assert await fetch_official_source("mcp-tools", "2026-07-28") is None


async def test_official_stream_is_bounded(monkeypatch):
    chunks = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                chunks.append(1)
                yield b"x" * 65536

    fixture_client(
        monkeypatch,
        httpx.Response(200, stream=Stream(), headers={"content-type": "text/markdown"}),
    )
    with pytest.raises(network.ResponseTooLarge):
        await fetch_official_source("mcp-tools", "2026-07-28")
    assert len(chunks) == 17


async def test_invalid_official_utf8_is_sanitized(monkeypatch):
    fixture_client(
        monkeypatch,
        httpx.Response(
            200,
            content=b"SYNTHETIC_SECRET\xff",
            headers={"content-type": "text/markdown"},
        ),
    )
    with pytest.raises(ValueError, match="^invalid_official_document$") as exc:
        await fetch_official_source("mcp-tools", "2026-07-28")
    assert "SYNTHETIC_SECRET" not in str(exc.value)
