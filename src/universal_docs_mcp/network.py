"""Allowlisted, bounded HTTP transport shared by registry and README fetches."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from urllib.parse import quote, urlsplit

import httpx

from . import __version__
from .source_catalog import source_for
from .validation import InfoArgs, OutlineArgs

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
ALLOWED_HOSTS = frozenset(
    {"pypi.org", "registry.npmjs.org", "crates.io", "api.github.com"}
)
_client: ContextVar[httpx.AsyncClient | None] = ContextVar(
    "docs_http_client", default=None
)


class ResponseTooLarge(ValueError):
    pass


def registry_url(package: str, ecosystem: str, version: str | None = None) -> str:
    if version is None:
        InfoArgs(package=package, ecosystem=ecosystem)
    else:
        OutlineArgs(package=package, ecosystem=ecosystem, version=version)
    name = quote(package, safe="")
    suffix = "/" + quote(version, safe="") if version is not None else ""
    if ecosystem == "python":
        return f"https://pypi.org/pypi/{name}{suffix}/json"
    if ecosystem == "javascript":
        return f"https://registry.npmjs.org/{name}{suffix}"
    if ecosystem == "rust":
        return f"https://crates.io/api/v1/crates/{name}{suffix}"
    raise ValueError("unsupported_ecosystem")


def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(15, connect=5, pool=5),
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
    )


@asynccontextmanager
async def network_lifespan():
    """Reuse connections in one server lifecycle; never share across event loops."""
    async with _new_client() as client:
        token = _client.set(client)
        try:
            yield
        finally:
            _client.reset(token)


async def _read(client, url, headers, max_bytes):
    async with client.stream("GET", url, headers=headers) as response:
        if response.status_code not in (200, 304, 404):
            response.raise_for_status()
        if response.status_code == 304:
            return httpx.Response(
                304, headers=response.headers, content=b"", request=response.request
            )
        # Request identity encoding and refuse unsolicited compression rather
        # than allocating an unbounded decoded gzip/brotli chunk before a cap.
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise ValueError("unsupported_content_encoding")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > max_bytes:
                raise ResponseTooLarge("upstream_response_too_large")
            body.extend(chunk)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=bytes(body),
            request=response.request,
        )


async def get_response(
    url: str, *, headers: dict | None = None, max_bytes: int = MAX_RESPONSE_BYTES
) -> httpx.Response:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in ALLOWED_HOSTS
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
        or parts.fragment
    ):
        raise ValueError("unsupported_upstream_url")
    return await _get_allowed_response(url, headers=headers, max_bytes=max_bytes)


def _valid_validator(value: str | None) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 256:
        return None
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value


async def get_conditional_response(
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    headers: dict | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> httpx.Response:
    conditional = dict(headers or {})
    valid_etag = _valid_validator(etag)
    valid_modified = _valid_validator(last_modified)
    if valid_etag is not None:
        conditional["If-None-Match"] = valid_etag
    if valid_modified is not None:
        conditional["If-Modified-Since"] = valid_modified
    return await get_response(url, headers=conditional, max_bytes=max_bytes)


async def get_catalog_response(
    source_id: str,
    version: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
) -> httpx.Response:
    """Fetch a fixed catalog path; this does not broaden the generic URL API."""
    source = source_for(source_id, version)
    headers = {"Accept": "text/markdown"}
    if _valid_validator(etag) is not None:
        headers["If-None-Match"] = etag
    if _valid_validator(last_modified) is not None:
        headers["If-Modified-Since"] = last_modified
    return await _get_allowed_response(
        source.retrieval_url, headers=headers, max_bytes=1024 * 1024
    )


async def _get_allowed_response(url: str, *, headers: dict | None, max_bytes: int):
    # Only policy-checked callers above reach this shared bounded transport.
    request_headers = {
        "User-Agent": f"universal-docs-mcp/{__version__}",
        **(headers or {}),
        "Accept-Encoding": "identity",
    }
    if urlsplit(url).hostname != "api.github.com" and any(
        k.lower() == "authorization" for k in request_headers
    ):
        raise ValueError("authorization_scope_violation")
    client = _client.get()
    if client is not None:
        return await asyncio.wait_for(
            _read(client, url, request_headers, max_bytes), timeout=20
        )
    async with _new_client() as client:
        return await asyncio.wait_for(
            _read(client, url, request_headers, max_bytes), timeout=20
        )
