"""Real product_cli.main coverage for the transactional init command."""
from __future__ import annotations

import io
import json
import os
import shlex
from pathlib import Path

import pytest

from universal_docs_mcp import product_cli

PYTHON = "/Users/operator/Desktop/research/universal-docs-mcp/.venv/bin/python"
HOOK = "/Users/operator/Desktop/research/universal-docs-mcp/.venv/bin/universal-docs-command-hook"
PREFLIGHT = "/Users/operator/Desktop/research/universal-docs-mcp/.venv/bin/universal-docs-preflight"


def call(root: Path, *extra: str, apply: bool = False) -> tuple[int, dict]:
    (root / "before").mkdir(parents=True, exist_ok=True)
    (root / "after").mkdir(exist_ok=True)
    (root / "before" / "requirements.txt").write_text("demo==1.0.0\n")
    (root / "after" / "requirements.txt").write_text("demo==1.0.1\n")
    out = io.BytesIO()
    args = ["init", "--harness", "claude-code", "--project-root", str(root),
            "--before", "before/requirements.txt", "--after", "after/requirements.txt"]
    if apply:
        args.append("--apply")
    old = os.environ.copy()
    os.environ.update({"UNIVERSAL_DOCS_INIT_UNIVERSAL_DOCS_COMMAND_HOOK": HOOK,
                       "UNIVERSAL_DOCS_INIT_UNIVERSAL_DOCS_PREFLIGHT": PREFLIGHT})
    try:
        rc = product_cli.main(args + list(extra), stdout=out)
    finally:
        os.environ.clear()
        os.environ.update(old)
    return rc, json.loads(out.getvalue())


def outputs(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_dry_run_is_selected_and_writes_nothing(tmp_path: Path):
    rc, receipt = call(tmp_path)
    assert rc == 0 and receipt["status"] == "selected"
    assert outputs(tmp_path) == {"before/requirements.txt", "after/requirements.txt"}


def test_clean_apply_readback_and_shell_quoting(tmp_path: Path):
    root = tmp_path / "project with spaces"
    rc, receipt = call(root, apply=True)
    assert rc == 0
    assert outputs(root) == {"before/requirements.txt", "after/requirements.txt", ".universal-docs/adapter.json", ".claude/settings.json"}
    settings = json.loads((root / ".claude/settings.json").read_text())
    command = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "universal-docs-command-hook" in command and shlex.quote(str(root / ".universal-docs/adapter.json")) in command
    assert receipt["proposed_relative_paths"] == [".universal-docs/adapter.json", ".claude/settings.json"]
    assert (root / ".universal-docs/adapter.json").stat().st_mode & 0o777 == 0o600


def test_existing_preimages_are_backed_up_and_bound(tmp_path: Path):
    (tmp_path / ".universal-docs").mkdir()
    (tmp_path / ".claude").mkdir()
    adapter = b'{"old":true}\n'
    settings = b'{"permissions":{"allow":[]}}\n'
    (tmp_path / ".universal-docs/adapter.json").write_bytes(adapter)
    (tmp_path / ".claude/settings.json").write_bytes(settings)
    rc, receipt = call(tmp_path, apply=True)
    assert rc == 0
    assert (tmp_path / ".universal-docs/adapter.json").read_bytes() != adapter
    assert (tmp_path / ".universal-docs/adapter.json.backup").read_bytes() == adapter
    assert (tmp_path / ".universal-docs/settings.json.backup").read_bytes() == settings
    assert {x["relative_path"] for x in receipt["backups"]} == {".universal-docs/adapter.json.backup", ".universal-docs/settings.json.backup"}


def test_backup_conflict_is_zero_write(tmp_path: Path):
    (tmp_path / ".universal-docs").mkdir()
    (tmp_path / ".universal-docs/adapter.json").write_text('{"old":true}\n')
    (tmp_path / ".universal-docs/adapter.json.backup").write_text('{"other":true}\n')
    before = outputs(tmp_path) | {"before/requirements.txt", "after/requirements.txt"}
    rc, receipt = call(tmp_path, apply=True)
    assert rc == 1 and receipt["reason"] == "backup_conflict"
    assert outputs(tmp_path) == before
    assert (tmp_path / ".universal-docs/adapter.json").read_text() == '{"old":true}\n'


@pytest.mark.parametrize("name,content,reason", [
    ("settings.json", b"x" * (64 * 1024 + 1), "settings_not_regular"),
    ("adapter.json", b"x" * (64 * 1024 + 1), "adapter_invalid"),
])
def test_oversized_existing_files_abstain(tmp_path: Path, name, content, reason):
    target = tmp_path / (".claude" if name.startswith("settings") else ".universal-docs")
    target.mkdir()
    (target / name).write_bytes(content)
    rc, receipt = call(tmp_path)
    assert rc == 1 and receipt["reason"] == reason


def test_symlink_and_special_are_rejected(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").symlink_to(tmp_path / "missing")
    assert call(tmp_path)[1]["reason"] == "settings_not_regular"
    (tmp_path / ".claude/settings.json").unlink()
    os.mkfifo(tmp_path / ".claude/settings.json")
    assert call(tmp_path)[1]["reason"] == "settings_not_regular"


def test_malformed_duplicate_and_conflicting_hook(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    p = tmp_path / ".claude/settings.json"
    p.write_text('{"hooks":{},"hooks":{}}')
    assert call(tmp_path)[1]["reason"] == "duplicate_json_key"
    p.write_text('{"hooks":{"UserPromptSubmit": [{"hooks": [{"type":"command","command":"/x/universal-docs-command-hook --wrong"}]}]}}')
    assert call(tmp_path)[1]["reason"] == "conflicting_universal_docs_hook"


def test_idempotence_and_injected_adapter_failure_leave_settings_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert call(tmp_path, apply=True)[0] == 0
    settings_path = tmp_path / ".claude/settings.json"
    before = settings_path.read_bytes()
    assert call(tmp_path, apply=True)[0] == 0 and settings_path.read_bytes() == before
    original = product_cli._atomic_write
    def fail_adapter(path, raw):
        if path.name == "adapter.json":
            raise OSError("secret path must not escape")
        return original(path, raw)
    monkeypatch.setattr(product_cli, "_atomic_write", fail_adapter)
    assert call(tmp_path, apply=True)[1]["reason"] == "write_failed"
    assert settings_path.read_bytes() == before


def test_path_escape_and_unselected_plan_abstain(tmp_path: Path):
    (tmp_path / "before").mkdir()
    (tmp_path / "after").mkdir()
    (tmp_path / "before/requirements.txt").write_text("demo==1.0.0\n")
    (tmp_path / "after/requirements.txt").write_text("demo==1.0.1\n")
    out = io.BytesIO()
    rc = product_cli.main(["init", "--harness", "claude-code", "--project-root", str(tmp_path),
                           "--before", "../outside", "--after", "after/requirements.txt"], stdout=out)
    assert rc == 1 and json.loads(out.getvalue())["reason"] == "manifest_path_escape"
    (tmp_path / "after/requirements.txt").write_text("demo==1.0.0\n")
    out = io.BytesIO()
    rc = product_cli.main(["init", "--harness", "claude-code", "--project-root", str(tmp_path),
                           "--before", "before/requirements.txt", "--after", "after/requirements.txt"], stdout=out)
    assert rc == 1 and json.loads(out.getvalue())["reason"] == "dependency_unchanged"
