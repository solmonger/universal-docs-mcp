import json

import pytest

from universal_docs_mcp import docs_fetcher
from universal_docs_mcp.official_sources import fetch_official_source
from universal_docs_mcp.source_catalog import list_sources
from universal_docs_mcp.source_identity import package_source_matches


class Response:
    def __init__(self, status_code=200, content=b"release", headers=None, data=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.text = content.decode("utf-8")
        self._data = data

    def json(self):
        return self._data if self._data is not None else json.loads(self.content)


@pytest.mark.asyncio
async def test_readme_falls_back_to_v_tag_and_returns_exact_provenance(monkeypatch):
    calls = []

    async def get(url, **kwargs):
        calls.append(url)
        if "/pypi/" in url:
            return Response(404)
        return Response(404 if "ref=2.3.0" in url else 200, b"readme")

    monkeypatch.setattr(docs_fetcher, "get_response", get)
    result = await docs_fetcher.fetch_docs_content_with_provenance(
        "pkg", "python", repo_url="https://github.com/acme/pkg", version="2.3.0"
    )
    assert result.source_url.endswith("ref=v2.3.0")
    assert calls[-2].endswith("ref=2.3.0")
    assert calls[-1].endswith("ref=v2.3.0")
    for ref in ("2.3.0", "v2.3.0"):
        assert package_source_matches(
            package="pkg",
            ecosystem="python",
            version="2.3.0",
            source="github_readme",
            source_url=f"https://api.github.com/repos/acme/pkg/readme?ref={ref}",
        )
    assert not package_source_matches(
        package="pkg",
        ecosystem="python",
        version="2.3.0",
        source="github_readme",
        source_url="https://api.github.com/repos/acme/pkg/readme?ref=main",
    )


@pytest.mark.asyncio
async def test_release_notes_and_version_diff_are_exact_and_bounded(monkeypatch):
    async def get(url, **kwargs):
        if "releases/tags/" in url:
            if url.endswith("/2.0"):
                return Response(404)
            return Response(
                data={
                    "name": "2.0",
                    "tag_name": "v2.0",
                    "published_at": "2026-01-01",
                    "body": "notes",
                }
            )
        return Response(
            data=[
                {
                    "tag_name": "v1.0",
                    "name": "old",
                    "body": "old",
                    "published_at": "2025-01-01",
                },
                {
                    "tag_name": "v1.5",
                    "name": "middle",
                    "body": "middle",
                    "published_at": "2025-02-01",
                },
                {
                    "tag_name": "v2.0",
                    "name": "new",
                    "body": "new",
                    "published_at": "2026-01-01",
                },
                {
                    "tag_name": "v2.1",
                    "name": "future",
                    "body": "future",
                    "published_at": "2027-01-01",
                },
                {
                    "tag_name": "v2.0rc1",
                    "name": "pre",
                    "body": "pre",
                    "published_at": "2026-01-01",
                },
            ]
        )

    monkeypatch.setattr(docs_fetcher, "get_response", get)
    release = await docs_fetcher.fetch_release_notes_from_github(
        "https://github.com/acme/pkg", "2.0"
    )
    assert release.source == "github_release"
    assert package_source_matches(
        package="pkg",
        ecosystem="python",
        version="2.0",
        source=release.source,
        source_url=release.source_url,
    )
    diff = await docs_fetcher.fetch_version_diff(
        "pkg", "python", "1.0", "2.0", repo_url="https://github.com/acme/pkg"
    )
    assert diff.source == "github_release_diff"
    assert "middle" in diff.content and "new" in diff.content
    assert (
        "old" not in diff.content
        and "future" not in diff.content
        and "pre" not in diff.content
    )
    assert len(diff.content.encode()) <= 256 * 1024


def test_local_python_stubs_are_confined_bounded_and_signature_only(tmp_path):
    from universal_docs_mcp.local_stubs import extract_local_stubs

    stub = tmp_path / ".venv/lib/python3.12/site-packages/pkg"
    stub.mkdir(parents=True)
    (stub / "__init__.pyi").write_text(
        'class BaseModel:\n    """Model docs."""\n    def save(self, x: int) -> str: ...\n'
    )
    (stub / "validators.py").write_text(
        'def field_validator(x):\n    """Validate input."""\n    return x\n'
    )
    result = extract_local_stubs(
        tmp_path, "pkg", "python", "2.11", symbols=["BaseModel", "field_validator"]
    )
    assert result and "UNTRUSTED LOCAL STUB DATA" in result.content
    assert "def save(self, x: int) -> str" in result.content
    assert "return x" not in result.content
    assert result.content_bytes <= 4096 and not result.relative_path.startswith("/")
    assert extract_local_stubs(tmp_path, "../outside", "python", "2.11") is None
    (stub / "evil.pyi").symlink_to("/etc/passwd")


def test_local_typescript_stubs_extract_requested_exports(tmp_path):
    from universal_docs_mcp.local_stubs import extract_local_stubs

    package = tmp_path / "node_modules/pkg"
    package.mkdir(parents=True)
    (package / "index.d.ts").write_text(
        "export interface Client {\n  get(): string\n}\nexport function create(): Client\n"
    )
    result = extract_local_stubs(
        tmp_path, "pkg", "typescript", "1.2.3", symbols=["Client"]
    )
    assert result and "export interface Client" in result.content
    assert "create" not in result.content

    scoped = tmp_path / "node_modules/@types/demo"
    scoped.mkdir(parents=True)
    (scoped / "index.d.ts").write_text("export type Config = { retries: number };\n")
    scoped_res = extract_local_stubs(
        tmp_path, "@types/demo", "typescript", "1.0.0", symbols=["Config"]
    )
    assert scoped_res and "export type Config" in scoped_res.content


@pytest.mark.asyncio
async def test_version_diff_returns_none_when_no_releases_in_range(monkeypatch):
    async def get(url, **kwargs):
        return Response(data=[{"tag_name": "v0.9", "name": "old", "body": "old"}])

    monkeypatch.setattr(docs_fetcher, "get_response", get)
    assert (
        await docs_fetcher.fetch_version_diff(
            "pkg", "python", "1.0", "2.0", repo_url="https://github.com/acme/pkg"
        )
        is None
    )


@pytest.mark.asyncio
async def test_catalog_is_data_driven_and_official_text_plain_allowed(monkeypatch):
    assert len(list_sources()) >= 9
    assert (
        next(
            s
            for s in list_sources()
            if (s.source_id, s.version) == ("mcp-tools", "2026-07-28")
        ).retrieval_url
        == "https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md"
    )

    async def get(*args):
        return Response(
            content=b"# docs", headers={"content-type": "text/plain; charset=utf-8"}
        )

    monkeypatch.setattr("universal_docs_mcp.official_sources.get_catalog_response", get)
    assert (await fetch_official_source("fastapi", "llms")).content == "# docs"

    async def html(*args):
        return Response(content=b"<html>", headers={"content-type": "text/html"})

    monkeypatch.setattr(
        "universal_docs_mcp.official_sources.get_catalog_response", html
    )
    with pytest.raises(ValueError):
        await fetch_official_source("fastapi", "llms")
