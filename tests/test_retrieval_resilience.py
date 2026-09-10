import json

import httpx
import pytest

from universal_docs_mcp import server
from universal_docs_mcp.cache import DocsCache
from universal_docs_mcp.docs_fetcher import FetchedDocument
from universal_docs_mcp.registries import PackageInfo
from universal_docs_mcp.compaction import compact


async def test_exact_cache_works_without_registry_and_marks_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(server, 'cache', DocsCache(tmp_path))
    async def package(*a, **kw):
        return PackageInfo('demo', 'python', '1.2.3', '')
    async def docs(*a, **kw):
        return FetchedDocument('# Usage\nUse demo.', 'pypi_description', 'https://pypi.org/pypi/demo/1.2.3/json')
    monkeypatch.setattr(server, 'fetch_package', package)
    monkeypatch.setattr(server, 'fetch_docs_content_with_provenance', docs)
    args = {'package': 'demo', 'ecosystem': 'python', 'version': '1.2.3'}
    first = json.loads((await server.call_tool('get_package_docs', args))[0].text)
    async def offline(*a, **kw):
        raise httpx.ConnectError('SYNTHETIC_OFFLINE')
    monkeypatch.setattr(server, 'fetch_package', offline)
    second = json.loads((await server.call_tool('get_package_docs', args))[0].text)
    assert second['found'] is True
    assert second['cached'] is True
    assert second['metadata_refreshed'] is False
    assert second['fetched_at'] == first['fetched_at']
    assert second['version_binding'] == 'registry_version'
    refreshed = json.loads((await server.call_tool('get_package_docs', {**args, 'force_refresh': True}))[0].text)
    assert refreshed['error'] == 'upstream_unavailable'


async def test_no_stable_release_does_not_fall_back_to_prerelease(monkeypatch, tmp_path):
    monkeypatch.setattr(server, 'cache', DocsCache(tmp_path))
    async def package(*a, **kw):
        return PackageInfo('demo', 'python', None, '')
    async def docs(*a, **kw):
        pytest.fail('no stable version must not fetch unversioned docs')
    monkeypatch.setattr(server, 'fetch_package', package)
    monkeypatch.setattr(server, 'fetch_docs_content_with_provenance', docs)
    result = json.loads((await server.call_tool('get_package_docs', {'package': 'demo'}))[0].text)
    assert result['error'] == 'no_stable_release'


def test_compact_metadata_is_bounded_and_honest():
    result = compact('\n'.join('## Section ' + str(i) + '\nbody' for i in range(1000)), budget_tokens=200)
    assert len(result['section_map']) <= 100
    assert len(result['sections_omitted']) <= 100
    assert result['section_map_total'] == 1000
    assert result['section_map_truncated'] is True
    assert len(json.dumps(result)) < 65536


async def test_outline_paginates_section_map(monkeypatch):
    async def load(*a, **kw):
        return (PackageInfo('demo', 'python', '1.0.0', ''), '', '\n'.join('## Section ' + str(i) for i in range(250)), False, {})
    monkeypatch.setattr(server, '_load_document', load)
    result = json.loads((await server.call_tool('get_docs_outline', {'package': 'demo', 'offset': 100, 'limit': 50}))[0].text)
    assert len(result['section_map']) == 50
    assert result['section_map'][0]['slug'] == 'section-100'
    assert result['next_offset'] == 150
    assert result['section_map_total'] == 250


def test_long_heading_metadata_cannot_bypass_budget():
    result = compact('## ' + 'x' * 100000 + '\nbody', budget_tokens=200)
    assert len(json.dumps(result)) < 65536
    assert len(result['section_map'][0]['slug']) <= 128
    assert result['section_map'][0]['title_truncated'] is True

