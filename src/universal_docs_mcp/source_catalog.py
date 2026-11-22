"""Narrow, immutable official-source policy; no URL or latest aliases as input."""

import json
from dataclasses import dataclass
from importlib.resources import files
from types import MappingProxyType
from urllib.parse import urlsplit


@dataclass(frozen=True)
class OfficialSource:
    source_id: str
    version: str
    url: str
    retrieval_url: str
    version_binding: str = "versioned_url"
    content_types: tuple[str, ...] = ("text/markdown",)


def _load_sources() -> MappingProxyType:
    data = json.loads(
        files("universal_docs_mcp").joinpath("source_catalog.json").read_text("utf-8")
    )
    if not isinstance(data, list):
        raise ValueError("invalid_source_catalog")
    items = {}
    for row in data:
        if not isinstance(row, dict) or set(row) != {
            "source_id",
            "version",
            "url",
            "retrieval_url",
            "content_types",
        }:
            raise ValueError("invalid_source_catalog")
        source_id, version = row["source_id"], row["version"]
        if (
            not isinstance(source_id, str)
            or not source_id
            or not isinstance(version, str)
            or not version
        ):
            raise ValueError("invalid_source_catalog")
        urls = []
        for key in ("url", "retrieval_url"):
            url = row[key]
            try:
                parsed = urlsplit(url)
                valid = (
                    parsed.scheme == "https"
                    and parsed.hostname
                    and not parsed.username
                    and not parsed.password
                    and not parsed.query
                    and not parsed.fragment
                    and parsed.port in (None, 443)
                )
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise ValueError("invalid_source_catalog")
            urls.append(url)
        types = row["content_types"]
        if (
            not isinstance(types, list)
            or not types
            or any(t not in ("text/markdown", "text/plain") for t in types)
        ):
            raise ValueError("invalid_source_catalog")
        key = source_id, version
        if key in items:
            raise ValueError("invalid_source_catalog")
        items[key] = OfficialSource(
            source_id, version, *urls, content_types=tuple(types)
        )
    if items.get(("mcp-tools", "2026-07-28")) != OfficialSource(
        "mcp-tools",
        "2026-07-28",
        "https://modelcontextprotocol.io/specification/2026-07-28/server/tools",
        "https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md",
        content_types=("text/markdown",),
    ):
        raise ValueError("invalid_source_catalog")
    return MappingProxyType(items)


SOURCES = _load_sources()


def list_sources() -> tuple[OfficialSource, ...]:
    return tuple(SOURCES.values())


def source_for(source_id: str, version: str) -> OfficialSource:
    try:
        return SOURCES[(source_id, version)]
    except (KeyError, TypeError):
        raise ValueError("unsupported_official_source") from None
