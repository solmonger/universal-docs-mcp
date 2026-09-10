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
from pathlib import Path
import time
import weakref
import httpx
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

from .registries import fetch_package, PackageInfo
from .docs_fetcher import fetch_docs_content_with_provenance
from .cache import DocsCache
from .network import ResponseTooLarge, network_lifespan
from .compaction import compact, get_section, parse_sections, section_map, estimate_tokens
from .lockfile import read_pins, manifest_kind
from .validation import TOOL_ARGS, ALIASES
from pydantic import ValidationError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

server = Server("universal-docs")
cache = DocsCache()
TOOL_TIMEOUT = 45
MAX_CONCURRENT_TOOLS = 4
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
    return [types.Tool(
        name=name, description=description,
        inputSchema=TOOL_ARGS[name].model_json_schema(),
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                          idempotentHint=True,
                                          openWorldHint=name not in ("cache_stats", "get_project_dependencies")),
    ) for name, description in descriptions.items()]


class RetrievalMiss(Exception):
    def __init__(self, code):
        self.code = code


def _error(code: str, *, retryable: bool = False) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps({
        "found": None, "error": code, "retryable": retryable,
    }))]


@server.call_tool(validate_input=False)
async def mcp_call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    # The SDK's default validation echoes rejected values. Validate ourselves so
    # credential-bearing invalid input cannot leak through its error formatter.
    content = await call_tool(name, arguments)
    payload = json.loads(content[0].text)
    return types.CallToolResult(content=content, structuredContent=payload,
                                isError="error" in payload)


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
            result = await asyncio.wait_for(_dispatch_tool(name, arguments), timeout=TOOL_TIMEOUT)
        if sum(len(item.text.encode("utf-8")) for item in result) > 128 * 1024:
            return _error("response_too_large")
        return result
    except asyncio.TimeoutError:
        return _error("tool_timeout", retryable=True)
    except ResponseTooLarge:
        return _error("upstream_response_too_large")
    except RetrievalMiss as exc:
        return [types.TextContent(type="text", text=json.dumps({"found": False, "error": exc.code, "retryable": False}))]
    except httpx.HTTPError:
        return _error("upstream_unavailable", retryable=True)
    except ValueError:
        return _error("upstream_invalid", retryable=False)
    except Exception as exc:
        # Do not emit exception messages, URLs or traceback locals to stderr.
        logger.error("Tool failed (%s)", type(exc).__name__)
        return _error("internal_error")


async def _dispatch_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    if name == "get_package_info":
        return await _handle_get_info(arguments)
    elif name == "get_package_docs":
        return await _handle_get_docs(arguments)
    elif name == "get_docs_outline":
        return await _handle_get_outline(arguments)
    elif name == "get_project_dependencies":
        return await _handle_project_dependencies(arguments)
    elif name == "cache_stats":
        stats = cache.stats()
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
        return [types.TextContent(type="text", text=json.dumps({
            "found": False, "error": "package_not_found", "retryable": False,
        }))]

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
    """Exact TTL-valid docs need no fresh metadata; latest always resolves anew."""
    package = args["package"]
    ecosystem = args.get("ecosystem")
    requested = args.get("version")
    namespace = ALIASES.get(ecosystem, ecosystem) if ecosystem else "auto"
    request_key = f"docrequest-v4:{namespace}:{package}:{requested}"
    record = cache.get(request_key) if requested and not args.get("force_refresh") else None
    if record and record.get("content") and isinstance(record.get("package_info"), dict):
        info = PackageInfo(**record["package_info"])
        version = requested
        was_cached = True
        metadata_refreshed = False
    else:
        info = await fetch_package(package, ecosystem)
        if not info:
            return None
        version = requested or info.latest_stable
        if not version:
            raise RetrievalMiss("no_stable_release")
        cache_key = f"docsraw-v4:{info.ecosystem}:{info.name}:{version}"
        record = None if args.get("force_refresh") else cache.get(cache_key)
        metadata_refreshed = True
        was_cached = bool(record and record.get("content"))
        if not was_cached:
            fetched = await fetch_docs_content_with_provenance(
                package=info.name, ecosystem=info.ecosystem, docs_url=info.docs_url,
                repo_url=info.repository, version=version,
            )
            if not fetched:
                return info, "", "", False, {}
            if len(fetched.content.encode("utf-8")) > 1024 * 1024:
                raise ValueError("document_too_large")
            record = {"content": fetched.content, "source": fetched.source,
                      "source_url": fetched.source_url, "fetched_at": time.time(),
                      "package_info": {key: getattr(info, key, None) for key in PackageInfo.__dataclass_fields__}}
            cache.set(cache_key, record)
        if requested:
            cache.set(request_key, record)
    content = record["content"]
    provenance = {key: record.get(key) for key in ("source", "source_url", "fetched_at")}
    provenance["metadata_refreshed"] = metadata_refreshed
    provenance["version_binding"] = "unverified_git_ref" if provenance["source"] == "github_readme" else "registry_version"
    provenance["content_trust"] = "untrusted_upstream"
    header = (f"# {info.name} v{version} ({info.ecosystem})\n"
              f"Source: {provenance['source']}\n"
              f"Version binding: {provenance['version_binding']}\n"
              f"Source URL: {provenance['source_url']}\n\n---\n\n")
    return info, header, content, was_cached, provenance


async def _handle_get_docs(args: dict) -> list[types.TextContent]:
    requested_budget = args.get("max_tokens", 1500)
    if isinstance(requested_budget, bool) or not isinstance(requested_budget, int) or not 200 <= requested_budget <= 6000:
        return [types.TextContent(type="text", text=json.dumps({
            "found": False, "error": "invalid_max_tokens",
            "message": "max_tokens must be an integer from 200 through 6000",
        }))]
    loaded = await _load_document(args)
    if loaded is None:
        return [types.TextContent(type="text", text=json.dumps({"found": False, "error": "package_not_found"}))]
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
                "content": body[:requested_budget * 4],
                "truncated": len(body) > requested_budget * 4,
                "tokens_included": estimate_tokens(body[:requested_budget * 4]),
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
        result.update({
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
        })
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


async def _handle_get_outline(args: dict) -> list[types.TextContent]:
    loaded = await _load_document(args)
    if loaded is None:
        return [types.TextContent(type="text", text=json.dumps({"found": False, "error": "package_not_found"}))]
    info, header, raw, was_cached, provenance = loaded
    if not raw:
        return [types.TextContent(type="text", text=json.dumps({
            "found": False,
            "error": "documentation_not_found",
            "package": info.name,
            "ecosystem": info.ecosystem,
        }, indent=2))]
    sections = parse_sections(raw)
    return [types.TextContent(type="text", text=json.dumps({
        "found": True,
        "package": info.name,
        "ecosystem": info.ecosystem,
        "version": args.get("version") or info.latest_stable,
        "section_map": section_map(sections, offset=args.get("offset", 0), limit=args.get("limit", 100)),
        "section_map_total": len(sections),
        "next_offset": args.get("offset", 0) + args.get("limit", 100) if args.get("offset", 0) + args.get("limit", 100) < len(sections) else None,
        "source": provenance.get("source"),
        "source_url": provenance.get("source_url"),
                "fetched_at": provenance.get("fetched_at"),
                "metadata_refreshed": provenance.get("metadata_refreshed"),
                "version_binding": provenance.get("version_binding"),
                "content_trust": "untrusted_upstream",
        "cached": was_cached,
        "next": "Call get_package_docs with section=<slug> for one section, or omit section for a compact view.",
    }, indent=2))]


async def _handle_project_dependencies(args: dict) -> list[types.TextContent]:
    path = args["manifest_path"]
    root = os.environ.get("UNIVERSAL_DOCS_PROJECT_ROOT")
    if not root:
        return _error("manifest_access_disabled")
    try:
        pins = read_pins(path, root=Path(root))
    except (OSError, ValueError) as exc:
        return [types.TextContent(type="text", text=json.dumps({
            "found": False,
            "error": "manifest_error",
            "message": "Manifest could not be read or parsed; check format, path and permissions.",
        }, indent=2))]
    return [types.TextContent(type="text", text=json.dumps({
        "found": True,
        "manifest_kind": manifest_kind(Path(path)),
        "dependencies": [pin.to_dict() for pin in pins],
        "usage": "Use only registry_lookup=true dependencies for public-registry docs. A range is not an installed version; consult a lockfile. Markers are unevaluated.",
    }, indent=2))]


async def amain():
    try:
        async with network_lifespan(), mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        cache.close()


def main():
    asyncio.run(amain())


if __name__ == "__main__":
    main()
