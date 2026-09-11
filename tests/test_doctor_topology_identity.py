from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from universal_docs_mcp import product_cli


def _receipt(root: Path, command: str) -> tuple[int, dict]:
    import io

    out = io.BytesIO()
    rc = product_cli.main([command, "--project-root", str(root)], stdout=out)
    assert out.getvalue().count(b"\n") == 1
    return rc, json.loads(out.getvalue())


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_doctor_and_rollback_reject_control_parent_without_external_touch(
    tmp_path: Path, kind: str
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"fixed")
    control = root / ".universal-docs"
    if kind == "symlink":
        control.symlink_to(outside, target_is_directory=True)
    else:
        os.mkfifo(control)
    before = sentinel.read_bytes()
    for command in ("doctor", "rollback"):
        rc, receipt = _receipt(root, command)
        assert rc == 1
        assert receipt["status"] == "fail"
        assert receipt["reason"] == "output_parent_invalid"
    assert sentinel.read_bytes() == before
    assert list(outside.iterdir()) == [sentinel]


def test_state_nested_exact_keys_and_escape_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / ".universal-docs").mkdir(parents=True)
    state = {
        "schema": product_cli._STATE_SCHEMA,
        "paths": {
            "adapter": ".universal-docs/adapter.json",
            "settings": ".claude/settings.json",
            "state": product_cli._STATE_RELATIVE_PATH,
        },
        "adapter": {"sha256": "0" * 64, "preimage_sha256": None, "backup": None},
        "hook": {"command_sha256": "1" * 64, "argv_sha256": "2" * 64},
        "settings": {"preimage_sha256": None},
    }
    for mutate in (
        lambda x: x["hook"].update(extra=True),
        lambda x: x["paths"].update(extra="x"),
        lambda x: x["paths"].update(adapter="../outside"),
        lambda x: x["adapter"].update(backup=".universal-docs/backups/adapter-" + "0" * 64 + ".json", extra=True),
    ):
        candidate = json.loads(json.dumps(state))
        mutate(candidate)
        (root / ".universal-docs/install-state.json").write_text(json.dumps(candidate))
        with pytest.raises(ValueError, match="install_state_invalid"):
            product_cli._read_state(root)


def test_identity_requires_console_scripts_and_distribution_owned_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in (product_cli._HOOK_NAME, product_cli._PREFLIGHT_NAME):
        script = bin_dir / name
        script.write_text("#!/bin/sh\n")
        script.chmod(0o700)
    module_path = tmp_path / "site" / "universal_docs_mcp" / "__init__.py"
    module_path.parent.mkdir(parents=True)
    module_path.write_text("__version__ = 'test'\n")
    module = SimpleNamespace(__file__=str(module_path), __version__=product_cli.__version__)
    dist = SimpleNamespace(
        version=product_cli.__version__,
        files=[Path("universal_docs_mcp/__init__.py")],
        locate_file=lambda item: module_path,
    )
    monkeypatch.delenv("UNIVERSAL_DOCS_INIT_UNIVERSAL_DOCS_COMMAND_HOOK", raising=False)
    monkeypatch.delenv("UNIVERSAL_DOCS_INIT_UNIVERSAL_DOCS_PREFLIGHT", raising=False)
    monkeypatch.setattr(product_cli.sys, "executable", str(bin_dir / "python"))
    monkeypatch.setattr(product_cli.importlib, "import_module", lambda name: module)
    monkeypatch.setattr(product_cli.importlib.metadata, "distribution", lambda name: dist)
    assert product_cli._doctor_installation()["status"] == "pass"
    elsewhere = tmp_path / "elsewhere.py"
    elsewhere.write_text("__version__ = 'test'\n")
    module.__file__ = str(elsewhere)
    assert product_cli._doctor_installation()["reason"] == "identity_mismatch"


def test_identity_override_is_fixture_not_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNIVERSAL_DOCS_INIT_UNIVERSAL_DOCS_COMMAND_HOOK", str(tmp_path / "hook"))
    result = product_cli._doctor_installation()
    assert result["status"] == "fail"
    assert result["reason"] == "fixture_override"
    assert result["owned"] is False


def test_executable_validation_rejects_link_fifo_directory_and_non_executable(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    link = tmp_path / "link"
    link.symlink_to(directory, target_is_directory=True)
    plain = tmp_path / "plain"
    plain.write_text("x")
    for path in (directory, fifo, link, plain):
        with pytest.raises(ValueError):
            product_cli._validate_executable(path)


def test_hash_and_nul_path_validation_is_bounded(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="install_state_invalid"):
        product_cli._relative_state_path("x\x00y", tmp_path)
    with pytest.raises(ValueError, match="install_state_invalid"):
        product_cli._state_hash("not-a-hash")
    assert stat.S_ISDIR((tmp_path).stat().st_mode)
