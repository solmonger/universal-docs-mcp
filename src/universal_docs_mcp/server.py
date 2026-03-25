"""Universal Docs MCP Server.

Provides tools for fetching latest stable documentation for any package.

Tools:
  get_package_info    — Get metadata (version, docs URL, description)
  get_package_docs    — Get actual documentation content
  search_package      — Search across registries
  get_changelog       — Get recent changes/releases
"""

import asyncio
import json
import logging
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

from .registries import fetch_package, REGISTRY_MAP
from .docs_fetcher import fetch_docs_content
from .cache import DocsCache

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
                },
                "required": ["package"],
            },
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
                },
                "required": ["package"],
            },
        ),
        types.Tool(
            name="cache_stats",
            description="Get cache statistics (total entries, valid, expired).",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    if name == "get_package_info":
        return await _handle_get_info(arguments)
    elif name == "get_package_docs":
        return await _handle_get_docs(arguments)
    elif name == "cache_stats":
        stats = cache.stats()
        return [types.TextContent(type="text", text=json.dumps(stats, indent=2))]
    else:
        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


async def _handle_get_info(args: dict) -> list[types.TextContent]:
    package = args["package"]
    ecosystem = args.get("ecosystem")

    cache_key = f"info:{ecosystem or 'auto'}:{package}"
    cached = cache.get(cache_key)
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


async def _handle_get_docs(args: dict) -> list[types.TextContent]:
    package = args["package"]
    ecosystem = args.get("ecosystem")

    cache_key = f"docs:{ecosystem or 'auto'}:{package}"
    cached = cache.get(cache_key)
    if cached:
        return [types.TextContent(type="text", text=cached.get("content", "No docs cached"))]

    # First get package info
    info = await fetch_package(package, ecosystem)
    if not info:
        return [types.TextContent(
            type="text",
            text=f"Package '{package}' not found",
        )]

    content = await fetch_docs_content(
        package=info.name,
        ecosystem=info.ecosystem,
        docs_url=info.docs_url,
        repo_url=info.repository,
    )

    if not content:
        msg = f"No documentation content found for {info.name} ({info.ecosystem})"
        if info.docs_url:
            msg += f"\nDocs URL: {info.docs_url}"
        if info.repository:
            msg += f"\nRepository: {info.repository}"
        return [types.TextContent(type="text", text=msg)]

    header = (
        f"# {info.name} v{info.latest_stable} ({info.ecosystem})\n"
        f"License: {info.license or 'unknown'}\n"
    )
    if info.docs_url:
        header += f"Docs: {info.docs_url}\n"
    header += "\n---\n\n"

    full_content = header + content
    cache.set(cache_key, {"content": full_content})
    return [types.TextContent(type="text", text=full_content)]


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
