"""Fallback keeps honest document identity through metadata/cache side effects."""

import time
from dataclasses import asdict

import httpx

from universal_docs_mcp.cache import DocsCache
from universal_docs_mcp.preflight import PreflightRequest, run_preflight
from universal_docs_mcp.registries import PackageInfo


def seed(cache, age):
    cache.set(
        "docrequest-v4:python:demo:latest",
        {
            "content": "# Usage\nVersion one usage only",
            "source": "pypi_description",
            "source_url": "https://pypi.org/pypi/demo/1.0.0/json",
            "fetched_at": time.time() - age,
            "version": "1.0.0",
            "package_info": asdict(PackageInfo("demo", "python", "1.0.0", "Demo")),
        },
    )


async def offline(*args, **kwargs):
    raise httpx.ConnectError("fixture offline")


def request():
    return PreflightRequest(
        package="demo",
        ecosystem="python",
        selection="latest",
        query="usage",
        freshness_mode="allow_stale",
    )


async def test_recent_cache_can_fallback_after_latest_check_failure(tmp_path):
    cache = DocsCache(tmp_path, ttl=3600)
    seed(cache, age=20)
    result = await run_preflight(request(), cache=cache, fetch_package_fn=offline)
    assert result["found"] is True
    assert result["receipt"]["freshness"]["state"] == "stale_cache"
    assert result["receipt"]["freshness"]["checked_at"] is None
    cache.close()


async def test_fallback_survives_concurrent_cache_pruning(tmp_path):
    cache = DocsCache(tmp_path, ttl=1)
    seed(cache, age=20)
    conn = cache._get_conn()
    conn.execute("UPDATE docs_cache SET fetched_at = ?", (time.time() - 20,))
    conn.commit()

    async def package(*args, **kwargs):
        # Another request writes while this request awaits upstream metadata.
        cache.set("concurrent-request", {"data": "fixture"})
        return PackageInfo("demo", "python", "2.0.0", "Demo")

    result = await run_preflight(
        request(), cache=cache, fetch_package_fn=package, fetch_document_fn=offline
    )
    assert result["found"] is True
    assert result["receipt"]["target"]["target_version"] == "1.0.0"
    assert result["receipt"]["freshness"]["state"] == "stale_cache"
    cache.close()
