import json
from types import SimpleNamespace

from universal_docs_mcp import server
from universal_docs_mcp.cache import DocsCache
from universal_docs_mcp.compaction import parse_sections, rank_sections, tokenize_query
from universal_docs_mcp.contracts import RESULT_TYPES
from universal_docs_mcp.docs_fetcher import FetchedDocument
from universal_docs_mcp.registries import PackageInfo


def test_ranker_uses_boundaries_identifier_parts_and_deterministic_bm25():
    sections = parse_sections(
        "# Pkg\n## Offset Settings\nConfigure offset and reset.\n"
        "## Model Validators\nUse ModelValidator and model_validator validators.\n"
        "## Router\nuseRouter handles routing.\n"
        "## First Retry\nretry timeout.\n## Second Retry\nretry timeout.\n"
    )
    assert rank_sections(sections, "set") == []
    ranked = rank_sections(sections, "model_validator")
    assert ranked[0].section.slug == "model-validators"
    assert "modelvalidator" in tokenize_query("ModelValidator")
    assert {"model", "validator"} <= set(tokenize_query("model_validator"))
    assert rank_sections(sections, "useRouter")[0].section.slug == "router"
    weighted = parse_sections(
        "## Rare Alpha\ncommon common common common.\n"
        "## Common Notes\ncommon common common common.\n"
    )
    assert rank_sections(weighted, "alpha common")[0].section.slug == "rare-alpha"
    assert [r.section.slug for r in rank_sections(sections, "retry timeout")][-2:] == [
        "first-retry",
        "second-retry",
    ]


async def test_get_package_docs_query_returns_schema_validated_match(monkeypatch):
    async def loaded(*args, **kwargs):
        return (
            PackageInfo("demo", "python", "1.2.3", ""),
            "metadata\n",
            "# Demo\n## Installation\nInstall package.\n## Model Validators\nUse model_validator.",
            False,
            {
                "source": "pypi_description",
                "source_url": "https://pypi.org/pypi/demo/1.2.3/json",
                "fetched_at": 1.0,
                "metadata_refreshed": True,
                "version_binding": "registry_version",
            },
        )

    monkeypatch.setattr(server, "_load_document", loaded)
    result = await server.mcp_call_tool(
        "get_package_docs",
        {"package": "demo", "query": "model_validator", "max_tokens": 200},
    )
    assert result.isError is False
    assert "Model Validators" in result.structuredContent["content"]
    assert result.structuredContent["matched_sections"] == ["model-validators"]
    RESULT_TYPES["get_package_docs"].validate_python(result.structuredContent)


async def test_get_package_docs_query_no_match_has_outline_only(monkeypatch):
    async def loaded(*args, **kwargs):
        return (
            PackageInfo("demo", "python", "1.2.3", ""),
            "intro",
            "# Demo\n## Installation\nInstall it.",
            False,
            {
                "source": "pypi_description",
                "source_url": "https://pypi.org/",
                "fetched_at": 1.0,
                "metadata_refreshed": True,
                "version_binding": "registry_version",
            },
        )

    monkeypatch.setattr(server, "_load_document", loaded)
    result = await server.mcp_call_tool(
        "get_package_docs", {"package": "demo", "query": "unrelated"}
    )
    assert result.isError is False
    assert result.structuredContent["no_match"] is True
    assert result.structuredContent["content"] == ""
    assert result.structuredContent["section_map"]


async def test_query_and_section_are_mutually_exclusive():
    result = await server.mcp_call_tool(
        "get_package_docs", {"package": "demo", "section": "usage", "query": "install"}
    )
    assert result.isError is True
    assert result.structuredContent["error"] == "invalid_arguments"


async def test_official_docs_query_and_unknown_source(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "cache", DocsCache(tmp_path))

    async def fetch(source_id, version):
        return FetchedDocument(
            "# Tools\n## tools/list\nList tools.\n## Other\nOther details.",
            "official_markdown",
            "https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md",
        )

    monkeypatch.setattr(server, "fetch_official_source", fetch)
    result = await server.mcp_call_tool(
        "get_official_docs",
        {"source_id": "mcp-tools", "version": "2026-07-28", "query": "tools/list"},
    )
    assert result.isError is False
    assert "List tools" in result.structuredContent["content"]
    assert result.structuredContent["content_trust"] == "untrusted_upstream"
    RESULT_TYPES["get_official_docs"].validate_python(result.structuredContent)
    unknown = await server.mcp_call_tool(
        "get_official_docs", {"source_id": "unknown", "version": "2026-07-28"}
    )
    assert unknown.isError is True


async def test_roots_list_manifest_fallback_is_confined(monkeypatch, tmp_path):
    monkeypatch.delenv("UNIVERSAL_DOCS_PROJECT_ROOT", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    (project / "package.json").write_text('{"dependencies":{"demo":"1.2.3"}}')
    root = SimpleNamespace(uri=project.as_uri())
    session = SimpleNamespace(list_roots=lambda: None)

    async def list_roots():
        return SimpleNamespace(roots=[root])

    session.list_roots = list_roots
    context = SimpleNamespace(session=session)
    monkeypatch.setattr(
        type(server.server), "request_context", property(lambda self: context)
    )
    result = await server.call_tool(
        "get_project_dependencies", {"manifest_path": "package.json"}
    )
    assert json.loads(result[0].text)["found"] is True

    session.list_roots = lambda: None

    async def unsafe_roots():
        return SimpleNamespace(
            roots=[
                SimpleNamespace(uri="file:///"),
                SimpleNamespace(uri=f"file://{__import__('pathlib').Path.home()}"),
            ]
        )

    session.list_roots = unsafe_roots
    result = await server.call_tool(
        "get_project_dependencies", {"manifest_path": "package.json"}
    )
    assert json.loads(result[0].text)["error"] == "manifest_access_disabled"


async def test_no_match_zero_tokens_and_official_exact_section(monkeypatch):
    from universal_docs_mcp.compaction import select_by_query

    miss = select_by_query("# Title\n## Usage\nHello.\n", "nonexistent", header="HDR")
    assert miss["no_match"] is True
    assert miss["content"] == ""
    assert miss["tokens_included"] == 0

    async def fake_official(source_id, version, **kwargs):
        return SimpleNamespace(
            source_id=source_id,
            version=version,
            source_url="https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md",
            version_binding="versioned_url",
            fetched_at=1700000000.0,
            cached=False,
            content="# Spec\n## Tools List\nExact tools list.\n## Other Tools\nNoise.\n",
        )

    monkeypatch.setattr(server, "retrieve_official_document", fake_official)
    hit = await server.mcp_call_tool(
        "get_official_docs",
        {"source_id": "mcp-tools", "version": "2026-07-28", "section": "tools-list"},
    )
    assert hit.isError is False
    payload = json.loads(hit.content[0].text)
    assert payload["matched_sections"] == ["tools-list"]
    assert "Exact tools list." in payload["content"]
    assert "Noise." not in payload["content"]

    missing_sec = await server.mcp_call_tool(
        "get_official_docs",
        {"source_id": "mcp-tools", "version": "2026-07-28", "section": "nope"},
    )
    assert missing_sec.isError is True
    assert json.loads(missing_sec.content[0].text)["error"] == "section_not_found"
