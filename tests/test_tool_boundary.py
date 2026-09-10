"""Tool boundary contracts independent of client-side validation."""

import json

import pytest

from universal_docs_mcp import server


@pytest.mark.parametrize(
    "name,args",
    [
        ("get_package_docs", {}),
        ("get_package_docs", {"package": ""}),
        ("get_package_info", {"package": "../private?token=SYNTHETIC_SECRET"}),
        (
            "get_package_docs",
            {"package": "demo", "version": "../other?token=SYNTHETIC_SECRET"},
        ),
        ("get_package_docs", {"package": "demo", "version": "latest"}),
        (
            "get_package_docs",
            {"package": "demo", "ecosystem": "python", "version": "^1.0"},
        ),
        ("get_package_docs", {"package": "demo", "ecosystem": "npm", "version": "1.2"}),
        ("get_docs_outline", {"package": "demo", "ecosystem": "unsupported"}),
        ("get_package_info", {"package": "demo", "force_refresh": "false"}),
        ("get_package_docs", {"package": "demo", "max_tokens": True}),
        ("get_package_docs", {"package": "demo", "extra": "SYNTHETIC_SECRET"}),
        ("cache_stats", {"extra": "SYNTHETIC_SECRET"}),
    ],
)
async def test_bad_arguments_rejected_before_dispatch(monkeypatch, name, args):
    async def dispatch(*a, **kw):
        pytest.fail("invalid input reached I/O")

    monkeypatch.setattr(server, "_dispatch_tool", dispatch)
    result = await server.call_tool(name, args)
    payload = json.loads(result[0].text)
    assert payload["error"] == "invalid_arguments"
    assert payload["retryable"] is False
    assert "SYNTHETIC" not in result[0].text


async def test_unknown_tool_is_safe_json():
    result = await server.call_tool("SYNTHETIC_SECRET", {})
    assert json.loads(result[0].text)["error"] == "unknown_tool"
    assert "SYNTHETIC" not in result[0].text


async def test_execution_failure_sets_protocol_error(monkeypatch):
    import mcp.types as types

    async def broken(*args):
        return [
            types.TextContent(
                type="text",
                text=json.dumps({"found": None, "error": "upstream_unavailable"}),
            )
        ]

    monkeypatch.setattr(server, "call_tool", broken)
    assert hasattr(server, "mcp_call_tool"), "protocol wrapper missing"
    result = await server.mcp_call_tool("get_package_docs", {"package": "demo"})
    assert result.isError is True
    assert result.structuredContent == json.loads(result.content[0].text)


async def test_unexpected_error_does_not_echo_exception(monkeypatch):
    async def broken(*a):
        raise KeyError("SYNTHETIC_SECRET")

    monkeypatch.setattr(server, "_dispatch_tool", broken)
    result = await server.call_tool("get_package_info", {"package": "demo"})
    assert json.loads(result[0].text)["error"] == "internal_error"


async def test_tool_deadline_returns_safe_retryable_error(monkeypatch):
    import asyncio

    assert hasattr(server, "TOOL_TIMEOUT"), "tool deadline missing"
    monkeypatch.setattr(server, "TOOL_TIMEOUT", 0.01)

    async def stalled(*a):
        await asyncio.Event().wait()

    monkeypatch.setattr(server, "_dispatch_tool", stalled)
    result = await server.call_tool("cache_stats", {})
    assert json.loads(result[0].text)["error"] == "tool_timeout"


async def test_concurrency_backpressure_is_immediate(monkeypatch):
    import asyncio

    assert hasattr(server, "MAX_CONCURRENT_TOOLS"), "admission limit missing"
    entered = 0
    ready = asyncio.Event()
    release = asyncio.Event()

    async def stalled(*a):
        nonlocal entered
        entered += 1
        if entered == server.MAX_CONCURRENT_TOOLS:
            ready.set()
        await release.wait()
        return server._error("synthetic")

    monkeypatch.setattr(server, "_dispatch_tool", stalled)
    tasks = [
        asyncio.create_task(server.call_tool("cache_stats", {}))
        for _ in range(server.MAX_CONCURRENT_TOOLS)
    ]
    try:
        await asyncio.wait_for(ready.wait(), 1)
        result = await server.call_tool("cache_stats", {})
        assert json.loads(result[0].text)["error"] == "server_busy"
    finally:
        release.set()
        await asyncio.gather(*tasks)


async def test_tool_payload_has_hard_size_limit(monkeypatch):
    import mcp.types as types

    async def huge(*a):
        return [
            types.TextContent(
                type="text", text=json.dumps({"content": "x" * (256 * 1024)})
            )
        ]

    monkeypatch.setattr(server, "_dispatch_tool", huge)
    result = await server.call_tool("cache_stats", {})
    assert json.loads(result[0].text)["error"] == "response_too_large"

    assert "SYNTHETIC" not in result[0].text
