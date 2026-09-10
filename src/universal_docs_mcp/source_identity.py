"""Pure package-document source identity checks.

Cache rows are local data, not authority.  A row is usable only when its
embedded package/source identity agrees with the exact request.  This module
stays free of cache, network, and model code so the same policy can be reused
at retrieval and delivery boundaries.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit

from .network import registry_url
from .validation import ALIASES

_REGISTRY_SOURCES = {
    "python": "pypi_description",
    "javascript": "npm_readme",
}
_GITHUB_OWNER = re.compile(r"[A-Za-z0-9_-]+")
_GITHUB_REPO = re.compile(r"[A-Za-z0-9_.-]+")


def _canonical_ecosystem(ecosystem: object) -> str:
    if not isinstance(ecosystem, str):
        return ""
    return ALIASES.get(ecosystem, ecosystem)


def _github_repository_parts(repository: object) -> tuple[str, str] | None:
    if not isinstance(repository, str):
        return None
    try:
        parsed = urlsplit(repository)
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
    if not _GITHUB_OWNER.fullmatch(owner) or not _GITHUB_REPO.fullmatch(repo):
        return None
    return owner, repo


def _github_source_parts(source_url: object, version: str) -> tuple[str, str] | None:
    if not isinstance(source_url, str):
        return None
    try:
        parsed = urlsplit(source_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.github.com"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.fragment
            or parsed.query != f"ref={quote(version, safe='')}"
        ):
            return None
    except ValueError:
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 4 or parts[0] != "repos" or parts[3] != "readme":
        return None
    owner, repo = parts[1:3]
    if not _GITHUB_OWNER.fullmatch(owner) or not _GITHUB_REPO.fullmatch(repo):
        return None
    return owner, repo


def _expected_registry_url(
    package: str, ecosystem: str, version: str
) -> tuple[str, str] | None:
    canonical = _canonical_ecosystem(ecosystem)
    expected_source = _REGISTRY_SOURCES.get(canonical)
    if expected_source is None:
        return None
    try:
        return expected_source, registry_url(package, canonical, version)
    except (TypeError, ValueError):
        return None


def package_source_matches(
    *,
    package: str,
    ecosystem: str,
    version: str,
    source: object,
    source_url: object,
    repository: object = None,
) -> bool:
    """Return whether a document source binds one exact package version.

    Registry sources must equal the URL produced by the package's allowlisted
    registry endpoint.  GitHub README sources must be the safe API README URL
    with an exact ``ref`` and, when registry metadata supplies a repository,
    that repository must be the same owner/repository.  Every other source
    kind or URL is rejected, including local, credential-bearing, and other
    hosts.
    """
    if not isinstance(package, str) or not package:
        return False
    if not isinstance(ecosystem, str) or not ecosystem:
        return False
    if not isinstance(version, str) or not version:
        return False
    if any(character in version for character in ("/", "?", "#", "\x00")):
        return False

    expected = _expected_registry_url(package, ecosystem, version)
    if expected is not None:
        expected_source, expected_url = expected
        if source == expected_source:
            return source_url == expected_url

    if source != "github_readme":
        return False
    source_parts = _github_source_parts(source_url, version)
    if source_parts is None:
        return False
    if repository is not None:
        repository_parts = _github_repository_parts(repository)
        if repository_parts is None or repository_parts != source_parts:
            return False
    return True


def registry_version_for_source_url(
    *, package: str, ecosystem: str, source: object, source_url: object
) -> str | None:
    """Recover a version only from a canonical exact registry URL.

    This exists solely for legacy rows without a persisted ``version`` field.
    GitHub refs are deliberately not accepted as a substitute for a registry
    binding because a Git ref is not proof of a published package artifact.
    """
    expected = _expected_registry_url(package, ecosystem, "0.0.0")
    if expected is None or source != expected[0] or not isinstance(source_url, str):
        return None
    canonical = _canonical_ecosystem(ecosystem)
    try:
        parsed = urlsplit(source_url)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.query
            or parsed.port not in (None, 443)
        ):
            return None
        segments = parsed.path.strip("/").split("/")
        if canonical == "python":
            if len(segments) != 4 or segments[0] != "pypi" or segments[3] != "json":
                return None
            raw_package, raw_version = segments[1:3]
        elif canonical == "javascript":
            if len(segments) != 2:
                return None
            raw_package, raw_version = segments
        else:
            return None
        version = unquote(raw_version)
    except (TypeError, ValueError):
        return None
    if not version:
        return None
    if _expected_registry_url(package, canonical, version) != (
        expected[0],
        source_url,
    ):
        return None
    # The package segment is checked by reconstructing the complete URL above;
    # keep the local variables consumed so malformed paths cannot be mistaken
    # for a version-only match.
    if not raw_package:
        return None
    return version
