"""Machine-checkable result contracts advertised at the MCP boundary."""

import json

import pytest
from jsonschema import Draft202012Validator, ValidationError

from universal_docs_mcp import server
from universal_docs_mcp.cache import DocsCache


async def test_cache_tool_advertises_and_satisfies_its_output_schema(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(server, "cache", DocsCache(tmp_path))
    tool = next(t for t in await server.list_tools() if t.name == "cache_stats")
    assert tool.outputSchema is not None, "cache_stats has no output contract"
    Draft202012Validator.check_schema(tool.outputSchema)
    result = await server.mcp_call_tool("cache_stats", {})
    payload = result.structuredContent
    assert payload == json.loads(result.content[0].text)
    assert result.isError is False
    validator = Draft202012Validator(tool.outputSchema)
    validator.validate(payload)
    with pytest.raises(ValidationError):
        validator.validate({"available": True, "total": "not-a-count"})
    with pytest.raises(ValidationError):
        validator.validate({**payload, "unexpected": "must-not-leak"})


@pytest.mark.parametrize("name", list(server.TOOL_ARGS))
async def test_every_tool_declares_a_strict_result_contract(name):
    tool = next(t for t in await server.list_tools() if t.name == name)
    assert tool.outputSchema is not None, f"{name} has no output contract"
    validator = Draft202012Validator(tool.outputSchema)
    validator.check_schema(tool.outputSchema)
    with pytest.raises(ValidationError):
        validator.validate({"arbitrary": "not-a-result"})
    # Application failures are data, and must satisfy the advertised contract.
    validator.validate(
        {"found": None, "error": "upstream_unavailable", "retryable": True}
    )


async def test_result_contract_violation_is_sanitized_before_sdk_validation(
    monkeypatch,
):
    import mcp.types as types

    async def invalid_result(*args):
        return [
            types.TextContent(
                type="text", text=json.dumps({"unexpected": "SYNTHETIC_OUTPUT_SECRET"})
            )
        ]

    monkeypatch.setattr(server, "_dispatch_tool", invalid_result)
    result = await server.mcp_call_tool("cache_stats", {})
    assert result.isError is True
    assert result.structuredContent["error"] == "invalid_tool_result"
    assert "SYNTHETIC" not in result.model_dump_json()


async def test_real_domain_results_match_each_schema(monkeypatch, tmp_path):
    from universal_docs_mcp.docs_fetcher import FetchedDocument
    from universal_docs_mcp.registries import PackageInfo

    monkeypatch.setattr(server, "cache", DocsCache(tmp_path / "cache"))
    monkeypatch.setenv("UNIVERSAL_DOCS_PROJECT_ROOT", str(tmp_path))
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"example": "1.2.3"}})
    )

    async def package(*args, **kwargs):
        return PackageInfo("example", "python", "1.2.3", "Offline test fixture")

    async def document(*args, **kwargs):
        return FetchedDocument(
            "# Usage\nUse the documented API.\n## Limits\nOne request.",
            "pypi_description",
            "https://pypi.org/pypi/example/1.2.3/json",
        )

    monkeypatch.setattr(server, "fetch_package", package)
    monkeypatch.setattr(server, "fetch_docs_content_with_provenance", document)
    tools = {t.name: t for t in await server.list_tools()}
    requests = [
        ("get_package_info", {"package": "example", "ecosystem": "python"}),
        ("get_package_info", {"package": "example", "ecosystem": "python"}),
        (
            "get_package_docs",
            {"package": "example", "ecosystem": "python", "version": "1.2.3"},
        ),
        (
            "get_package_docs",
            {
                "package": "example",
                "ecosystem": "python",
                "version": "1.2.3",
                "section": "usage",
            },
        ),
        (
            "get_docs_outline",
            {
                "package": "example",
                "ecosystem": "python",
                "version": "1.2.3",
                "limit": 1,
            },
        ),
        ("get_project_dependencies", {"manifest_path": "package.json"}),
        ("cache_stats", {}),
    ]
    for name, args in requests:
        result = await server.mcp_call_tool(name, args)
        assert result.isError is False, (name, result.structuredContent)
        assert result.structuredContent == json.loads(result.content[0].text)
        Draft202012Validator(tools[name].outputSchema).validate(
            result.structuredContent
        )
    missing_section = await server.mcp_call_tool(
        "get_package_docs",
        {
            "package": "example",
            "ecosystem": "python",
            "version": "1.2.3",
            "section": "missing",
        },
    )
    assert missing_section.isError is True
    Draft202012Validator(tools["get_package_docs"].outputSchema).validate(
        missing_section.structuredContent
    )
