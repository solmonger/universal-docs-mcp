"""Exercise a fresh MCP process with the real SDK client, not handler mocks."""

import asyncio
import json
import os
import sys
from datetime import timedelta

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_real_stdio_manifest_errors_and_cache(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {
                    "safe": "1.2.3",
                    "private": "git+https://user:SYNTHETIC_STDIO_TOKEN@github.com/acme/demo.git",
                }
            }
        )
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "universal_docs_mcp.server"],
        cwd=str(tmp_path),
        env={
            "HOME": str(tmp_path),
            "PATH": os.defpath,
            "UNIVERSAL_DOCS_PROJECT_ROOT": str(tmp_path),
            "UNIVERSAL_DOCS_CACHE_DIR": str(tmp_path / "cache"),
        },
    )
    log_path = tmp_path / "stderr.log"
    with log_path.open("w") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=15)
            ) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "universal-docs"
                from universal_docs_mcp import __version__

                assert initialized.serverInfo.version == __version__
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "get_package_info",
                    "get_package_docs",
                    "get_docs_outline",
                    "get_project_dependencies",
                    "cache_stats",
                }
                manifest = await session.call_tool(
                    "get_project_dependencies", {"manifest_path": "package.json"}
                )
                assert manifest.isError is False
                payload = manifest.structuredContent
                assert payload == json.loads(manifest.content[0].text)
                assert payload["dependencies"][0]["pinned"] == "1.2.3"
                assert payload["dependencies"][1]["spec_redacted"] is True
                assert "SYNTHETIC_STDIO_TOKEN" not in manifest.model_dump_json()
                assert str(tmp_path) not in manifest.model_dump_json()
                invalid = await session.call_tool(
                    "get_package_docs",
                    {
                        "package": "demo",
                        "version": "../SYNTHETIC_INVALID_TOKEN",
                    },
                )
                assert invalid.isError is True
                assert invalid.structuredContent["error"] == "invalid_arguments"
                assert "SYNTHETIC_INVALID_TOKEN" not in invalid.model_dump_json()
                unknown = await session.call_tool("SYNTHETIC_UNKNOWN_TOOL_TOKEN", {})
                assert unknown.isError is True
                assert unknown.structuredContent["error"] == "unknown_tool"
                (tmp_path / "package.json").write_text(
                    json.dumps(
                        {
                            "dependencies": {
                                "https://user:SYNTHETIC_NAME_TOKEN@example.test/pkg": "1.2.3"
                            }
                        }
                    )
                )
                malformed = await session.call_tool(
                    "get_project_dependencies", {"manifest_path": "package.json"}
                )
                assert malformed.isError is True
                assert malformed.structuredContent["error"] == "manifest_error"
                assert "SYNTHETIC" not in malformed.model_dump_json()
                assert "SYNTHETIC" not in unknown.model_dump_json()
                (tmp_path / "package.json").write_text(
                    json.dumps(
                        {"dependencies": {f"safe{i}": "1.2.3" for i in range(350)}}
                    )
                )
                oversized = await session.call_tool(
                    "get_project_dependencies", {"manifest_path": "package.json"}
                )
                assert oversized.isError is True
                assert oversized.structuredContent["error"] == "response_too_large"
                assert (
                    len(oversized.model_dump_json(by_alias=True).encode("utf-8"))
                    <= 128 * 1024
                )
                stats = await session.call_tool("cache_stats", {})
                assert stats.structuredContent["available"] is True
                assert stats.structuredContent["total"] == 0
                await asyncio.wait_for(session.send_ping(), timeout=5)
    assert "SYNTHETIC" not in log_path.read_text()
    assert (
        "WARNING:mcp.server.lowlevel.server:dependency_diagnostic_redacted"
        in log_path.read_text()
    )
