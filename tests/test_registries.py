"""Tests for package registry clients."""

import pytest
from universal_docs_mcp.registries import fetch_pypi, fetch_npm, fetch_crates, fetch_package


@pytest.mark.asyncio
async def test_fetch_pypi_requests():
    info = await fetch_pypi("requests")
    assert info is not None
    assert info.name == "requests"
    assert info.ecosystem == "python"
    assert info.latest_stable  # has a version
    assert "HTTP" in info.description or "http" in info.description.lower()


@pytest.mark.asyncio
async def test_fetch_pypi_nonexistent():
    info = await fetch_pypi("this-package-definitely-does-not-exist-xyz-123")
    assert info is None


@pytest.mark.asyncio
async def test_fetch_npm_express():
    info = await fetch_npm("express")
    assert info is not None
    assert info.name == "express"
    assert info.ecosystem == "javascript"
    assert info.latest_stable


@pytest.mark.asyncio
async def test_fetch_npm_nonexistent():
    info = await fetch_npm("this-package-definitely-does-not-exist-xyz-123")
    assert info is None


@pytest.mark.asyncio
async def test_fetch_crates_serde():
    info = await fetch_crates("serde")
    assert info is not None
    assert info.name == "serde"
    assert info.ecosystem == "rust"
    assert info.docs_url
    assert "docs.rs" in info.docs_url


@pytest.mark.asyncio
async def test_fetch_package_auto_detect():
    # "requests" exists on PyPI, should auto-detect
    info = await fetch_package("requests")
    assert info is not None
    assert info.ecosystem == "python"


@pytest.mark.asyncio
async def test_fetch_package_with_ecosystem():
    info = await fetch_package("express", ecosystem="npm")
    assert info is not None
    assert info.ecosystem == "javascript"
