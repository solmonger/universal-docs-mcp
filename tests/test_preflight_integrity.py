"""Receipt integrity faults found while integrating the preflight candidate."""

import json
import time
from dataclasses import asdict

import httpx

from universal_docs_mcp.cache import DocsCache
from universal_docs_mcp.docs_fetcher import FetchedDocument
from universal_docs_mcp.preflight import PreflightRequest, run_preflight
from universal_docs_mcp.registries import PackageInfo


async def test_latest_fallback_names_the_cached_document_version_not_metadata_latest(
    tmp_path,
):
    cache = DocsCache(tmp_path, ttl=1)
    cache.set(
        "docrequest-v4:python:demo:latest",
        {
            "content": "# Usage\nVersion one usage only",
            "source": "pypi_description",
            "source_url": "https://pypi.org/pypi/demo/1.0.0/json",
            "fetched_at": time.time() - 20,
            "version": "1.0.0",
            "package_info": asdict(PackageInfo("demo", "python", "2.0.0", "Demo")),
        },
    )

    async def offline(*args, **kwargs):
        raise httpx.ConnectError("offline")

    request = PreflightRequest(
        package="demo",
        ecosystem="python",
        selection="latest",
        query="usage",
        freshness_mode="allow_stale",
    )
    result = await run_preflight(request, cache=cache, fetch_package_fn=offline)
    assert result["found"] is True
    assert result["receipt"]["target"]["target_version"] == "1.0.0"
    assert result["receipt"]["target"]["latest_observed"] is None
    assert "v1.0.0" in result["context"]
    assert "v2.0.0" not in result["context"]


def test_cli_exit_reflects_emitted_oversize_error(monkeypatch, tmp_path):
    import io
    from types import SimpleNamespace

    from universal_docs_mcp import preflight

    request = {
        "package": "demo",
        "ecosystem": "python",
        "selection": "latest",
        "query": "usage",
        "freshness_mode": "require_check",
    }
    monkeypatch.setenv("UNIVERSAL_DOCS_CACHE_DIR", str(tmp_path))
    output = io.BytesIO()
    monkeypatch.setattr(
        preflight.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode())),
    )
    monkeypatch.setattr(preflight.sys, "stdout", SimpleNamespace(buffer=output))

    async def oversized(*args, **kwargs):
        return {"found": True, "context": "x" * preflight.MAX_OUTPUT_BYTES}

    monkeypatch.setattr(preflight, "_run_cli", oversized)
    code = preflight.main()
    emitted = json.loads(output.getvalue())
    assert emitted["error"] == "response_too_large"
    assert code != 0


def test_no_match_cli_is_not_success(monkeypatch, tmp_path):
    import io
    from types import SimpleNamespace

    from universal_docs_mcp import preflight

    request = {
        "package": "demo",
        "ecosystem": "python",
        "selection": "latest",
        "query": "no-such-section",
        "freshness_mode": "require_check",
    }
    monkeypatch.setenv("UNIVERSAL_DOCS_CACHE_DIR", str(tmp_path))
    output = io.BytesIO()
    monkeypatch.setattr(
        preflight.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode())),
    )
    monkeypatch.setattr(preflight.sys, "stdout", SimpleNamespace(buffer=output))

    async def package(*args, **kwargs):
        return PackageInfo("demo", "python", "1.0.0", "Demo")

    async def document(*args, **kwargs):
        return FetchedDocument(
            "# Usage\nHello",
            "pypi_description",
            "https://pypi.org/pypi/demo/1.0.0/json",
        )

    async def run(request, cache):
        return await run_preflight(
            request, cache=cache, fetch_package_fn=package, fetch_document_fn=document
        )

    monkeypatch.setattr(preflight, "_run_cli", run)
    code = preflight.main()
    emitted = json.loads(output.getvalue())
    assert emitted["found"] is True
    assert emitted["receipt"]["selection"]["no_match"] is True
    assert emitted["context"] == ""
    assert code != 0
