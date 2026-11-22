"""Fetch raw README/registry descriptions with explicit source provenance."""

import os
import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlsplit

from packaging.version import InvalidVersion, Version

from .network import get_response, registry_url

_readme_source_url: ContextVar[str | None] = ContextVar(
    "readme_source_url", default=None
)


@dataclass(frozen=True)
class FetchedDocument:
    content: str
    source: str
    source_url: str


def _github_readme_url(repo_url: str, version: Optional[str] = None) -> Optional[str]:
    try:
        parsed = urlsplit(repo_url or "")
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.port not in (None, 443)
        ):
            return None
    except ValueError:
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2:
        return None
    owner, repo = parts
    repo = repo.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", owner) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", repo
    ):
        return None
    if repo in (".", ".."):
        return None
    api_url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    if version:
        api_url += "?ref=" + quote(version, safe="")
    return api_url


async def fetch_readme_document_from_github(
    repo_url: str, version: Optional[str] = None
) -> Optional[FetchedDocument]:
    api_url = _github_readme_url(repo_url, version)
    if not api_url:
        return None
    headers = {"Accept": "application/vnd.github.raw+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
        # A broad token must not turn a public documentation lookup into a
        # private-repository read merely because registry metadata names it.
        repository_url = api_url.rsplit("/readme", 1)[0]
        metadata = await get_response(
            repository_url,
            headers={
                "Authorization": headers["Authorization"],
                "Accept": "application/vnd.github+json",
            },
        )
        if metadata.status_code == 404:
            return None
        repository = metadata.json()
        if not isinstance(repository, dict) or not isinstance(
            repository.get("private"), bool
        ):
            raise ValueError("invalid_github_visibility")
        if repository["private"]:
            return None
    urls = [api_url]
    if version and not version.startswith("v"):
        urls.append(_github_readme_url(repo_url, "v" + version))
    for url in urls:
        response = await get_response(url, headers=headers, max_bytes=1024 * 1024)
        if response.status_code == 200 and response.text:
            return FetchedDocument(response.text, "github_readme", url)
        if response.status_code != 404:
            return None
    return None


async def fetch_readme_from_github(
    repo_url: str, version: Optional[str] = None
) -> Optional[str]:
    result = await fetch_readme_document_from_github(repo_url, version)
    _readme_source_url.set(result.source_url if result else None)
    return result.content if result else None


def _github_repo_api(repo_url: str) -> Optional[str]:
    readme_url = _github_readme_url(repo_url)
    if not readme_url:
        return None
    return readme_url.rsplit("/readme", 1)[0]


async def _check_public_repo(repo_api: str, headers: dict) -> bool:
    if "Authorization" not in headers:
        return True
    metadata = await get_response(
        repo_api,
        headers={
            "Authorization": headers["Authorization"],
            "Accept": "application/vnd.github+json",
        },
    )
    if metadata.status_code == 404:
        return False
    repository = metadata.json()
    if not isinstance(repository, dict) or not isinstance(
        repository.get("private"), bool
    ):
        raise ValueError("invalid_github_visibility")
    return not repository["private"]


def _github_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def fetch_release_notes_from_github(
    repo_url: str, version: str
) -> Optional[FetchedDocument]:
    repo_api = _github_repo_api(repo_url)
    if (
        not repo_api
        or not isinstance(version, str)
        or not version
        or any(c in version for c in "/?#\x00")
    ):
        return None
    headers = _github_headers()
    if not await _check_public_repo(repo_api, headers):
        return None
    tags = [version] + (["v" + version] if not version.startswith("v") else [])
    for tag in tags:
        url = f"{repo_api}/releases/tags/{quote(tag, safe='')}"
        response = await get_response(url, headers=headers, max_bytes=512 * 1024)
        if response.status_code == 404:
            continue
        if response.status_code != 200:
            return None
        data = response.json()
        if (
            not isinstance(data, dict)
            or data.get("tag_name") != tag
            or not isinstance(data.get("body", ""), str)
        ):
            return None
        content = "\n".join(
            str(data.get(k) or "") for k in ("name", "tag_name", "published_at", "body")
        ).strip()
        return FetchedDocument(content[: 512 * 1024], "github_release", url)
    return None


def _version_value(value: str, ecosystem: str):
    value = value.lstrip("v")
    try:
        return Version(value)
    except InvalidVersion:
        return None


async def fetch_version_diff(
    package: str,
    ecosystem: str,
    from_version: str,
    to_version: str,
    *,
    repo_url: Optional[str] = None,
) -> Optional[FetchedDocument]:
    if not repo_url:
        return None
    low, high = (
        _version_value(from_version, ecosystem),
        _version_value(to_version, ecosystem),
    )
    if low is None or high is None or low >= high:
        return None
    repo_api = _github_repo_api(repo_url)
    if not repo_api:
        return None
    headers = _github_headers()
    if not await _check_public_repo(repo_api, headers):
        return None
    response = await get_response(
        f"{repo_api}/releases?per_page=20", headers=headers, max_bytes=512 * 1024
    )
    if response.status_code != 200:
        return None
    entries = response.json()
    if not isinstance(entries, list):
        return None
    selected = []
    for entry in entries[:20]:
        if (
            not isinstance(entry, dict)
            or entry.get("prerelease")
            or not isinstance(entry.get("tag_name"), str)
        ):
            continue
        value = _version_value(entry["tag_name"], ecosystem)
        if value is not None and not value.is_prerelease and low < value <= high:
            selected.append((value, entry))
    selected.sort(key=lambda pair: pair[0])
    selected = selected[-10:]
    blocks = []
    for value, entry in selected:
        block = f"## {entry['tag_name']}\n{entry.get('name') or ''}\n{entry.get('body') or ''}".strip()
        blocks.append(block)
    if not blocks:
        return None
    content = f"# {package}: {from_version} to {to_version}\n\n" + "\n\n".join(blocks)
    content = content.encode("utf-8")[: 256 * 1024].decode("utf-8", errors="ignore")
    return FetchedDocument(
        content, "github_release_diff", f"{repo_api}/releases?per_page=20"
    )


async def fetch_pypi_description(
    package: str, version: Optional[str] = None
) -> Optional[str]:
    response = await get_response(registry_url(package, "python", version))
    if response.status_code == 404:
        return None
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("info"), dict):
        raise ValueError("invalid_pypi_document")
    description = data["info"].get("description", "")
    if not isinstance(description, str):
        raise ValueError("invalid_pypi_description")
    return description or None


async def fetch_npm_readme(
    package: str, version: Optional[str] = None
) -> Optional[str]:
    response = await get_response(registry_url(package, "javascript", version))
    if response.status_code == 404:
        return None
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("invalid_npm_document")
    readme = data.get("readme", "")
    if not isinstance(readme, str):
        raise ValueError("invalid_npm_readme")
    return readme if readme and readme != "ERROR: No README data found!" else None


async def fetch_docs_content_with_provenance(
    package: str,
    ecosystem: str,
    docs_url: Optional[str] = None,
    repo_url: Optional[str] = None,
    version: Optional[str] = None,
) -> Optional[FetchedDocument]:
    """Never replace an unavailable version with default-branch content.

    GitHub fallback is explicitly an unverified Git ref, not proof of package
    artifact identity. Server responses expose this weaker version binding.
    """
    if ecosystem == "python":
        content = await fetch_pypi_description(package, version)
        if content:
            return FetchedDocument(
                content, "pypi_description", registry_url(package, "python", version)
            )
    if ecosystem in ("javascript", "typescript"):
        content = await fetch_npm_readme(package, version)
        if content:
            return FetchedDocument(
                content, "npm_readme", registry_url(package, "javascript", version)
            )
    if repo_url:
        content = await fetch_readme_from_github(repo_url, version)
        if content:
            exact_url = (
                _readme_source_url.get() or _github_readme_url(repo_url, version) or ""
            )
            return FetchedDocument(content, "github_readme", exact_url)
    return None


async def fetch_docs_content(
    package: str,
    ecosystem: str,
    docs_url: Optional[str] = None,
    repo_url: Optional[str] = None,
    version: Optional[str] = None,
) -> Optional[str]:
    result = await fetch_docs_content_with_provenance(
        package, ecosystem, docs_url, repo_url, version
    )
    return result.content if result else None
