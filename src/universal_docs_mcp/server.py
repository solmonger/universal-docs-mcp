"""Universal Docs MCP Server.

Provides tools for fetching latest stable documentation for any package.

Tools:
  get_package_info    — Get metadata (version, docs URL, description)
  get_package_docs    — Get actual documentation content
  get_docs_outline    — Inspect available README sections
  get_project_dependencies — Read local manifest constraints
  cache_stats         — Inspect cache counts
"""

import asyncio
import json
import logging
import os
import weakref
from pathlib import Path
from typing import Any

import httpx
import mcp.server.stdio
import mcp.types as types
from mcp.server import Server
from mcp.shared.exceptions import McpError
from pydantic import ValidationError

from . import __version__
from .cache import DocsCache
from .compaction import (
    compact,
    estimate_tokens,
    get_section,
    parse_sections,
    section_map,
)
from .contracts import RESULT_TYPES, output_schema
from .docs_fetcher import fetch_docs_content_with_provenance
from .lockfile import manifest_kind, read_pins
from .network import ResponseTooLarge, network_lifespan
from .registries import fetch_package
from .retrieval import retrieve_document
from .validation import TOOL_ARGS

logger = logging.getLogger(__name__)


class _SafeDiagnostics(logging.Filter):
    """Retain dependency event attribution/severity, never raw client text."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != logger.name:
            record.msg = "dependency_diagnostic_redacted"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def _configure_logging() -> None:
    # Only the standalone process owns logging. Importing the library does not
    # reconfigure its host. Structured tool errors remain the failure contract.
    handler = logging.StreamHandler()
    handler.addFilter(_SafeDiagnostics())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


server = Server("universal-docs", version=__version__)
cache = DocsCache()
TOOL_TIMEOUT = 45
MAX_CONCURRENT_TOOLS = 4
MAX_TOOL_RESULT_BYTES = 128 * 1024
_limiters = weakref.WeakKeyDictionary()


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    descriptions = {
        "get_package_info": "Get public PyPI/npm/crates.io package metadata and latest stable version (null if none).",
        "get_package_docs": "Fetch untrusted README/registry descriptions at an exact version or latest stable. Approximate content-only max_tokens budget, not full API search. Check version_binding and truncation before using examples.",
        "get_docs_outline": "Inspect untrusted versioned README sections with offset/limit pagination; use a returned slug with get_package_docs.",
        "get_project_dependencies": "Read a supported manifest under the configured UNIVERSAL_DOCS_PROJECT_ROOT. Disabled without a root. Nonregistry references are redacted; ranges are not installed pins.",
        "cache_stats": "Inspect cache entry counts and availability.",
    }
    return [
        types.Tool(
            name=name,
            description=description,
            inputSchema=TOOL_ARGS[name].model_json_schema(),
            outputSchema=output_schema(name),
            annotations=types.ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=name not in ("cache_stats", "get_project_dependencies"),
            ),
        )
        for name, description in descriptions.items()
    ]


class RetrievalMiss(Exception):
    def __init__(self, code):
        self.code = code


def _error(code: str, *, retryable: bool = False) -> list[types.TextContent]:
    return [
        types.TextContent(
            type="text",
            text=json.dumps(
                {
                    "found": None,
                    "error": code,
                    "retryable": retryable,
                }
            ),
        )
    ]


@server.call_tool(validate_input=False)
async def mcp_call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    # The SDK's default validation echoes rejected values. Validate ourselves so
    # credential-bearing invalid input cannot leak through its error formatter.
    content = await call_tool(name, arguments)
    payload = json.loads(content[0].text)
    result = types.CallToolResult(
        content=content, structuredContent=payload, isError="error" in payload
    )
    # Include both representations and their escaping, not only plain text.
    if (
        len(result.model_dump_json(by_alias=True).encode("utf-8"))
        > MAX_TOOL_RESULT_BYTES
    ):
        content = _error("response_too_large")
        return types.CallToolResult(
            content=content, structuredContent=json.loads(content[0].text), isError=True
        )
    if name in RESULT_TYPES:
        try:
            RESULT_TYPES[name].validate_python(payload)
        except ValidationError:
            # SDK schema diagnostics can include the invalid result itself.
            # Replace it before SDK-side validation; never echo the exception.
            logger.error("Tool result violated its output contract")
            content = _error("invalid_tool_result")
            return types.CallToolResult(
                content=content,
                structuredContent=json.loads(content[0].text),
                isError=True,
            )
    return result


# The maintained v1 decorator maps ordinary handler exceptions to an
# isError tool result. Unknown tools are request-shape errors instead, so gate
# them before that SDK wrapper. Keep this message fixed: neither tool names nor
# arguments belong in a protocol error or its logs.
_sdk_call_tool_handler = server.request_handlers[types.CallToolRequest]


async def _wire_call_tool_handler(request: types.CallToolRequest):
    if request.params.name not in TOOL_ARGS:
        raise McpError(
            types.ErrorData(code=types.INVALID_PARAMS, message="Invalid tool call")
        )
    return await _sdk_call_tool_handler(request)


server.request_handlers[types.CallToolRequest] = _wire_call_tool_handler


async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    model = TOOL_ARGS.get(name)
    if model is None:
        return _error("unknown_tool")
    try:
        arguments = model.model_validate(arguments).model_dump(exclude_none=True)
    except (ValidationError, ValueError):
        return _error("invalid_arguments")
    loop = asyncio.get_running_loop()
    limiter = _limiters.setdefault(loop, asyncio.Semaphore(MAX_CONCURRENT_TOOLS))
    if limiter.locked():
        return _error("server_busy", retryable=True)
    try:
        async with limiter:
            result = await asyncio.wait_for(
                _dispatch_tool(name, arguments), timeout=TOOL_TIMEOUT
            )
        if (
            sum(len(item.text.encode("utf-8")) for item in result)
            > MAX_TOOL_RESULT_BYTES
        ):
            return _error("response_too_large")
        return result
    except asyncio.TimeoutError:
        return _error("tool_timeout", retryable=True)
    except ResponseTooLarge:
        return _error("upstream_response_too_large")
    except RetrievalMiss as exc:
        return [
            types.TextContent(
                type="text",
                text=json.dumps(
                    {"found": False, "error": exc.code, "retryable": False}
                ),
            )
        ]
    except httpx.HTTPError:
        return _error("upstream_unavailable", retryable=True)
    except ValueError:
        return _error("upstream_invalid", retryable=False)
    except Exception as exc:
        # Do not emit exception messages, URLs or traceback locals to stderr.
        logger.error("Tool failed (%s)", type(exc).__name__)
        return _error("internal_error")


async def _dispatch_tool(
    name: str, arguments: dict[str, Any]
) -> list[types.TextContent]:
    if name == "get_package_info":
        return await _handle_get_info(arguments)
    elif name == "get_package_docs":
        return await _handle_get_docs(arguments)
    elif name == "get_docs_outline":
        return await _handle_get_outline(arguments)
    elif name == "get_project_dependencies":
        return await _handle_project_dependencies(arguments)
    elif name == "cache_stats":
        stats = cache.stats_with_availability()
        if not stats["available"]:
            stats.update(total=None, valid=None, expired=None)
        return [types.TextContent(type="text", text=json.dumps(stats, indent=2))]
    else:
        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


async def _handle_get_info(args: dict) -> list[types.TextContent]:
    package = args["package"]
    ecosystem = args.get("ecosystem")

    cache_key = f"info:{ecosystem or 'auto'}:{package}"
    cached = cache.get(cache_key) if not args.get("force_refresh") else None
    if cached:
        cached["_cached"] = True
        return [types.TextContent(type="text", text=json.dumps(cached, indent=2))]

    info = await fetch_package(package, ecosystem)
    if not info:
        return [
            types.TextContent(
                type="text",
                text=json.dumps(
                    {
                        "found": False,
                        "error": "package_not_found",
                        "retryable": False,
                    }
                ),
            )
        ]

    result = {
        "name": info.name,
        "ecosystem": info.ecosystem,
        "latest_stable": info.latest_stable,
        "description": info.description,
        "docs_url": info.docs_url,
        "repository": info.repository,
        "homepage": info.homepage,
        "license": info.license,
    }
    cache.set(cache_key, result)
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


async def _load_document(args: dict) -> tuple[Any, str, str, bool, dict] | None:
    """Compatibility wrapper around the SDK-neutral retrieval core."""
    try:
        return await retrieve_document(
            args,
            cache=cache,
            fetch_package_fn=fetch_package,
            fetch_document_fn=fetch_docs_content_with_provenance,
        )
    except ValueError as exc:
        if str(exc) == "no_stable_release":
            raise RetrievalMiss("no_stable_release") from None
        raise


async def _handle_get_docs(args: dict) -> list[types.TextContent]:
    requested_budget = args.get("max_tokens", 1500)
    if (
        isinstance(requested_budget, bool)
        or not isinstance(requested_budget, int)
        or not 200 <= requested_budget <= 6000
    ):
        return [
            types.TextContent(
                type="text",
                text=json.dumps(
                    {
                        "found": False,
                        "error": "invalid_max_tokens",
                        "message": "max_tokens must be an integer from 200 through 6000",
                    }
                ),
            )
        ]
    loaded = await _load_document(args)
    if loaded is None:
        return [
            types.TextContent(
                type="text",
                text=json.dumps({"found": False, "error": "package_not_found"}),
            )
        ]
    info, header, raw, was_cached, provenance = loaded
    if not raw:
        result = {
            "found": False,
            "error": "documentation_not_found",
            "package": info.name,
            "ecosystem": info.ecosystem,
            "version": args.get("version") or info.latest_stable,
            "docs_url": info.docs_url,
            "repository": info.repository,
        }
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    section_key = args.get("section")
    if section_key:
        section = get_section(parse_sections(raw), section_key)
        if section is None:
            result = {
                "found": False,
                "error": "section_not_found",
                "requested_section": section_key,
                "section_map": section_map(parse_sections(raw)),
            }
        else:
            body = f"{header}## {section.title}\n\n{section.body}"
            result = {
                "found": True,
                "content": body[: requested_budget * 4],
                "truncated": len(body) > requested_budget * 4,
                "tokens_included": estimate_tokens(body[: requested_budget * 4]),
                "budget_tokens": requested_budget,
                "package": info.name,
                "ecosystem": info.ecosystem,
                "version": args.get("version") or info.latest_stable,
                "section": section.to_dict(),
                "source": provenance.get("source"),
                "source_url": provenance.get("source_url"),
                "fetched_at": provenance.get("fetched_at"),
                "metadata_refreshed": provenance.get("metadata_refreshed"),
                "version_binding": provenance.get("version_binding"),
                "content_trust": "untrusted_upstream",
                "cached": was_cached,
            }
    else:
        result = compact(raw, budget_tokens=requested_budget, header=header)
        result.update(
            {
                "found": True,
                "package": info.name,
                "ecosystem": info.ecosystem,
                "version": args.get("version") or info.latest_stable,
                "source": provenance.get("source"),
                "source_url": provenance.get("source_url"),
                "fetched_at": provenance.get("fetched_at"),
                "metadata_refreshed": provenance.get("metadata_refreshed"),
                "version_binding": provenance.get("version_binding"),
                "content_trust": "untrusted_upstream",
                "cached": was_cached,
            }
        )
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


async def _handle_get_outline(args: dict) -> list[types.TextContent]:
    loaded = await _load_document(args)
    if loaded is None:
        return [
            types.TextContent(
                type="text",
                text=json.dumps({"found": False, "error": "package_not_found"}),
            )
        ]
    info, header, raw, was_cached, provenance = loaded
    if not raw:
        return [
            types.TextContent(
                type="text",
                text=json.dumps(
                    {
                        "found": False,
                        "error": "documentation_not_found",
                        "package": info.name,
                        "ecosystem": info.ecosystem,
                    },
                    indent=2,
                ),
            )
        ]
    sections = parse_sections(raw)
    return [
        types.TextContent(
            type="text",
            text=json.dumps(
                {
                    "found": True,
                    "package": info.name,
                    "ecosystem": info.ecosystem,
                    "version": args.get("version") or info.latest_stable,
                    "section_map": section_map(
                        sections,
                        offset=args.get("offset", 0),
                        limit=args.get("limit", 100),
                    ),
                    "section_map_total": len(sections),
                    "next_offset": args.get("offset", 0) + args.get("limit", 100)
                    if args.get("offset", 0) + args.get("limit", 100) < len(sections)
                    else None,
                    "source": provenance.get("source"),
                    "source_url": provenance.get("source_url"),
                    "fetched_at": provenance.get("fetched_at"),
                    "metadata_refreshed": provenance.get("metadata_refreshed"),
                    "version_binding": provenance.get("version_binding"),
                    "content_trust": "untrusted_upstream",
                    "cached": was_cached,
                    "next": "Call get_package_docs with section=<slug> for one section, or omit section for a compact view.",
                },
                indent=2,
            ),
        )
    ]


async def _handle_project_dependencies(args: dict) -> list[types.TextContent]:
    path = args["manifest_path"]
    root = os.environ.get("UNIVERSAL_DOCS_PROJECT_ROOT")
    if not root:
        return _error("manifest_access_disabled")
    try:
        pins = read_pins(path, root=Path(root))
    except (OSError, ValueError):
        return [
            types.TextContent(
                type="text",
                text=json.dumps(
                    {
                        "found": False,
                        "error": "manifest_error",
                        "message": "Manifest could not be read or parsed; check format, path and permissions.",
                    },
                    indent=2,
                ),
            )
        ]
    return [
        types.TextContent(
            type="text",
            text=json.dumps(
                {
                    "found": True,
                    "manifest_kind": manifest_kind(Path(path)),
                    "dependencies": [pin.to_dict() for pin in pins],
                    "usage": "Use only registry_lookup=true dependencies for public-registry docs. A range is not an installed version; consult a lockfile. Markers are unevaluated.",
                },
                indent=2,
            ),
        )
    ]


async def amain():
    try:
        async with (
            network_lifespan(),
            mcp.server.stdio.stdio_server() as (read_stream, write_stream),
        ):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )
    finally:
        cache.close()


def main():
    _configure_logging()
    asyncio.run(amain())


if __name__ == "__main__":
    main()
