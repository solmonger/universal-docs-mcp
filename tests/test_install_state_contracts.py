from __future__ import annotations

import io
import json
from pathlib import Path

from universal_docs_mcp import product_cli


def _tools(tmp_path: Path, monkeypatch):
    for name in ("universal-docs-command-hook", "universal-docs-preflight"):
        path = tmp_path / name
        path.write_text("#!/bin/sh\n")
        path.chmod(0o700)
        monkeypatch.setenv("UNIVERSAL_DOCS_INIT_" + name.replace("-", "_").upper(), str(path))


def _project(tmp_path: Path, monkeypatch, *, old_adapter: bytes | None = None) -> Path:
    root = tmp_path / "project"
    (root / "before").mkdir(parents=True)
    (root / "after").mkdir()
    (root / "before/requirements.txt").write_text("demo==1.0.0\n")
    (root / "after/requirements.txt").write_text("demo==1.0.1\n")
    _tools(tmp_path, monkeypatch)
    if old_adapter is not None:
        (root / ".universal-docs").mkdir()
        (root / ".universal-docs/adapter.json").write_bytes(old_adapter)
    return root


def _call(root: Path, *args: str):
    out = io.BytesIO()
    rc = product_cli.main([*args, "--project-root", str(root)], stdout=out)
    return rc, json.loads(out.getvalue())


def _apply(root: Path):
    return _call(root, "init", "--harness", "claude-code", "--before", "before/requirements.txt", "--after", "after/requirements.txt", "--apply")


def test_dry_run_has_proposed_state_but_creates_no_cache_or_state(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    rc, receipt = _call(root, "init", "--harness", "claude-code", "--before", "before/requirements.txt", "--after", "after/requirements.txt")
    assert rc == 0 and receipt["changed"] is True
    assert not (root / ".universal-docs").exists()
    assert not (root / ".claude").exists()


def test_preexisting_adapter_is_restored_from_exact_state_pin(tmp_path, monkeypatch):
    old = b'{"preexisting":true}\n'
    root = _project(tmp_path, monkeypatch, old_adapter=old)
    assert _apply(root)[0] == 0
    backups = list((root / ".universal-docs/backups").glob("adapter-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == old
    (root / ".universal-docs/backups").joinpath("adapter-deadbeef.json").write_bytes(b"historical")
    assert _call(root, "rollback", "--apply")[0] == 0
    assert (root / ".universal-docs/adapter.json").read_bytes() == old
    assert not (root / ".universal-docs/install-state.json").exists()


def test_no_adapter_preimage_removes_generated_adapter(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    assert _apply(root)[0] == 0
    assert _call(root, "rollback", "--apply")[0] == 0
    assert not (root / ".universal-docs/adapter.json").exists()
    assert _call(root, "rollback", "--apply")[1]["reason"] == "already_rolled_back"


def test_tampered_pinned_backup_fails_without_writes(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch, old_adapter=b'{"old":1}\n')
    assert _apply(root)[0] == 0
    backup = next((root / ".universal-docs/backups").glob("adapter-*.json"))
    backup.write_bytes(b"tampered")
    before = {p: p.read_bytes() for p in (root / ".universal-docs/adapter.json", root / ".claude/settings.json", root / ".universal-docs/install-state.json")}
    rc, receipt = _call(root, "rollback", "--apply")
    assert rc == 1 and receipt["reason"] == "rollback_backup_invalid"
    assert {p: p.read_bytes() for p in before} == before


def test_rollback_preserves_unrelated_current_settings_and_is_idempotent(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    assert _apply(root)[0] == 0
    settings = root / ".claude/settings.json"
    value = json.loads(settings.read_text())
    value["operator_field"] = {"keep": True}
    settings.write_text(json.dumps(value))
    assert _call(root, "rollback", "--apply")[0] == 0
    restored = json.loads(settings.read_text())
    assert restored["operator_field"] == {"keep": True}
    assert _call(root, "rollback", "--apply")[1]["changed"] is False


def test_cache_scope_distinguishes_default_and_explicit(tmp_path, monkeypatch):
    monkeypatch.delenv("UNIVERSAL_DOCS_CACHE_DIR", raising=False)
    default, status = product_cli._doctor_cache(tmp_path)
    assert default["scope"] == "default_user_installation" and status in {"pass", "unknown"}
    explicit = tmp_path / "cache"
    explicit.mkdir()
    monkeypatch.setenv("UNIVERSAL_DOCS_CACHE_DIR", str(explicit))
    info, status = product_cli._doctor_cache(tmp_path)
    assert info["scope"] == "explicit" and status == "pass"
    assert not (tmp_path / ".universal-docs/cache").exists()


def test_doctor_requires_state_and_uses_absolute_rollback_root(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    assert _apply(root)[0] == 0
    monkeypatch.setattr(product_cli, "_doctor_installation", lambda: {"status": "pass", "reason": "identity_match", "module_path": "/installed/module.py", "installed_version": "0.4.0rc2", "executable": "/installed/python"})
    rc, receipt = _call(root, "doctor")
    assert receipt["schema"] == "universal-docs.doctor/v1"
    state_check = next(item for item in receipt["checks"] if item["id"] == "install_state")
    assert state_check["status"] == "pass"
    assert "ABSOLUTE" not in receipt["rollback"]["command"]
    assert str(root) in receipt["rollback"]["command"]


def test_state_last_failure_leaves_no_active_install(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    original = product_cli._atomic_write
    def fail_state(path, raw):
        if path.name == "install-state.json":
            raise OSError("state-last")
        return original(path, raw)
    monkeypatch.setattr(product_cli, "_atomic_write", fail_state)
    rc, receipt = _apply(root)
    assert rc == 1 and receipt["reason"] == "write_failed"
    assert not (root / ".universal-docs/adapter.json").exists()
    assert not (root / ".claude/settings.json").exists()
    assert not (root / ".universal-docs/install-state.json").exists()
