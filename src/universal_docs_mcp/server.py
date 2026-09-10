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
import time
import httpx
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

from .registries import fetch_package, REGISTRY_MAP
from .docs_fetcher import fetch_docs_content_with_provenance
from .cache import DocsCache
from .compaction import compact, get_section, parse_sections, section_map, estimate_tokens
from .lockfile import read_pins

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

server = Server("universal-docs")
cache = DocsCache()


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_package_info",
            description=(
                "Get metadata for a package: latest stable version, description, "
                "docs URL, repository, license. Supports Python (PyPI), "
                "JavaScript/TypeScript (npm), and Rust (crates.io)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "package": {
                        "type": "string",
                        "description": "Package name (e.g., 'requests', 'express', 'serde')",
                    },
                    "ecosystem": {
                        "type": "string",
                        "description": "Language/ecosystem: python, javascript, typescript, rust. Auto-detected if omitted.",
                        "enum": list(set(REGISTRY_MAP.keys())),
                    },
                    "force_refresh": {
                        "type": "boolean",
                        "description": "Bypass the metadata cache and fetch current registry data.",
                    },
                },
                "required": ["package"],
            },
            annotations=types.ToolAnnotations(
                title="Get Package Info",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
            ),
        ),
        types.Tool(
            name="get_package_docs",
            description=(
                "Fetch actual documentation content for a package. Returns README "
                "or description text. Use get_package_info first to check version."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "package": {
                        "type": "string",
                        "description": "Package name",
                    },
                    "ecosystem": {
                        "type": "string",
                        "description": "Language/ecosystem (auto-detected if omitted)",
                        "enum": list(set(REGISTRY_MAP.keys())),
                    },
                    "version": {
                        "type": "string",
                        "description": "Exact package version. Omit for latest stable.",
                    },
                    "section": {
                        "type": "string",
                        "description": "Optional section slug/title from get_docs_outline.",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Approximate output budget for compact docs (default 1500, max 6000).",
                        "minimum": 200,
                        "maximum": 6000,
                    },
                    "force_refresh": {
                        "type": "boolean",
                        "description": "Bypass the raw-document cache and fetch current documentation.",
                    },
                },
                "required": ["package"],
            },
            annotations=types.ToolAnnotations(
                title="Get Package Docs",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
            ),
        ),
        types.Tool(
            name="get_docs_outline",
            description=(
                "Fetch current package documentation once and return a compact section map. "
                "Call this before requesting a specific section when the README is large. "
                "Supports an exact version so project-pinned docs are not confused with latest."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "package": {"type": "string", "description": "Package name"},
                    "ecosystem": {"type": "string", "description": "python, javascript/typescript, or rust"},
                    "version": {"type": "string", "description": "Exact version; omit for latest stable"},
                    "force_refresh": {
                        "type": "boolean",
                        "description": "Bypass the raw-document cache and fetch current documentation.",
                    },
                },
                "required": ["package"],
            },
            annotations=types.ToolAnnotations(
                title="Get Documentation Outline",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
            ),
        ),
        types.Tool(
            name="get_project_dependencies",
            description=(
                "Read a local requirements.txt, pyproject.toml, package.json, Cargo.toml, or Cargo.lock "
                "and return dependency versions/specs for pinned documentation lookup."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "manifest_path": {"type": "string", "description": "Path to a supported project manifest"},
                },
                "required": ["manifest_path"],
            },
            annotations=types.ToolAnnotations(
                title="Read Project Dependencies",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
        types.Tool(
            name="cache_stats",
            description="Get cache statistics (total entries, valid, expired).",
            inputSchema={"type": "object", "properties": {}},
            annotations=types.ToolAnnotations(
                title="Cache Stats",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        return await _dispatch_tool(name, arguments)
    except (httpx.HTTPError, ValueError) as exc:
        # Transport/invalid upstream data is unknown, not evidence of absence.
        return [types.TextContent(type="text", text=json.dumps({
            "found": None, "error": "upstream_unavailable",
            "exception_type": type(exc).__name__, "retryable": True,
        }))]


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
        return [types.TextContent(
            type="text",
            text=f"Package '{package}' not found" + (f" in {ecosystem}" if ecosystem else " in any registry"),
        )]

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
    """Load raw documentation once, with version-aware caching and provenance."""
    package = args["package"]
    ecosystem = args.get("ecosystem")
    version = args.get("version")
    info = await fetch_package(package, ecosystem)
    if not info:
        return None

    version = version or info.latest_stable
    cache_key = f"docsraw-v3:{info.ecosystem}:{info.name}:{version}"
    cached = None if args.get("force_refresh") else cache.get(cache_key)
    if cached and cached.get("content"):
        content = cached["content"]
        provenance = {
            "source": cached.get("source", "unknown_cached_source"),
            "source_url": cached.get("source_url", ""),
            "fetched_at": cached.get("fetched_at"),
        }
        was_cached = True
    else:
        fetched = await fetch_docs_content_with_provenance(
            package=info.name,
            ecosystem=info.ecosystem,
            docs_url=info.docs_url,
            repo_url=info.repository,
            version=version,
        )
        if not fetched:
            return info, "", "", False, {}
        content = fetched.content
        provenance = {"source": fetched.source, "source_url": fetched.source_url, "fetched_at": time.time()}
        cache.set(cache_key, {"content": content, **provenance})
        was_cached = False

    shown_version = version or info.latest_stable
    header = (
        f"# {info.name} v{shown_version} ({info.ecosystem})\n"
        f"License: {info.license or 'unknown'}\n"
        f"Source: {provenance['source']}\n"
    )
    if provenance.get("source_url"):
        header += f"Source URL: {provenance['source_url']}\n"
    if info.docs_url:
        header += f"Docs: {info.docs_url}\n"
    header += "\n---\n\n"
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
        "section_map": section_map(sections),
        "source": provenance.get("source"),
        "source_url": provenance.get("source_url"),
                "fetched_at": provenance.get("fetched_at"),
        "cached": was_cached,
        "next": "Call get_package_docs with section=<slug> for one section, or omit section for a compact view.",
    }, indent=2))]


async def _handle_project_dependencies(args: dict) -> list[types.TextContent]:
    path = args["manifest_path"]
    try:
        pins = read_pins(path)
    except (OSError, ValueError) as exc:
        return [types.TextContent(type="text", text=json.dumps({
            "found": False,
            "error": "manifest_error",
            "message": "Manifest could not be read or parsed; check format, path and permissions.",
        }, indent=2))]
    return [types.TextContent(type="text", text=json.dumps({
        "found": True,
        "manifest_path": path,
        "dependencies": [pin.to_dict() for pin in pins],
        "usage": "Use ecosystem, and pinned when present, with get_docs_outline or get_package_docs.",
    }, indent=2))]


async def amain():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main():
    asyncio.run(amain())


if __name__ == "__main__":
    main()
