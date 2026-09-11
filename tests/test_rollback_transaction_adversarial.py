from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from universal_docs_mcp import product_cli


def call(root: Path, *args: str) -> tuple[int, dict]:
    out = io.BytesIO()
    rc = product_cli.main([*args, "--project-root", str(root)], stdout=out)
    assert out.getvalue().count(b"\n") == 1
    assert len(out.getvalue()) <= product_cli.MAX_OUTPUT_BYTES
    return rc, json.loads(out.getvalue())


def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: bytes | None = None, settings: dict | None = None) -> Path:
    root = tmp_path / "project"
    (root / "before").mkdir(parents=True)
    (root / "after").mkdir()
    (root / "before/requirements.txt").write_text("demo==1.0.0\n")
    (root / "after/requirements.txt").write_text("demo==1.0.1\n")
    for name in ("universal-docs-command-hook", "universal-docs-preflight"):
        tool = tmp_path / name
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o700)
        monkeypatch.setenv(
            "UNIVERSAL_DOCS_INIT_" + name.replace("-", "_").upper(), str(tool)
        )
    if adapter is not None:
        (root / ".universal-docs").mkdir()
        (root / ".universal-docs/adapter.json").write_bytes(adapter)
    if settings is not None:
        (root / ".claude").mkdir()
        (root / ".claude/settings.json").write_bytes(json.dumps(settings).encode() + b"\n")
    return root


def init_apply(root: Path) -> None:
    rc, receipt = call(
        root,
        "init",
        "--harness",
        "claude-code",
        "--before",
        "before/requirements.txt",
        "--after",
        "after/requirements.txt",
        "--apply",
    )
    assert rc == 0, receipt


def snapshot(root: Path) -> dict[str, tuple[bool, bytes | None, int | None, int | None]]:
    result = {}
    for relative in (
        ".universal-docs/adapter.json",
        ".universal-docs/install-state.json",
        ".universal-docs/rollback-tombstone.json",
        ".universal-docs/recovery-marker.json",
        ".claude/settings.json",
    ):
        path = root / relative
        try:
            info = path.lstat()
        except FileNotFoundError:
            result[relative] = (False, None, None, None)
        else:
            result[relative] = (
                True,
                path.read_bytes() if path.is_file() else None,
                info.st_mode,
                info.st_mtime_ns,
            )
    backups = root / ".universal-docs/backups"
    if backups.exists():
        result["backups"] = tuple(
            (p.relative_to(root).as_posix(), p.read_bytes(), p.lstat().st_mode, p.lstat().st_mtime_ns)
            for p in sorted(backups.iterdir())
        )
    else:
        result["backups"] = ()
    return result


def test_dry_run_describes_operations_and_writes_nothing(tmp_path, monkeypatch):
    root = project(tmp_path, monkeypatch, b'{"old":1}\n')
    init_apply(root)
    settings = root / ".claude/settings.json"
    current = json.loads(settings.read_text())
    current["unrelated_after_init"] = {"kept": True}
    settings.write_text(json.dumps(current, indent=2) + "\n")
    before = snapshot(root)
    rc, receipt = call(root, "rollback")
    assert rc == 0 and receipt["changed"] is True
    assert receipt["adapter"]["restored_sha256"] == product_cli._sha256(b'{"old":1}\n')
    assert receipt["operations"] == [
        "restore_adapter",
        "remove_exact_universal_docs_hook",
        "remove_install_state",
        "write_tombstone_last",
    ]
    assert snapshot(root) == before


@pytest.mark.parametrize(
    ("adapter_preimage", "settings_preimage"),
    [(True, True), (True, False), (False, True), (False, False)],
    ids=["adapter-settings", "adapter-only", "settings-only", "neither"],
)
def test_success_restores_all_preimage_topologies_and_preserves_unrelated(
    tmp_path, monkeypatch, adapter_preimage, settings_preimage
):
    settings_seed = {"unrelated_before": True} if settings_preimage else None
    root = project(tmp_path, monkeypatch, b'{"old":1}\n' if adapter_preimage else None, settings_seed)
    init_apply(root)
    settings = root / ".claude/settings.json"
    value = json.loads(settings.read_text())
    value["unrelated_after_init"] = "preserve"
    settings.write_text(json.dumps(value) + "\n")
    before_settings = json.loads(settings.read_text())
    assert call(root, "rollback", "--apply")[0] == 0
    assert (root / ".universal-docs/adapter.json").exists() is adapter_preimage
    if settings_preimage:
        restored = json.loads(settings.read_text())
        assert restored["unrelated_before"] is True
        assert restored["unrelated_after_init"] == "preserve"
    else:
        assert json.loads(settings.read_text())["unrelated_after_init"] == "preserve"
    assert before_settings["unrelated_after_init"] == "preserve"
    assert call(root, "rollback", "--apply")[1]["changed"] is False


@pytest.mark.parametrize("boundary", ["settings_backup", "settings_write", "adapter_restore", "state_remove", "tombstone_write"])
def test_each_forward_mutation_boundary_is_failure_atomic(tmp_path, monkeypatch, boundary):
    root = project(tmp_path, monkeypatch, b'{"old":1}\n', {"unrelated_before": True})
    init_apply(root)
    before = snapshot(root)
    original_write = product_cli._atomic_write
    original_backup = product_cli._write_backup
    original_restore = product_cli._restore_file
    original_unlink = Path.unlink

    def fail_backup(path, raw, *, kind):
        if boundary == "settings_backup" and kind == "settings":
            raise OSError("injected settings backup")
        if boundary == "adapter_backup" and kind == "adapter":
            raise OSError("injected adapter backup")
        return original_backup(path, raw, kind=kind)

    def fail_write(path, raw):
        if boundary == "settings_write" and path == root / ".claude/settings.json":
            raise OSError("injected settings write")
        if boundary == "tombstone_write" and path == root / ".universal-docs/rollback-tombstone.json":
            raise OSError("injected tombstone write")
        return original_write(path, raw)

    restore_calls = 0

    def fail_restore(path, raw, mode, mtime_ns):
        nonlocal restore_calls
        restore_calls += 1
        if boundary == "adapter_restore" and path == root / ".universal-docs/adapter.json" and restore_calls == 1:
            raise OSError("injected adapter restore")
        return original_restore(path, raw, mode, mtime_ns)

    def fail_unlink(path, *args, **kwargs):
        if boundary == "state_remove" and path == root / ".universal-docs/install-state.json":
            raise OSError("injected state removal")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(product_cli, "_write_backup", fail_backup)
    monkeypatch.setattr(product_cli, "_atomic_write", fail_write)
    monkeypatch.setattr(product_cli, "_restore_file", fail_restore)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    rc, receipt = call(root, "rollback", "--apply")
    assert rc == 1 and receipt["reason"] == "rollback_failed"
    assert snapshot(root) == before


@pytest.mark.parametrize("compensation_target", ["settings", "adapter", "state", "tombstone"])
def test_each_compensation_restore_is_attempted_and_fails_closed(
    tmp_path, monkeypatch, compensation_target
):
    root = project(tmp_path, monkeypatch, b'{"old":1}\n')
    init_apply(root)
    before = snapshot(root)
    original_write = product_cli._atomic_write
    original_restore = product_cli._restore_file
    restore_calls = 0

    def fail_forward(path, raw):
        if path == root / ".universal-docs/rollback-tombstone.json":
            raise OSError("forward fault")
        return original_write(path, raw)

    target_paths = {
        "settings": root / ".claude/settings.json",
        "adapter": root / ".universal-docs/adapter.json",
        "state": root / ".universal-docs/install-state.json",
        "tombstone": root / ".universal-docs/rollback-tombstone.json",
    }
    # Forward adapter restoration is call 1; compensation restores follow.
    compensation_calls = {"settings": 2, "adapter": 3, "state": 4, "tombstone": 5}

    def fail_recovery(path, raw, mode, mtime_ns):
        nonlocal restore_calls
        restore_calls += 1
        if path == target_paths[compensation_target] and restore_calls == compensation_calls[compensation_target]:
            raise RuntimeError("injected compensation failure")
        return original_restore(path, raw, mode, mtime_ns)

    monkeypatch.setattr(product_cli, "_atomic_write", fail_forward)
    monkeypatch.setattr(product_cli, "_restore_file", fail_recovery)
    rc, receipt = call(root, "rollback", "--apply")
    assert rc == 1 and receipt["reason"] == "recovery_failed"
    marker_path = root / ".universal-docs/recovery-marker.json"
    assert marker_path.exists()
    marker = json.loads(marker_path.read_text())
    assert marker["schema"] == "universal-docs.recovery-marker/v2"
    assert set(marker) == {
        "schema",
        "state_sha256",
        "adapter_sha256",
        "settings_sha256",
        "settings_backup_sha256",
        "preimage_sha256",
    }
    assert all(not Path(value).is_absolute() for value in marker.values() if isinstance(value, str))
    state_path = root / ".universal-docs/install-state.json"
    if compensation_target == "state":
        assert not state_path.exists()
    else:
        assert state_path.read_bytes() == before[".universal-docs/install-state.json"][1]
    assert "injected" not in json.dumps(receipt)


def test_malformed_preexisting_tombstone_is_rejected_before_writes(tmp_path, monkeypatch):
    root = project(tmp_path, monkeypatch, b'{"old":1}\n')
    init_apply(root)
    tombstone = root / ".universal-docs/rollback-tombstone.json"
    tombstone.write_bytes(b"tampered")
    before = snapshot(root)
    rc, receipt = call(root, "rollback", "--apply")
    assert rc == 1 and receipt["reason"] == "rollback_tombstone_invalid"
    assert snapshot(root) == before


def test_compensation_failure_fails_closed_and_next_call_refuses(tmp_path, monkeypatch):
    root = project(tmp_path, monkeypatch, b'{"old":1}\n')
    init_apply(root)
    before = snapshot(root)
    original_write = product_cli._atomic_write
    original_restore = product_cli._restore_file
    restore_calls = 0

    def fail_tombstone(path, raw):
        if path == root / ".universal-docs/rollback-tombstone.json":
            raise OSError("forward fault")
        return original_write(path, raw)

    def fail_compensation(path, raw, mode, mtime_ns):
        nonlocal restore_calls
        restore_calls += 1
        if restore_calls == 4:  # forward adapter, compensation settings, adapter, state
            raise RuntimeError("secret exception must not escape")
        return original_restore(path, raw, mode, mtime_ns)

    monkeypatch.setattr(product_cli, "_atomic_write", fail_tombstone)
    monkeypatch.setattr(product_cli, "_restore_file", fail_compensation)
    rc, receipt = call(root, "rollback", "--apply")
    assert rc == 1 and receipt["reason"] == "recovery_failed"
    assert "secret" not in json.dumps(receipt)
    state = root / ".universal-docs/install-state.json"
    marker = root / ".universal-docs/recovery-marker.json"
    assert not state.exists() and marker.exists()
    marker_raw = marker.read_bytes()
    marker_value = json.loads(marker_raw)
    assert marker_value["state_sha256"] == product_cli._sha256(before[".universal-docs/install-state.json"][1])
    assert snapshot(root)["backups"] == before["backups"]
    monkeypatch.undo()
    rc, receipt = call(root, "init", "--harness", "claude-code", "--before", "before/requirements.txt", "--after", "after/requirements.txt", "--apply")
    assert rc == 1 and receipt["reason"] == "recovery_required"
    assert snapshot(root)["backups"] == before["backups"]
    rc, receipt = call(root, "doctor")
    assert rc == 1
    state_check = next(check for check in receipt["checks"] if check["id"] == "install_state")
    assert state_check["reason"] == "recovery_required"
    rc, receipt = call(root, "rollback", "--apply")
    assert rc == 1 and receipt["reason"] == "recovery_required"
    assert marker.read_bytes() == marker_raw and not state.exists()


def test_generated_adapter_removal_failure_restores_pre_call_topology(tmp_path, monkeypatch):
    root = project(tmp_path, monkeypatch)
    init_apply(root)
    before = snapshot(root)
    original_unlink = Path.unlink

    def fail_adapter_unlink(path, *args, **kwargs):
        if path == root / ".universal-docs/adapter.json":
            raise OSError("generated adapter removal")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_adapter_unlink)
    rc, receipt = call(root, "rollback", "--apply")
    assert rc == 1 and receipt["reason"] == "rollback_failed"
    assert snapshot(root) == before


def test_initial_marker_write_failure_is_zero_mutation(tmp_path, monkeypatch):
    root = project(tmp_path, monkeypatch, b'{"old":1}\n')
    init_apply(root)
    before = snapshot(root)
    original_write = product_cli._atomic_write

    def fail_marker(path, raw):
        if path == root / ".universal-docs/recovery-marker.json":
            raise OSError("marker write")
        return original_write(path, raw)

    monkeypatch.setattr(product_cli, "_atomic_write", fail_marker)
    rc, receipt = call(root, "rollback", "--apply")
    assert rc == 1 and receipt["reason"] == "rollback_failed"
    assert snapshot(root) == before


def test_marker_remove_failure_is_recovery_failed_and_quarantines(tmp_path, monkeypatch):
    root = project(tmp_path, monkeypatch, b'{"old":1}\n')
    init_apply(root)
    original_unlink = Path.unlink

    def fail_marker_remove(path, *args, **kwargs):
        if path == root / ".universal-docs/recovery-marker.json":
            raise OSError("marker remove")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_marker_remove)
    rc, receipt = call(root, "rollback", "--apply")
    assert rc == 1 and receipt["reason"] == "recovery_failed"
    assert (root / ".universal-docs/recovery-marker.json").is_file()
    assert not (root / ".universal-docs/install-state.json").exists()
    monkeypatch.undo()
    for command in (
        ["init", "--harness", "claude-code", "--before", "before/requirements.txt", "--after", "after/requirements.txt", "--apply"],
        ["doctor"],
        ["rollback", "--apply"],
    ):
        rc, receipt = call(root, *command)
        assert rc == 1
        if command[0] != "doctor":
            assert receipt["reason"] == "recovery_required"


def test_new_settings_backup_survives_cleanup_failure_hash_bound(tmp_path, monkeypatch):
    root = project(tmp_path, monkeypatch)
    init_apply(root)
    before = snapshot(root)
    original_unlink = Path.unlink
    original_write = product_cli._atomic_write

    def fail_tombstone(path, raw):
        if path == root / ".universal-docs/rollback-tombstone.json":
            raise OSError("tombstone write")
        return original_write(path, raw)

    def fail_backup_cleanup(path, *args, **kwargs):
        if path.name.startswith("settings-"):
            raise OSError("backup cleanup")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_backup_cleanup)
    monkeypatch.setattr(product_cli, "_atomic_write", fail_tombstone)
    rc, receipt = call(root, "rollback", "--apply")
    assert rc == 1 and receipt["reason"] == "recovery_failed"
    assert snapshot(root)[".universal-docs/install-state.json"] == before[".universal-docs/install-state.json"]
    settings_backups = [entry for entry in snapshot(root)["backups"] if entry[0].startswith(".universal-docs/backups/settings-")]
    assert len(settings_backups) == 1
    relative, raw, _mode, _mtime = settings_backups[0]
    assert relative.endswith(product_cli._sha256(raw) + ".json")
    assert json.loads(raw) == json.loads((root / ".claude/settings.json").read_bytes())
