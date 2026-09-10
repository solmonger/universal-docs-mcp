"""Bound upstream I/O and reject unsafe endpoint components."""
import httpx
import pytest

from universal_docs_mcp import docs_fetcher


@pytest.mark.parametrize('url', [
    'https://evil.test/github.com/acme/demo',
    'https://github.com.evil.test/acme/demo',
    'https://user:SYNTHETIC_SECRET@github.com/acme/demo',
    'https://github.com/acme/../private',
])
def test_github_fallback_requires_real_uncredentialed_repo(url):
    assert docs_fetcher._github_readme_url(url, '1.2.3') is None


@pytest.mark.parametrize('package,version', [('demo', '../other?token=SYNTHETIC_SECRET'), ('../other', '1.2.3')])
async def test_fetcher_rejects_endpoint_injection(monkeypatch, package, version):
    def no_client(*args, **kwargs):
        pytest.fail('invalid input opened a network client')
    monkeypatch.setattr(httpx, 'AsyncClient', no_client)
    with pytest.raises(ValueError):
        await docs_fetcher.fetch_pypi_description(package, version)


async def test_upstream_stream_is_bounded_without_content_length(monkeypatch):
    import importlib.util
    assert importlib.util.find_spec('universal_docs_mcp.network') is not None, 'bounded transport missing'
    from universal_docs_mcp import network
    chunks = []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(10):
                chunks.append(1)
                yield b'x' * 1024
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream(), request=request))
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))
    with pytest.raises(ValueError, match='upstream_response_too_large'):
        await network.get_response('https://pypi.org/pypi/demo/json', max_bytes=2048)
    assert len(chunks) <= 3
