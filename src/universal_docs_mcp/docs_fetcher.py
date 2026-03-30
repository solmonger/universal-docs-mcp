"""Fetch actual documentation content from package doc sites.

After getting package metadata from registries.py, this module
fetches the actual documentation content from docs URLs.
"""

import os
import re
from typing import Optional
import httpx


async def fetch_readme_from_github(repo_url: str) -> Optional[str]:
    """Fetch README content from a GitHub repository."""
    if not repo_url:
        return None

    # Normalize URL to API format
    match = re.search(r"github\.com/([^/]+/[^/]+)", repo_url)
    if not match:
        return None

    repo_path = match.group(1).rstrip("/")
    api_url = f"https://api.github.com/repos/{repo_path}/readme"

    headers = {"Accept": "application/vnd.github.raw+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            api_url,
            headers=headers,
        )
        if resp.status_code == 200:
            content = resp.text
            # Truncate to first 3000 chars for context window efficiency
            if len(content) > 3000:
                content = content[:3000] + "\n\n... [truncated — see full docs]"
            return content
    return None


async def fetch_pypi_description(package: str) -> Optional[str]:
    """Fetch the long description from PyPI (usually README)."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"https://pypi.org/pypi/{package}/json")
        if resp.status_code != 200:
            return None
        data = resp.json()
        desc = data["info"].get("description", "")
        if len(desc) > 3000:
            desc = desc[:3000] + "\n\n... [truncated]"
        return desc if desc else None


async def fetch_npm_readme(package: str) -> Optional[str]:
    """Fetch README from npm registry."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"https://registry.npmjs.org/{package}")
        if resp.status_code != 200:
            return None
        data = resp.json()
        readme = data.get("readme", "")
        if len(readme) > 3000:
            readme = readme[:3000] + "\n\n... [truncated]"
        return readme if readme and readme != "ERROR: No README data found!" else None


async def fetch_docs_content(
    package: str,
    ecosystem: str,
    docs_url: Optional[str] = None,
    repo_url: Optional[str] = None,
) -> Optional[str]:
    """Fetch documentation content for a package.

    Tries multiple sources in order:
    1. PyPI/npm embedded docs (fastest, most reliable)
    2. GitHub README (good fallback)
    3. Docs URL scraping (last resort)
    """
    # Ecosystem-specific fetchers
    if ecosystem == "python":
        content = await fetch_pypi_description(package)
        if content:
            return content

    if ecosystem in ("javascript", "typescript"):
        content = await fetch_npm_readme(package)
        if content:
            return content

    # GitHub README fallback
    if repo_url:
        content = await fetch_readme_from_github(repo_url)
        if content:
            return content

    return None
