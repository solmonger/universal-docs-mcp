"""Fetch explicitly cataloged versioned documentation, never registry URLs."""

from .docs_fetcher import FetchedDocument
from .network import get_catalog_response
from .source_catalog import source_for


async def fetch_official_source(source_id: str, version: str) -> FetchedDocument | None:
    source = source_for(source_id, version)
    response = await get_catalog_response(source_id, version)
    if response.status_code == 404:
        return None
    content_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if content_type != "text/markdown":
        raise ValueError("invalid_official_document")
    try:
        content = response.content.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("invalid_official_document") from None
    if not content.strip():
        return None
    return FetchedDocument(
        content=content,
        source="official_markdown",
        source_url=source.retrieval_url,
    )
