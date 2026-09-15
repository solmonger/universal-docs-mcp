"""Narrow, immutable official-source policy; no URL or latest aliases as input."""

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class OfficialSource:
    source_id: str
    version: str
    url: str
    retrieval_url: str
    version_binding: str = "versioned_url"


_TOOLS_URL = "https://modelcontextprotocol.io/specification/2026-07-28/server/tools"
SOURCES = MappingProxyType(
    {
        ("mcp-tools", "2026-07-28"): OfficialSource(
            "mcp-tools", "2026-07-28", _TOOLS_URL, _TOOLS_URL + ".md"
        ),
    }
)


def source_for(source_id: str, version: str) -> OfficialSource:
    try:
        return SOURCES[(source_id, version)]
    except (KeyError, TypeError):
        raise ValueError("unsupported_official_source") from None
