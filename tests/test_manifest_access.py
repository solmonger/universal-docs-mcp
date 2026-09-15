"""The manifest tool must be explicitly scoped to a trusted local project."""

import json
import os

import pytest

from universal_docs_mcp import server


async def test_manifest_access_disabled_without_root(tmp_path, monkeypatch):
    monkeypatch.delenv("UNIVERSAL_DOCS_PROJECT_ROOT", raising=False)
    manifest = tmp_path / "package.json"
    manifest.write_text('{"dependencies":{"demo":"1.2.3"}}')
    result = await server.call_tool(
        "get_project_dependencies", {"manifest_path": str(manifest)}
    )
    assert json.loads(result[0].text)["error"] == "manifest_access_disabled"


async def test_authorized_manifest_omits_absolute_path(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIVERSAL_DOCS_PROJECT_ROOT", str(tmp_path))
    manifest = tmp_path / "package.json"
    manifest.write_text('{"dependencies":{"demo":"1.2.3"}}')
    result = await server.call_tool(
        "get_project_dependencies", {"manifest_path": "package.json"}
    )
    payload = json.loads(result[0].text)
    assert payload["found"] is True
    assert payload["dependencies"][0]["pinned"] == "1.2.3"
    assert str(tmp_path) not in result[0].text


@pytest.mark.parametrize("kind", ["outside", "symlink", "fifo"])
async def test_disallowed_manifest_never_read(tmp_path, monkeypatch, kind):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("UNIVERSAL_DOCS_PROJECT_ROOT", str(root))
    outside = tmp_path / "package.json"
    outside.write_text('{"dependencies":{"SYNTHETIC_PRIVATE":"1.2.3"}}')
    path = root / "package.json"
    if kind == "outside":
        path = outside
    elif kind == "symlink":
        path.symlink_to(outside)
    else:
        os.mkfifo(path)
    if kind == "fifo":
        # Baseline would block forever; first require the scoped-reader API.
        import inspect

        from universal_docs_mcp.lockfile import read_pins

        assert "root" in inspect.signature(read_pins).parameters
    result = await server.call_tool(
        "get_project_dependencies", {"manifest_path": str(path)}
    )
    assert json.loads(result[0].text)["error"] == "manifest_error"
    assert "SYNTHETIC" not in result[0].text
