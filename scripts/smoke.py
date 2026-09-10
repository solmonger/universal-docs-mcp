"""Inspect a fresh stdio server; opt into real public-registry retrieval explicitly."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import universal_docs_mcp


async def smoke(live: bool) -> dict:
    result = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "live": live,
        "python": sys.version,
        "package_version": importlib.metadata.version("universal-docs-mcp"),
        "mcp_version": importlib.metadata.version("mcp"),
        "module_directory": str(Path(universal_docs_mcp.__file__).parent),
        "module_sha256": {},
        "calls": [],
    }
    for path in sorted(Path(universal_docs_mcp.__file__).parent.glob("*.py")):
        result["module_sha256"][path.name] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    with tempfile.TemporaryDirectory(prefix="universal-docs-smoke-") as directory:
        root = Path(directory)
        (root / "package.json").write_text(
            json.dumps(
                {
                    "dependencies": {
                        "exact": "1.2.3",
                        "private": "git+https://user:SYNTHETIC_SMOKE_CANARY@github.com/acme/demo.git",
                    }
                }
            )
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "universal_docs_mcp.server"],
            cwd=directory,
            env={
                "HOME": directory,
                "PATH": os.defpath,
                "UNIVERSAL_DOCS_PROJECT_ROOT": directory,
                "UNIVERSAL_DOCS_CACHE_DIR": str(root / "cache"),
            },
        )
        with (root / "server.stderr").open("w+") as stderr:
            async with stdio_client(params, errlog=stderr) as (read, write):
                async with ClientSession(
                    read, write, read_timeout_seconds=timedelta(seconds=60)
                ) as session:
                    initialized = await session.initialize()
                    result["initialization"] = initialized.model_dump(mode="json")
                    tools = await session.list_tools()
                    result["tools"] = [
                        tool.model_dump(mode="json") for tool in tools.tools
                    ]
                    assert len(tools.tools) == 5
                    assert len({tool.name for tool in tools.tools}) == 5

                    async def call(name, args, expected_error=None):
                        response = await session.call_tool(name, args)
                        payload = response.structuredContent
                        assert isinstance(payload, dict)
                        assert json.loads(response.content[0].text) == payload
                        assert response.isError == (expected_error is not None), (
                            name,
                            payload,
                        )
                        if expected_error:
                            assert payload.get("error") == expected_error, payload
                        result["calls"].append(
                            {
                                "tool": name,
                                "arguments": args,
                                "isError": response.isError,
                                "payload": payload,
                            }
                        )
                        return payload

                    manifest = await call(
                        "get_project_dependencies", {"manifest_path": "package.json"}
                    )
                    assert manifest["dependencies"][0]["pinned"] == "1.2.3"
                    assert manifest["dependencies"][1]["spec_redacted"] is True
                    assert "SYNTHETIC_SMOKE_CANARY" not in json.dumps(manifest)
                    await call(
                        "get_package_docs",
                        {"package": "demo", "version": "../private"},
                        "invalid_arguments",
                    )
                    await call("missing_tool", {}, "unknown_tool")
                    if live:
                        for package, ecosystem in [
                            ("requests", "python"),
                            ("express", "npm"),
                            ("serde", "rust"),
                        ]:
                            info = await call(
                                "get_package_info",
                                {
                                    "package": package,
                                    "ecosystem": ecosystem,
                                    "force_refresh": True,
                                },
                            )
                            assert info["name"] == package and info["latest_stable"]
                        args = {
                            "package": "requests",
                            "ecosystem": "python",
                            "version": "2.32.3",
                        }
                        docs = await call(
                            "get_package_docs",
                            {**args, "force_refresh": True, "max_tokens": 600},
                        )
                        assert (
                            docs["found"]
                            and docs["content"]
                            and docs["cached"] is False
                        )
                        assert (
                            docs["version"] == "2.32.3"
                            and docs["version_binding"] == "registry_version"
                        )
                        assert (
                            docs["source_url"]
                            == "https://pypi.org/pypi/requests/2.32.3/json"
                        )
                        assert len(docs["content"]) <= 2400
                        outline = await call("get_docs_outline", args)
                        assert (
                            outline["cached"] is True
                            and outline["metadata_refreshed"] is False
                        )
                        assert outline["fetched_at"] == docs["fetched_at"]
                        sections = [
                            section
                            for section in outline["section_map"]
                            if section["chars"]
                        ]
                        assert sections
                        section = await call(
                            "get_package_docs",
                            {**args, "section": sections[0]["slug"], "max_tokens": 200},
                        )
                        assert (
                            section["found"]
                            and section["content"]
                            and len(section["content"]) <= 800
                        )
                        assert (
                            section["cached"]
                            and section["fetched_at"] == docs["fetched_at"]
                        )
                    stats = await call("cache_stats", {})
                    assert stats["available"] is True
                    if live:
                        assert stats["valid"] > 0
                    await session.send_ping()
            stderr.seek(0)
            server_logs = stderr.read()
            assert "SYNTHETIC_SMOKE_CANARY" not in server_logs
            assert "Traceback" not in server_logs
            result["server_stderr"] = server_logs
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    result["verified"] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Contact real public PyPI/npm/crates registries",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(smoke(args.live))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "verified": result["verified"],
                "artifact": str(args.output.resolve()),
                "calls": len(result["calls"]),
                "package_version": result["package_version"],
                "mcp_version": result["mcp_version"],
            }
        )
    )


if __name__ == "__main__":
    main()
