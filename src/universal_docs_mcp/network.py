"""Allowlisted, bounded HTTP transport shared by registry and README fetches."""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from urllib.parse import quote, urlsplit

import httpx

from .validation import InfoArgs, OutlineArgs

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
ALLOWED_HOSTS = frozenset({'pypi.org', 'registry.npmjs.org', 'crates.io', 'api.github.com'})
_client: ContextVar[httpx.AsyncClient | None] = ContextVar('docs_http_client', default=None)


class ResponseTooLarge(ValueError):
    pass


def registry_url(package: str, ecosystem: str, version: str | None = None) -> str:
    if version is None:
        InfoArgs(package=package, ecosystem=ecosystem)
    else:
        OutlineArgs(package=package, ecosystem=ecosystem, version=version)
    name = quote(package, safe='')
    suffix = '/' + quote(version, safe='') if version is not None else ''
    if ecosystem == 'python':
        return f'https://pypi.org/pypi/{name}{suffix}/json'
    if ecosystem == 'javascript':
        return f'https://registry.npmjs.org/{name}{suffix}'
    if ecosystem == 'rust':
        return f'https://crates.io/api/v1/crates/{name}{suffix}'
    raise ValueError('unsupported_ecosystem')


def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(15, connect=5, pool=5),
        follow_redirects=False, trust_env=False,
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
    async with client.stream('GET', url, headers=headers) as response:
        if response.status_code != 404:
            response.raise_for_status()
        # Request identity encoding and refuse unsolicited compression rather
        # than allocating an unbounded decoded gzip/brotli chunk before a cap.
        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
            raise ValueError('unsupported_content_encoding')
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > max_bytes:
                raise ResponseTooLarge('upstream_response_too_large')
            body.extend(chunk)
        return httpx.Response(response.status_code, headers=response.headers,
                              content=bytes(body), request=response.request)


async def get_response(url: str, *, headers: dict | None = None,
                       max_bytes: int = MAX_RESPONSE_BYTES) -> httpx.Response:
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.hostname not in ALLOWED_HOSTS
            or parts.username or parts.password or parts.port not in (None, 443) or parts.fragment):
        raise ValueError('unsupported_upstream_url')
    request_headers = {'User-Agent': 'universal-docs-mcp/0.2.0', **(headers or {}),
                       'Accept-Encoding': 'identity'}
    if parts.hostname != 'api.github.com' and any(k.lower() == 'authorization' for k in request_headers):
        raise ValueError('authorization_scope_violation')
    client = _client.get()
    if client is not None:
        return await asyncio.wait_for(_read(client, url, request_headers, max_bytes), timeout=20)
    async with _new_client() as client:
        return await asyncio.wait_for(_read(client, url, request_headers, max_bytes), timeout=20)
