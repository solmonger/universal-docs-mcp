"""Hermetic regression tests for stable registry metadata selection."""

import httpx
import pytest

from universal_docs_mcp import registries


@pytest.fixture
def mock_registry(monkeypatch):
    """Route every registry request through an httpx MockTransport."""
    original = registries.httpx.AsyncClient

    def install(handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(
            registries.httpx,
            "AsyncClient",
            lambda **kwargs: original(transport=transport, **kwargs),
        )

    return install


@pytest.mark.asyncio
async def test_pypi_ignores_prerelease_dev_and_yanked_only_releases(mock_registry):
    payload = {
        "info": {
            "version": "3.0.0",
            "summary": "demo",
            "project_urls": None,
        },
        "releases": {
            "3.0.0": [{"yanked": True}],
            "2.0.0rc1": [{"yanked": False}],
            "1.5.0.dev1": [{"yanked": False}],
            "1.4.0": [{"yanked": False}],
        },
    }

    def handler(request):
        return httpx.Response(200, json=payload, request=request)

    mock_registry(handler)
    info = await registries.fetch_pypi("demo")

    assert info is not None
    assert info.latest_stable == "1.4.0"


@pytest.mark.asyncio
async def test_pypi_does_not_fallback_to_prerelease_info_version(mock_registry):
    payload = {
        "info": {"version": "2.0.0rc1", "summary": None},
        "releases": {
            "2.0.0rc1": [{"yanked": False}],
            "1.0.0": [{"yanked": True}],
        },
    }

    def handler(request):
        return httpx.Response(200, json=payload, request=request)

    mock_registry(handler)
    info = await registries.fetch_pypi("demo")

    assert info is not None
    assert info.latest_stable is None
    assert info.description == ""


@pytest.mark.asyncio
async def test_pypi_does_not_invent_available_release_when_files_are_missing(
    mock_registry,
):
    payload = {
        "info": {
            "version": "1.2.3",
            "summary": "demo",
            "home_page": None,
            "project_urls": None,
            "license": None,
        },
        "releases": {},
    }

    def handler(request):
        return httpx.Response(200, json=payload, request=request)

    mock_registry(handler)
    info = await registries.fetch_pypi("demo")

    assert info is not None
    assert info.latest_stable is None
    assert info.homepage is None
    assert info.docs_url is None
    assert info.repository is None
    assert info.license is None


@pytest.mark.asyncio
async def test_npm_falls_back_from_prerelease_latest_to_highest_stable(mock_registry):
    payload = {
        "description": "demo",
        "dist-tags": {"latest": "2.0.0-beta.1"},
        "versions": {
            "1.9.0": {"homepage": None, "license": None},
            "1.10.0": {"homepage": None, "license": None},
            "2.0.0-beta.1": {"homepage": "https://beta.example"},
        },
        "repository": None,
    }

    def handler(request):
        return httpx.Response(200, json=payload, request=request)

    mock_registry(handler)
    info = await registries.fetch_npm("demo")

    assert info is not None
    assert info.latest_stable == "1.10.0"
    assert info.homepage is None
    assert info.repository is None


@pytest.mark.asyncio
async def test_npm_respects_a_valid_stable_latest_tag(mock_registry):
    payload = {
        "description": "demo",
        "dist-tags": {"latest": "1.2.3"},
        "versions": {
            "1.2.3": {"homepage": "https://docs.example", "license": "MIT"},
            "2.0.0": {"homepage": "https://new.example", "license": "Apache-2.0"},
        },
    }

    def handler(request):
        return httpx.Response(200, json=payload, request=request)

    mock_registry(handler)
    info = await registries.fetch_npm("demo")

    assert info is not None
    assert info.latest_stable == "1.2.3"
    assert info.homepage == "https://docs.example"
    assert info.license == "MIT"


@pytest.mark.asyncio
async def test_npm_sorts_stable_semver_without_build_metadata_ordering(mock_registry):
    payload = {
        "description": "demo",
        "dist-tags": {"latest": "next"},
        "versions": {
            "1.0.0+10": {},
            "1.0.0+2": {},
            "1.0.1": {},
            "1.0.0-rc.1": {},
        },
    }

    def handler(request):
        return httpx.Response(200, json=payload, request=request)

    mock_registry(handler)
    info = await registries.fetch_npm("demo")

    assert info is not None
    assert info.latest_stable == "1.0.1"


@pytest.mark.asyncio
async def test_npm_repository_suffix_removal_is_literal_and_null_safe(mock_registry):
    payload = {
        "description": "demo",
        "dist-tags": {"latest": "1.0.0"},
        "versions": {"1.0.0": {"homepage": None, "license": None}},
        "repository": {"url": "git+https://github.com/acme/legit"},
    }

    def handler(request):
        return httpx.Response(200, json=payload, request=request)

    mock_registry(handler)
    info = await registries.fetch_npm("demo")

    assert info is not None
    assert info.repository == "https://github.com/acme/legit"


@pytest.mark.asyncio
async def test_crates_selects_highest_stable_non_yanked_version(mock_registry):
    payload = {
        "crate": {
            "newest_version": "3.0.0-beta.1",
            "description": "demo",
            "homepage": None,
            "repository": None,
        },
        "versions": [
            {"num": "1.0.0", "yanked": False, "license": None},
            {"num": "3.0.0-beta.1", "yanked": False, "license": "MIT"},
            {"num": "2.5.0", "yanked": False, "license": "Apache-2.0"},
            {"num": "2.9.0", "yanked": True, "license": "MIT"},
        ],
    }

    def handler(request):
        return httpx.Response(200, json=payload, request=request)

    mock_registry(handler)
    info = await registries.fetch_crates("demo")

    assert info is not None
    assert info.latest_stable == "2.5.0"
    assert info.docs_url == "https://docs.rs/demo/2.5.0"
    assert info.homepage is None
    assert info.repository is None
    assert info.license == "Apache-2.0"


@pytest.mark.asyncio
@pytest.mark.parametrize("newest", ["2.0.0-alpha.1", "9.0.0"])
async def test_crates_does_not_use_unverified_newest_version_as_fallback(
    mock_registry, newest
):
    payload = {
        "crate": {
            "newest_version": newest,
            "description": None,
            "homepage": None,
            "repository": None,
        },
        "versions": [
            {"num": "2.0.0-alpha.1", "yanked": False},
            {"num": "1.0.0", "yanked": True},
        ],
    }

    def handler(request):
        return httpx.Response(200, json=payload, request=request)

    mock_registry(handler)
    info = await registries.fetch_crates("demo")

    assert info is not None
    assert info.latest_stable is None
    assert info.docs_url is None
    assert info.description == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fetcher", "payload"),
    [
        (registries.fetch_pypi, {"info": None, "releases": {}}),
        (registries.fetch_npm, {"versions": [], "dist-tags": {}}),
        (registries.fetch_crates, {"crate": None, "versions": []}),
    ],
)
async def test_malformed_upstream_shapes_raise_safe_value_error(
    mock_registry, fetcher, payload
):
    def handler(request):
        return httpx.Response(200, json=payload, request=request)

    mock_registry(handler)

    with pytest.raises(ValueError, match="malformed registry response"):
        await fetcher("demo")


@pytest.mark.asyncio
async def test_nested_malformed_shapes_raise_safe_value_error(mock_registry):
    cases = [
        (
            registries.fetch_pypi,
            {
                "info": {"version": "1.0.0"},
                "releases": {"1.0.0": [None]},
            },
        ),
        (
            registries.fetch_npm,
            {
                "dist-tags": {"latest": "1.0.0"},
                "versions": {"1.0.0": None},
            },
        ),
        (
            registries.fetch_crates,
            {"crate": {}, "versions": [None]},
        ),
    ]

    for fetcher, payload in cases:

        def handler(request, payload=payload):
            return httpx.Response(200, json=payload, request=request)

        mock_registry(handler)
        with pytest.raises(ValueError, match="malformed registry response"):
            await fetcher("demo")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fetcher", [registries.fetch_pypi, registries.fetch_npm, registries.fetch_crates]
)
async def test_404_is_a_miss_but_http_failure_is_not(mock_registry, fetcher):
    def not_found(request):
        return httpx.Response(404, request=request)

    mock_registry(not_found)
    assert await fetcher("missing") is None

    def unavailable(request):
        return httpx.Response(503, request=request)

    mock_registry(unavailable)
    with pytest.raises(httpx.HTTPStatusError):
        await fetcher("demo")
