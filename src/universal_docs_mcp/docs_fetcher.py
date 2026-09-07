"""Fetch actual documentation content from package doc sites.

The fetch layer returns raw upstream content. Context shaping belongs to
``compaction.py`` so the cache never destroys information before a caller has
selected a token budget or section.
"""

from dataclasses import dataclass
import os
import re
from typing import Optional
from urllib.parse import quote

import httpx


@dataclass(frozen=True)
class FetchedDocument:
    """Raw documentation plus the source used to obtain it."""

    content: str
    source: str
    source_url: str


def _github_readme_url(repo_url: str, version: Optional[str] = None) -> Optional[str]:
    match = re.search(r"github\.com/([^/#?]+/[^/#?]+)", repo_url or "")
    if not match:
        return None
    repo_path = match.group(1).rstrip("/")
    if repo_path.endswith(".git"):
        repo_path = repo_path[:-4]
    api_url = f"https://api.github.com/repos/{repo_path}/readme"
    if version:
        api_url += f"?ref={quote(version, safe='') }"
    return api_url


async def fetch_readme_from_github(repo_url: str, version: Optional[str] = None) -> Optional[str]:
    """Fetch a README, using an exact Git ref when ``version`` is requested.

    A versioned request never falls back to the repository's default branch;
    doing so would label current README text as documentation for an older
    package version.
    """
    api_url = _github_readme_url(repo_url, version)
    if not api_url:
        return None

    headers = {"Accept": "application/vnd.github.raw+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(api_url, headers=headers)
        if resp.status_code == 200:
            return resp.text
        if resp.status_code != 404:
            resp.raise_for_status()
    return None


async def fetch_pypi_description(package: str, version: Optional[str] = None) -> Optional[str]:
    """Fetch the complete long description from PyPI."""
    endpoint = (
        f"https://pypi.org/pypi/{package}/{version}/json"
        if version
        else f"https://pypi.org/pypi/{package}/json"
    )
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(endpoint)
        if resp.status_code != 200:
            if resp.status_code != 404:
                resp.raise_for_status()
            return None
        data = resp.json()
        desc = data["info"].get("description", "")
        return desc or None


async def fetch_npm_readme(package: str, version: Optional[str] = None) -> Optional[str]:
    """Fetch the complete README from npm, optionally at an exact version."""
    endpoint = (
        f"https://registry.npmjs.org/{package}/{version}"
        if version
        else f"https://registry.npmjs.org/{package}"
    )
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(endpoint)
        if resp.status_code != 200:
            if resp.status_code != 404:
                resp.raise_for_status()
            return None
        data = resp.json()
        readme = data.get("readme", "")
        return readme if readme and readme != "ERROR: No README data found!" else None


async def fetch_docs_content_with_provenance(
    package: str,
    ecosystem: str,
    docs_url: Optional[str] = None,
    repo_url: Optional[str] = None,
    version: Optional[str] = None,
) -> Optional[FetchedDocument]:
    """Fetch raw docs and identify the exact upstream source used.

    If an exact version is requested, registry endpoints are queried at that
    version and the GitHub fallback is queried at the matching ref. A failed
    versioned ref is a miss, not permission to return the latest README.
    """
    if ecosystem == "python":
        content = await fetch_pypi_description(package, version=version)
        if content:
            url = (
                f"https://pypi.org/pypi/{package}/{version}/json"
                if version
                else f"https://pypi.org/pypi/{package}/json"
            )
            return FetchedDocument(content, "pypi_description", url)

    if ecosystem in ("javascript", "typescript"):
        content = await fetch_npm_readme(package, version=version)
        if content:
            url = (
                f"https://registry.npmjs.org/{package}/{version}"
                if version
                else f"https://registry.npmjs.org/{package}"
            )
            return FetchedDocument(content, "npm_readme", url)

    if repo_url:
        content = await fetch_readme_from_github(repo_url, version=version)
        if content:
            source_url = _github_readme_url(repo_url, version) or repo_url
            return FetchedDocument(content, "github_readme", source_url)

    return None


async def fetch_docs_content(
    package: str,
    ecosystem: str,
    docs_url: Optional[str] = None,
    repo_url: Optional[str] = None,
    version: Optional[str] = None,
) -> Optional[str]:
    """Backward-compatible content-only wrapper around the provenance fetcher."""
    result = await fetch_docs_content_with_provenance(
        package=package,
        ecosystem=ecosystem,
        docs_url=docs_url,
        repo_url=repo_url,
        version=version,
    )
    return result.content if result else None
