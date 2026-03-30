"""Package registry clients for fetching documentation metadata.

Supports: PyPI (Python), npm (JavaScript/TypeScript), crates.io (Rust)
"""

import httpx
from dataclasses import dataclass
from typing import Optional
from packaging.version import Version, InvalidVersion


@dataclass
class PackageInfo:
    """Metadata about a package from its registry."""
    name: str
    ecosystem: str
    latest_stable: str
    description: str
    homepage: Optional[str] = None
    docs_url: Optional[str] = None
    repository: Optional[str] = None
    license: Optional[str] = None


async def fetch_pypi(package: str) -> Optional[PackageInfo]:
    """Fetch package info from PyPI."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"https://pypi.org/pypi/{package}/json")
        if resp.status_code != 200:
            return None
        data = resp.json()
        info = data["info"]

        # Find latest stable version (skip pre-releases)
        stable = info["version"]
        releases = data.get("releases", {})
        # Parse and sort by actual version semantics, not lexicographic order
        parsed = []
        for ver in releases:
            try:
                parsed.append((Version(ver), ver))
            except InvalidVersion:
                continue
        for v, ver in sorted(parsed, reverse=True):
            if v.is_prerelease:
                continue
            if releases[ver]:  # has actual files
                stable = ver
                break

        docs_url = info.get("docs_url") or info.get("project_urls", {}).get("Documentation")
        homepage = info.get("home_page") or info.get("project_urls", {}).get("Homepage")
        repo = info.get("project_urls", {}).get("Source") or info.get("project_urls", {}).get("Repository")

        return PackageInfo(
            name=package,
            ecosystem="python",
            latest_stable=stable,
            description=info.get("summary", ""),
            homepage=homepage,
            docs_url=docs_url,
            repository=repo,
            license=info.get("license", ""),
        )


async def fetch_npm(package: str) -> Optional[PackageInfo]:
    """Fetch package info from npm registry."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"https://registry.npmjs.org/{package}")
        if resp.status_code != 200:
            return None
        data = resp.json()

        # Get latest stable (dist-tags.latest)
        latest = data.get("dist-tags", {}).get("latest", "")
        latest_info = data.get("versions", {}).get(latest, {})

        homepage = latest_info.get("homepage") or data.get("homepage")
        repo = data.get("repository", {})
        repo_url = repo.get("url", "") if isinstance(repo, dict) else str(repo)
        repo_url = repo_url.replace("git+", "").replace("git://", "https://").rstrip(".git")

        return PackageInfo(
            name=package,
            ecosystem="javascript",
            latest_stable=latest,
            description=data.get("description", ""),
            homepage=homepage,
            docs_url=homepage,  # npm packages usually use homepage for docs
            repository=repo_url or None,
            license=latest_info.get("license", ""),
        )


async def fetch_crates(package: str) -> Optional[PackageInfo]:
    """Fetch package info from crates.io."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"https://crates.io/api/v1/crates/{package}",
            headers={"User-Agent": "universal-docs-mcp/0.1.0"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        crate = data.get("crate", {})
        versions = data.get("versions", [])

        # Find latest stable (non-yanked, non-pre)
        stable = crate.get("newest_version", "")
        for v in versions:
            if not v.get("yanked") and "-" not in v.get("num", "-"):
                stable = v["num"]
                break

        return PackageInfo(
            name=package,
            ecosystem="rust",
            latest_stable=stable,
            description=crate.get("description", ""),
            homepage=crate.get("homepage"),
            docs_url=f"https://docs.rs/{package}/{stable}",
            repository=crate.get("repository"),
            license=versions[0].get("license", "") if versions else "",
        )


REGISTRY_MAP = {
    "python": fetch_pypi,
    "pypi": fetch_pypi,
    "pip": fetch_pypi,
    "javascript": fetch_npm,
    "typescript": fetch_npm,
    "npm": fetch_npm,
    "js": fetch_npm,
    "ts": fetch_npm,
    "rust": fetch_crates,
    "cargo": fetch_crates,
    "crate": fetch_crates,
}


async def fetch_package(package: str, ecosystem: Optional[str] = None) -> Optional[PackageInfo]:
    """Fetch package info, auto-detecting ecosystem if not specified."""
    if ecosystem:
        fetcher = REGISTRY_MAP.get(ecosystem.lower())
        if fetcher:
            return await fetcher(package)
        return None

    # Try all registries in order of likelihood
    for fetcher in [fetch_pypi, fetch_npm, fetch_crates]:
        try:
            result = await fetcher(package)
            if result:
                return result
        except Exception:
            continue
    return None
