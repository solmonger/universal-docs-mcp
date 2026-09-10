"""Run the current Python MCP client against a maintained-v1 stdio server.

This probe is intentionally an external-client gate. Run it from a disposable
environment containing the current ``mcp`` SDK, and point ``--server-python``
at the separately installed candidate server. It does not change the server's
runtime dependency bound or implement a protocol adapter.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CANARY = "SYNTHETIC_PROTOCOL_COMPAT_CANARY"
EXPECTED_LEGACY_VERSION = "2025-11-25"
EXPECTED_DISCOVERY_ERROR = -32602


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _attr(value: Any, *names: str) -> Any:
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _response_payload(response: Any) -> dict[str, Any]:
    payload = _attr(response, "structured_content", "structuredContent")
    if not isinstance(payload, dict):
        raise AssertionError("client returned no structured tool payload")
    if not response.content:
        raise AssertionError("client returned no text tool content")
    text_payload = json.loads(response.content[0].text)
    if text_payload != payload:
        raise AssertionError("structured and text tool payloads differ")
    return payload


def _tool_record(tool: Any) -> dict[str, Any]:
    annotations = getattr(tool, "annotations", None)
    input_schema = _attr(tool, "input_schema", "inputSchema") or {}
    output_schema = _attr(tool, "output_schema", "outputSchema")
    return {
        "name": tool.name,
        "has_output_schema": output_schema is not None,
        "input_additional_properties": input_schema.get("additionalProperties"),
        "read_only_hint": _attr(annotations, "read_only_hint", "readOnlyHint"),
        "open_world_hint": _attr(annotations, "open_world_hint", "openWorldHint"),
    }


def _server_parameters(
    *, server_python: Path, module: str, root: Path
) -> tuple[Any, dict[str, str]]:
    # Importing mcp is delayed until probe() so the module remains inspectable
    # from the maintained-v1 test environment.
    from mcp import StdioServerParameters

    env = {
        "HOME": str(root),
        "PATH": os.defpath,
        "UNIVERSAL_DOCS_PROJECT_ROOT": str(root),
        "UNIVERSAL_DOCS_CACHE_DIR": str(root / "cache"),
    }
    params = StdioServerParameters(
        command=str(server_python),
        args=["-m", module],
        cwd=str(root),
        env=env,
    )
    return params, env


async def _raw_discovery(
    *, server_python: Path, module: str, root: Path, log_path: Path
) -> dict[str, Any]:
    """Send only the modern discovery request and preserve its wire receipt."""

    _, env = _server_parameters(server_python=server_python, module=module, root=root)
    stderr = log_path.open("w")
    process = await asyncio.create_subprocess_exec(
        str(server_python),
        "-m",
        module,
        cwd=str(root),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr,
    )
    request = {
        "jsonrpc": "2.0",
        "id": "compat-discovery",
        "method": "server/discover",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {
                    "name": "universal-docs-protocol-gate",
                    "version": "1",
                },
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write((_json_bytes(request).decode() + "\n").encode())
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
        if not line:
            stderr.flush()
            raise AssertionError(
                f"server returned no discovery response (exit={process.returncode}, "
                f"stderr={log_path.read_text()!r})"
            )
        response = json.loads(line)
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)
        stderr.close()
    return {"request": request, "response": response}


async def probe(
    *, server_python: Path, module: str, live: bool, package: str, version: str
) -> dict[str, Any]:
    from mcp import Client
    from mcp.client.stdio import stdio_client

    result: dict[str, Any] = {
        "probe_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "client": {
            "python": sys.version,
            "python_executable": sys.executable,
            "mcp_version": importlib.metadata.version("mcp"),
        },
        "server": {
            "python_executable": str(server_python),
            "module": module,
        },
        "live": live,
        "catalog": {},
        "calls": [],
        "checks": {},
        "verified": False,
    }

    with tempfile.TemporaryDirectory(
        prefix="universal-docs-protocol-gate-"
    ) as directory:
        root = Path(directory)
        (root / "package.json").write_text(
            json.dumps(
                {
                    "dependencies": {
                        "safe": "1.2.3",
                        "private": f"git+https://user:{CANARY}@github.com/acme/demo.git",
                    }
                }
            )
        )
        discovery_log = root / "discovery.stderr"
        discovery = await _raw_discovery(
            server_python=server_python,
            module=module,
            root=root,
            log_path=discovery_log,
        )
        result["modern_discovery"] = discovery
        raw_response = discovery["response"]
        result["checks"]["modern_discovery_rejected_as_legacy"] = (
            raw_response.get("error", {}).get("code") == EXPECTED_DISCOVERY_ERROR
        )

        client_log = root / "client.stderr"
        params, _ = _server_parameters(
            server_python=server_python, module=module, root=root
        )
        client_failure: dict[str, str] | None = None
        client_stderr = ""
        with client_log.open("w+") as errlog:
            try:
                async with Client(
                    stdio_client(params, errlog=errlog),
                    mode="auto",
                    read_timeout_seconds=30,
                ) as client:
                    result["negotiation"] = {
                        "requested_mode": "auto",
                        "negotiated_protocol": client.protocol_version,
                        "server_info": client.server_info.model_dump(
                            mode="json", by_alias=True
                        )
                        if client.server_info
                        else None,
                    }
                    result["server"]["version"] = (
                        client.server_info.version if client.server_info else None
                    )
                    result["checks"]["auto_fell_back_to_legacy"] = (
                        client.protocol_version == EXPECTED_LEGACY_VERSION
                        and result["checks"]["modern_discovery_rejected_as_legacy"]
                    )

                    listings = [await client.list_tools() for _ in range(3)]
                    catalog_payloads = [
                        listing.model_dump(mode="json", by_alias=True)
                        for listing in listings
                    ]
                    catalog_hashes = [_sha256(payload) for payload in catalog_payloads]
                    tools = [_tool_record(tool) for tool in listings[0].tools]
                    result["catalog"] = {
                        "tool_names": [tool["name"] for tool in tools],
                        "tools": tools,
                        "sha256": catalog_hashes,
                    }
                    result["checks"]["catalog_order_and_schema_stable"] = (
                        len(set(catalog_hashes)) == 1
                        and [tool["name"] for tool in tools]
                        == [
                            "get_package_info",
                            "get_package_docs",
                            "get_docs_outline",
                            "get_project_dependencies",
                            "cache_stats",
                        ]
                        and all(tool["has_output_schema"] for tool in tools)
                        and all(
                            tool["input_additional_properties"] is False
                            and tool["read_only_hint"] is True
                            for tool in tools
                        )
                    )

                    stats = await client.call_tool("cache_stats", {})
                    stats_payload = _response_payload(stats)
                    assert not _attr(stats, "is_error", "isError")
                    result["calls"].append(
                        {
                            "tool": "cache_stats",
                            "is_error": False,
                            "available": stats_payload.get("available"),
                        }
                    )

                    manifest = await client.call_tool(
                        "get_project_dependencies", {"manifest_path": "package.json"}
                    )
                    manifest_payload = _response_payload(manifest)
                    assert not _attr(manifest, "is_error", "isError")
                    assert manifest_payload["dependencies"][1]["spec_redacted"] is True
                    manifest_wire = manifest.model_dump_json(by_alias=True)
                    result["calls"].append(
                        {
                            "tool": "get_project_dependencies",
                            "is_error": False,
                            "dependency_count": len(manifest_payload["dependencies"]),
                        }
                    )

                    invalid = await client.call_tool(
                        "get_package_docs",
                        {"package": "demo", "version": f"../{CANARY}"},
                    )
                    invalid_payload = _response_payload(invalid)
                    assert _attr(invalid, "is_error", "isError")
                    assert invalid_payload["error"] == "invalid_arguments"
                    invalid_wire = invalid.model_dump_json(by_alias=True)
                    result["calls"].append(
                        {
                            "tool": "get_package_docs",
                            "is_error": True,
                            "error": invalid_payload["error"],
                        }
                    )

                    unknown_error: Any | None = None
                    try:
                        await client.call_tool(CANARY, {})
                    except Exception as exc:  # protocol errors are client exceptions
                        unknown_error = exc
                    if unknown_error is None:
                        raise AssertionError("unknown tool returned a tool result")
                    error = getattr(unknown_error, "error", None)
                    error_code = getattr(error, "code", None)
                    error_message = getattr(error, "message", None)
                    result["checks"]["unknown_tool_is_wire_protocol_error"] = (
                        error_code == EXPECTED_DISCOVERY_ERROR
                        and error_message == "Invalid tool call"
                    )
                    result["calls"].append(
                        {
                            "tool": "unknown_tool",
                            "is_error": True,
                            "protocol_error_code": error_code,
                            "protocol_error_message": error_message,
                        }
                    )
                    unknown_text = str(unknown_error)

                    docs_wire = ""
                    if live:
                        docs_args = {
                            "package": package,
                            "ecosystem": "python",
                            "version": version,
                            "force_refresh": True,
                            "max_tokens": 400,
                        }
                        docs = await client.call_tool("get_package_docs", docs_args)
                        docs_payload = _response_payload(docs)
                        assert not _attr(docs, "is_error", "isError")
                        assert docs_payload["found"] is True
                        assert docs_payload["content"]
                        docs_wire = docs.model_dump_json(by_alias=True)
                        result["calls"].append(
                            {
                                "tool": "get_package_docs",
                                "is_error": False,
                                "found": docs_payload["found"],
                                "cached": docs_payload["cached"],
                                "version": docs_payload["version"],
                                "source": docs_payload["source"],
                                "content_sha256": _sha256(docs_payload["content"]),
                                "content_chars": len(docs_payload["content"]),
                            }
                        )
                        cached_docs = await client.call_tool(
                            "get_package_docs",
                            {
                                "package": package,
                                "ecosystem": "python",
                                "version": version,
                                "max_tokens": 400,
                            },
                        )
                        cached_payload = _response_payload(cached_docs)
                        assert not _attr(cached_docs, "is_error", "isError")
                        assert cached_payload["cached"] is True
                        result["calls"].append(
                            {
                                "tool": "get_package_docs",
                                "is_error": False,
                                "found": cached_payload["found"],
                                "cached": cached_payload["cached"],
                                "metadata_refreshed": cached_payload[
                                    "metadata_refreshed"
                                ],
                                "version": cached_payload["version"],
                            }
                        )

                    result["checks"]["wire_and_stderr_canary_absent"] = all(
                        CANARY not in value
                        for value in (
                            manifest_wire,
                            invalid_wire,
                            unknown_text,
                            docs_wire,
                        )
                    )
                    await client.session.send_ping()
            except Exception as exc:
                # Keep the raw discovery receipt when the modern client cannot
                # connect; do not turn a failed compatibility run into a pass.
                client_failure = {"type": type(exc).__name__}
            finally:
                errlog.seek(0)
                client_stderr = errlog.read()

        discovery_stderr = discovery_log.read_text()
        result["stderr"] = {
            "discovery_sha256": _sha256(discovery_stderr),
            "client_sha256": _sha256(client_stderr),
            "canary_absent": CANARY not in discovery_stderr + client_stderr,
            "diagnostic_redacted_present": "dependency_diagnostic_redacted"
            in discovery_stderr + client_stderr,
        }
        result["checks"]["stderr_canary_absent"] = result["stderr"]["canary_absent"]
        required_checks = {
            "modern_discovery_rejected_as_legacy",
            "auto_fell_back_to_legacy",
            "catalog_order_and_schema_stable",
            "unknown_tool_is_wire_protocol_error",
            "wire_and_stderr_canary_absent",
            "stderr_canary_absent",
        }
        result["checks"]["all_required_checks"] = (
            client_failure is None
            and required_checks <= result["checks"].keys()
            and all(result["checks"][name] for name in required_checks)
        )
        if client_failure is not None:
            result["client_failure"] = client_failure

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    result["verified"] = result["checks"].get("all_required_checks", False)
    if not result["verified"]:
        result["failure"] = "one or more compatibility checks failed"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server-python",
        type=Path,
        required=True,
        help="Python executable in the separately installed v1 server environment",
    )
    parser.add_argument(
        "--server-module", default="universal_docs_mcp.server", help="Server module"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Fetch the pinned public package README after offline checks",
    )
    parser.add_argument("--package", default="requests")
    parser.add_argument("--version", default="2.32.3")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    server_python = args.server_python.expanduser().absolute()
    if not server_python.is_file():
        parser.error(f"server executable does not exist: {server_python}")
    try:
        result = asyncio.run(
            probe(
                server_python=server_python,
                module=args.server_module,
                live=args.live,
                package=args.package,
                version=args.version,
            )
        )
    except Exception as exc:
        result = {
            "probe_version": 1,
            "client": {"python": sys.version, "python_executable": sys.executable},
            "server": {
                "python_executable": str(server_python),
                "module": args.server_module,
            },
            "verified": False,
            "failure": {"type": type(exc).__name__},
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "verified": result["verified"],
                "artifact": str(args.output.resolve()),
                "negotiated_protocol": result.get("negotiation", {}).get(
                    "negotiated_protocol"
                ),
                "calls": len(result.get("calls", [])),
            }
        )
    )
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
