"""Malformed names are manifest errors, not credential-bearing identities."""

import json

import pytest

from universal_docs_mcp import server
from universal_docs_mcp.lockfile import (
    parse_cargo_lock,
    parse_cargo_toml,
    parse_package_json,
)


def cases(name):
    quoted = json.dumps(name)
    return [
        (
            "package.json",
            parse_package_json,
            json.dumps({"dependencies": {name: "1.2.3"}}),
        ),
        ("Cargo.toml", parse_cargo_toml, f'[dependencies]\n{quoted} = "1.2.3"\n'),
        (
            "Cargo.toml",
            parse_cargo_toml,
            f'[dependencies]\nsafe = {{ package = {quoted}, version = "1.2.3" }}\n',
        ),
        (
            "Cargo.toml",
            parse_cargo_toml,
            f'[dependencies]\n{quoted} = {{ package = "safe", version = "1.2.3" }}\n',
        ),
        (
            "Cargo.lock",
            parse_cargo_lock,
            f'[[package]]\nname = {quoted}\nversion = "1.2.3"\nsource = "registry+https://github.com/rust-lang/crates.io-index"\n',
        ),
    ]


@pytest.mark.parametrize(
    "name",
    [
        "https://user:SYNTHETIC_NAME_TOKEN@example.test/demo",
        "git+https://user:SYNTHETIC_NAME_TOKEN@github.com/acme/demo.git",
        "../SYNTHETIC_NAME_TOKEN/private",
        "C:\\SYNTHETIC_NAME_TOKEN\\private",
        "demo\nSYNTHETIC_NAME_TOKEN",
    ],
)
@pytest.mark.parametrize("case_index", range(5))
def test_bad_dependency_name_fails_without_reflection(name, case_index):
    filename, parser, text = cases(name)[case_index]
    with pytest.raises(ValueError) as caught:
        parser(text, filename)
    assert "SYNTHETIC" not in str(caught.value)


@pytest.mark.parametrize("case_index", range(5))
async def test_bad_dependency_name_is_safe_protocol_error(
    case_index, monkeypatch, tmp_path
):
    filename, _, text = cases("https://user:SYNTHETIC_NAME_TOKEN@example.test/demo")[
        case_index
    ]
    (tmp_path / filename).write_text(text)
    monkeypatch.setenv("UNIVERSAL_DOCS_PROJECT_ROOT", str(tmp_path))
    result = await server.mcp_call_tool(
        "get_project_dependencies", {"manifest_path": filename}
    )
    assert result.isError is True
    assert result.structuredContent["error"] == "manifest_error"
    assert "SYNTHETIC" not in result.model_dump_json()
