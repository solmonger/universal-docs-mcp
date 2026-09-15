"""Official catalog sources use the shared preflight receipt and selector."""

import json
import time

import httpx
import pytest

from universal_docs_mcp.cache import DocsCache
from universal_docs_mcp.docs_fetcher import FetchedDocument
from universal_docs_mcp.official_preflight import official_cache_key
from universal_docs_mcp.preflight import (
    OfficialPreflightRequest,
    parse_request,
    run_preflight,
)

OFFICIAL_REQUEST = {
    "source_id": "mcp-tools",
    "selection": "requested",
    "requested_version": "2026-07-28",
    "query": "tools/list",
    "context_max_bytes": 1200,
    "freshness_mode": "require_check",
}


def test_parse_request_selects_strict_official_request():
    request = parse_request(json.dumps(OFFICIAL_REQUEST).encode())

    assert isinstance(request, OfficialPreflightRequest)
    assert request.model_dump(exclude_defaults=True) == OFFICIAL_REQUEST
    assert not hasattr(request, "package")


@pytest.mark.asyncio
async def test_official_request_uses_shared_selection_and_neutral_receipt(tmp_path):
    calls = []

    async def fetch(source_id, version):
        calls.append((source_id, version))
        return FetchedDocument(
            "# Tools\n\nOverview.\n\n## tools/list\n\nList tools and tool schemas.",
            "official_markdown",
            "https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md",
        )

    request = OfficialPreflightRequest.model_validate(OFFICIAL_REQUEST)
    result = await run_preflight(
        request,
        cache=DocsCache(tmp_path),
        fetch_official_fn=fetch,
    )

    assert calls == [("mcp-tools", "2026-07-28")]
    assert result["found"] is True
    assert "List tools and tool schemas." in result["context"]
    assert result["receipt"]["target"] == {
        "package": None,
        "ecosystem": None,
        "source_id": "mcp-tools",
        "selection": "requested",
        "target_version": "2026-07-28",
        "requested_version": "2026-07-28",
        "installed_version": None,
        "installed_resolution": "not_applicable",
        "latest_observed": None,
    }
    assert result["receipt"]["source"] == {
        "kind": "official_markdown",
        "url": "https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md",
        "version_binding": "versioned_url",
        "content_sha256": result["receipt"]["source"]["content_sha256"],
        "content_bytes": len(
            "# Tools\n\nOverview.\n\n## tools/list\n\nList tools and tool schemas.".encode()
        ),
    }
    assert "tools-list" in result["receipt"]["selection"]["matched_sections"]


@pytest.mark.parametrize(
    "payload",
    [
        {**OFFICIAL_REQUEST, "source_id": "https://evil.test/docs"},
        {**OFFICIAL_REQUEST, "selection": "latest", "requested_version": None},
        {**OFFICIAL_REQUEST, "version": "2026-07-28"},
        {**OFFICIAL_REQUEST, "package": "fake-package"},
    ],
)
def test_official_request_rejects_unreviewed_or_ambiguous_inputs(payload):
    with pytest.raises(ValueError):
        parse_request(json.dumps(payload).encode())


@pytest.mark.asyncio
async def test_official_allow_cache_reuses_only_the_exact_snapshot(tmp_path):
    calls = 0

    async def fetch(source_id, version):
        nonlocal calls
        calls += 1
        return FetchedDocument(
            "# Tools\n\n## tools/list\n\nCached exact source.",
            "official_markdown",
            "https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md",
        )

    request = OfficialPreflightRequest.model_validate(
        {**OFFICIAL_REQUEST, "freshness_mode": "allow_cache"}
    )
    cache = DocsCache(tmp_path)
    first = await run_preflight(request, cache=cache, fetch_official_fn=fetch)
    second = await run_preflight(request, cache=cache, fetch_official_fn=fetch)

    assert calls == 1
    assert first["receipt"]["freshness"]["state"] == "upstream_checked"
    assert second["receipt"]["freshness"]["state"] == "cache_hit"
    assert second["receipt"]["freshness"]["checked_at"] is None
    assert second["receipt"]["target"]["target_version"] == "2026-07-28"
    assert (
        second["receipt"]["source"]["content_sha256"]
        == first["receipt"]["source"]["content_sha256"]
    )


@pytest.mark.asyncio
async def test_official_allow_stale_keeps_exact_identity_after_check_failure(tmp_path):
    content = "# Tools\n\n## tools/list\n\nOld exact source."
    cache = DocsCache(tmp_path, ttl=1)
    cache.set(
        official_cache_key("mcp-tools", "2026-07-28"),
        {
            "content": content,
            "source_id": "mcp-tools",
            "version": "2026-07-28",
            "source": "official_markdown",
            "source_url": "https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md",
            "version_binding": "versioned_url",
            "fetched_at": time.time() - 3600,
        },
    )

    async def offline(source_id, version):
        raise httpx.ConnectError("offline")

    request = OfficialPreflightRequest.model_validate(
        {**OFFICIAL_REQUEST, "freshness_mode": "allow_stale"}
    )
    result = await run_preflight(
        request,
        cache=cache,
        fetch_official_fn=offline,
    )

    assert result["found"] is True
    assert result["receipt"]["freshness"]["state"] == "stale_cache"
    assert result["receipt"]["freshness"]["stale"] is True
    assert result["receipt"]["freshness"]["stale_reason"] == "upstream_unavailable"
    assert result["receipt"]["target"]["target_version"] == "2026-07-28"
    assert result["receipt"]["target"]["latest_observed"] is None
    assert result["receipt"]["source"]["version_binding"] == "versioned_url"
    assert result["receipt"]["source"]["content_sha256"]


@pytest.mark.asyncio
async def test_official_missing_is_absence_with_known_exact_target(tmp_path):
    async def missing(source_id, version):
        return None

    request = OfficialPreflightRequest.model_validate(OFFICIAL_REQUEST)
    result = await run_preflight(
        request,
        cache=DocsCache(tmp_path),
        fetch_official_fn=missing,
    )

    assert result["found"] is False
    assert result["error"] == "documentation_not_found"
    assert result["context"] == ""
    assert result["receipt"]["target"]["source_id"] == "mcp-tools"
    assert result["receipt"]["target"]["target_version"] == "2026-07-28"
    assert result["receipt"]["source"]["url"].endswith("/server/tools.md")
    assert result["receipt"]["source"]["content_sha256"] is None


def test_official_no_match_is_not_a_usable_cli_result(monkeypatch, tmp_path):
    import io
    from types import SimpleNamespace

    from universal_docs_mcp import preflight

    output = io.BytesIO()
    monkeypatch.setenv("UNIVERSAL_DOCS_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        preflight.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(json.dumps(OFFICIAL_REQUEST).encode())),
    )
    monkeypatch.setattr(preflight.sys, "stdout", SimpleNamespace(buffer=output))

    async def no_match(request, cache):
        assert isinstance(request, OfficialPreflightRequest)
        return {
            "schema": "universal-docs.preflight/v1",
            "found": True,
            "context": "",
            "receipt": {"selection": {"no_match": True}},
        }

    monkeypatch.setattr(preflight, "_run_cli", no_match)
    assert preflight.main() == 1
    assert json.loads(output.getvalue())["receipt"]["selection"]["no_match"] is True
