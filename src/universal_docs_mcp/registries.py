"""Package registry clients for fetching documentation metadata.

Supports: PyPI (Python), npm (JavaScript/TypeScript), crates.io (Rust)
"""

import re
from dataclasses import dataclass
from typing import Optional

import httpx
from packaging.version import InvalidVersion, Version

from .network import get_response, registry_url
from .validation import InfoArgs


@dataclass
class PackageInfo:
    """Metadata about a package from its registry."""

    name: str
    ecosystem: str
    latest_stable: Optional[str]
    description: str
    homepage: Optional[str] = None
    docs_url: Optional[str] = None
    repository: Optional[str] = None
    license: Optional[str] = None


def _malformed_response() -> ValueError:
    return ValueError("malformed registry response")


def _json_object(response: httpx.Response) -> dict:
    """Decode a registry object without leaking parser/key errors."""
    try:
        data = response.json()
    except Exception:
        raise _malformed_response() from None
    if not isinstance(data, dict):
        raise _malformed_response()
    return data


def _optional_text(value: object) -> Optional[str]:
    return value if isinstance(value, str) else None


def _description(value: object) -> str:
    return value if isinstance(value, str) else ""


def _first_text(*values: object) -> Optional[str]:
    for value in values:
        text = _optional_text(value)
        if text:
            return text
    return None


async def fetch_pypi(package: str) -> Optional[PackageInfo]:
    """Fetch package info from PyPI."""
    resp = await get_response(registry_url(package, "python"))
    if resp.status_code != 200:
        if resp.status_code != 404:
            resp.raise_for_status()
        return None

    data = _json_object(resp)
    info = data.get("info")
    if not isinstance(info, dict):
        raise _malformed_response()
    releases = data.get("releases", {})
    if not isinstance(releases, dict):
        raise _malformed_response()

    parsed = []
    for release, files in releases.items():
        if not isinstance(files, list) or any(
            not isinstance(file, dict) for file in files
        ):
            raise _malformed_response()
        if not isinstance(release, str):
            continue
        try:
            version = Version(release)
        except (InvalidVersion, TypeError):
            continue
        if version.is_prerelease or version.is_devrelease:
            continue
        if not any(
            isinstance(file, dict) and not file.get("yanked", False) for file in files
        ):
            continue
        parsed.append((version, release))

    stable: Optional[str] = None
    if parsed:
        stable = max(parsed, key=lambda item: item[0])[1]

    project_urls = info.get("project_urls")
    if project_urls is None:
        project_urls = {}
    if not isinstance(project_urls, dict):
        raise _malformed_response()

    docs_url = _first_text(info.get("docs_url"), project_urls.get("Documentation"))
    homepage = _first_text(info.get("home_page"), project_urls.get("Homepage"))
    repository = _first_text(project_urls.get("Source"), project_urls.get("Repository"))

    return PackageInfo(
        name=package,
        ecosystem="python",
        latest_stable=stable,
        description=_description(info.get("summary")),
        homepage=homepage,
        docs_url=docs_url,
        repository=repository,
        license=_optional_text(info.get("license")),
    )


# npm and crates use SemVer release keys. Build metadata is deliberately not
# part of this ordering: SemVer gives those keys equal precedence.
_SEMVER_RELEASE = re.compile(
    r"^(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:[.][0-9A-Za-z-]+)*))?"
    r"(?:[+][0-9A-Za-z-]+(?:[.][0-9A-Za-z-]+)*)?$"
)


def _stable_semver(value: object) -> Optional[tuple[int, int, int]]:
    if not isinstance(value, str):
        return None
    match = _SEMVER_RELEASE.fullmatch(value)
    if match is None or match.group(4) is not None:
        return None
    parts = tuple(int(part) for part in match.group(1, 2, 3))
    return (parts[0], parts[1], parts[2])


def _normalise_npm_repository(repository: object) -> Optional[str]:
    if repository is None:
        return None
    if isinstance(repository, dict):
        repository = repository.get("url")
    elif not isinstance(repository, str):
        return None
    if not isinstance(repository, str) or not repository:
        return None

    url = repository
    if url.startswith("git+"):
        url = url[4:]
    if url.startswith("git://"):
        url = "https://" + url[6:]
    if url.endswith(".git"):
        url = url[:-4]
    return url or None


async def fetch_npm(package: str) -> Optional[PackageInfo]:
    """Fetch package info from npm registry."""
    resp = await get_response(registry_url(package, "javascript"))
    if resp.status_code != 200:
        if resp.status_code != 404:
            resp.raise_for_status()
        return None

    data = _json_object(resp)
    versions = data.get("versions")
    if not isinstance(versions, dict) or any(
        not isinstance(version_info, dict) for version_info in versions.values()
    ):
        raise _malformed_response()
    dist_tags = data.get("dist-tags", {})
    if dist_tags is None:
        dist_tags = {}
    if not isinstance(dist_tags, dict):
        raise _malformed_response()

    latest_tag = dist_tags.get("latest")
    latest_key: Optional[str] = None
    latest_semver = _stable_semver(latest_tag)
    if latest_semver is not None and latest_tag in versions:
        latest_key = latest_tag
    else:
        stable_versions = []
        for release in versions:
            parsed = _stable_semver(release)
            if parsed is not None:
                stable_versions.append((parsed, release))
        if stable_versions:
            latest_key = max(stable_versions, key=lambda item: item[0])[1]

    latest_info: dict = {}
    if latest_key is not None:
        candidate = versions.get(latest_key)
        if not isinstance(candidate, dict):
            raise _malformed_response()
        latest_info = candidate

    homepage = _first_text(latest_info.get("homepage"), data.get("homepage"))
    repo_url = _normalise_npm_repository(data.get("repository"))

    return PackageInfo(
        name=package,
        ecosystem="javascript",
        latest_stable=latest_key,
        description=_description(data.get("description")),
        homepage=homepage,
        docs_url=homepage,
        repository=repo_url,
        license=_optional_text(latest_info.get("license")),
    )


async def fetch_crates(package: str) -> Optional[PackageInfo]:
    """Fetch package info from crates.io."""
    resp = await get_response(registry_url(package, "rust"))
    if resp.status_code != 200:
        if resp.status_code != 404:
            resp.raise_for_status()
        return None

    data = _json_object(resp)
    crate = data.get("crate")
    if not isinstance(crate, dict):
        raise _malformed_response()
    versions = data.get("versions")
    if not isinstance(versions, list):
        raise _malformed_response()

    stable_versions = []
    version_records = []
    for version_info in versions:
        if not isinstance(version_info, dict):
            raise _malformed_response()
        release = version_info.get("num")
        if not isinstance(release, str):
            raise _malformed_response()
        version_records.append((release, version_info))
        parsed = _stable_semver(release)
        if parsed is not None and not version_info.get("yanked", False):
            stable_versions.append((parsed, release, version_info))

    stable: Optional[str] = None
    selected_info: Optional[dict] = None
    if stable_versions:
        _, stable, selected_info = max(stable_versions, key=lambda item: item[0])

    license_info = selected_info or (version_records[0][1] if version_records else {})
    docs_url = f"https://docs.rs/{package}/{stable}" if stable else None

    return PackageInfo(
        name=package,
        ecosystem="rust",
        latest_stable=stable,
        description=_description(crate.get("description")),
        homepage=_optional_text(crate.get("homepage")),
        docs_url=docs_url,
        repository=_optional_text(crate.get("repository")),
        license=_optional_text(license_info.get("license")),
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


async def fetch_package(
    package: str, ecosystem: Optional[str] = None
) -> Optional[PackageInfo]:
    """Fetch package info, auto-detecting ecosystem if not specified."""
    if ecosystem:
        fetcher = REGISTRY_MAP.get(ecosystem.lower())
        if fetcher:
            return await fetcher(package)
        return None

    # Try all registries in order of likelihood
    last_error = None
    for ecosystem_name, fetcher in [
        ("python", fetch_pypi),
        ("javascript", fetch_npm),
        ("rust", fetch_crates),
    ]:
        try:
            InfoArgs(package=package, ecosystem=ecosystem_name)
        except ValueError:
            continue
        try:
            result = await fetcher(package)
            if result:
                return result
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    return None
