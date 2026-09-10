"""Fetch raw README/registry descriptions with explicit source provenance."""
from dataclasses import dataclass
import os
import re
from typing import Optional
from urllib.parse import quote, urlsplit

from .network import get_response, registry_url


@dataclass(frozen=True)
class FetchedDocument:
    content: str
    source: str
    source_url: str


def _github_readme_url(repo_url: str, version: Optional[str] = None) -> Optional[str]:
    try:
        parsed = urlsplit(repo_url or '')
        if (parsed.scheme != 'https' or parsed.hostname != 'github.com'
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.port not in (None, 443)):
            return None
    except ValueError:
        return None
    parts = parsed.path.strip('/').split('/')
    if len(parts) != 2:
        return None
    owner, repo = parts
    repo = repo.removesuffix('.git')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', owner) or not re.fullmatch(r'[A-Za-z0-9_.-]+', repo):
        return None
    if repo in ('.', '..'):
        return None
    api_url = f'https://api.github.com/repos/{owner}/{repo}/readme'
    if version:
        api_url += '?ref=' + quote(version, safe='')
    return api_url


async def fetch_readme_from_github(repo_url: str, version: Optional[str] = None) -> Optional[str]:
    api_url = _github_readme_url(repo_url, version)
    if not api_url:
        return None
    headers = {'Accept': 'application/vnd.github.raw+json'}
    token = os.environ.get('GITHUB_TOKEN')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    response = await get_response(api_url, headers=headers, max_bytes=1024 * 1024)
    return response.text or None if response.status_code == 200 else None


async def fetch_pypi_description(package: str, version: Optional[str] = None) -> Optional[str]:
    response = await get_response(registry_url(package, 'python', version))
    if response.status_code == 404:
        return None
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get('info'), dict):
        raise ValueError('invalid_pypi_document')
    description = data['info'].get('description', '')
    if not isinstance(description, str):
        raise ValueError('invalid_pypi_description')
    return description or None


async def fetch_npm_readme(package: str, version: Optional[str] = None) -> Optional[str]:
    response = await get_response(registry_url(package, 'javascript', version))
    if response.status_code == 404:
        return None
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError('invalid_npm_document')
    readme = data.get('readme', '')
    if not isinstance(readme, str):
        raise ValueError('invalid_npm_readme')
    return readme if readme and readme != 'ERROR: No README data found!' else None


async def fetch_docs_content_with_provenance(
    package: str, ecosystem: str, docs_url: Optional[str] = None,
    repo_url: Optional[str] = None, version: Optional[str] = None,
) -> Optional[FetchedDocument]:
    """Never replace an unavailable version with default-branch content.

    GitHub fallback is explicitly an unverified Git ref, not proof of package
    artifact identity. Server responses expose this weaker version binding.
    """
    if ecosystem == 'python':
        content = await fetch_pypi_description(package, version)
        if content:
            return FetchedDocument(content, 'pypi_description', registry_url(package, 'python', version))
    if ecosystem in ('javascript', 'typescript'):
        content = await fetch_npm_readme(package, version)
        if content:
            return FetchedDocument(content, 'npm_readme', registry_url(package, 'javascript', version))
    if repo_url:
        content = await fetch_readme_from_github(repo_url, version)
        if content:
            return FetchedDocument(content, 'github_readme', _github_readme_url(repo_url, version) or '')
    return None


async def fetch_docs_content(
    package: str, ecosystem: str, docs_url: Optional[str] = None,
    repo_url: Optional[str] = None, version: Optional[str] = None,
) -> Optional[str]:
    result = await fetch_docs_content_with_provenance(package, ecosystem, docs_url, repo_url, version)
    return result.content if result else None
