"""Adversarial init transaction and dry-run atomicity coverage."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from universal_docs_mcp import product_cli


@pytest.fixture(autouse=True)
def init_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("universal-docs-command-hook", "universal-docs-preflight"):
        path = tmp_path.parent / f"{tmp_path.name}-{name}"
        path.write_text("#!/bin/sh\n")
        path.chmod(0o700)
        monkeypatch.setenv("UNIVERSAL_DOCS_INIT_" + name.replace("-", "_").upper(), str(path))


def invoke(root: Path, *, apply: bool, after: str = "1.0.1") -> tuple[int, dict]:
    (root / "before").mkdir(parents=True, exist_ok=True)
    (root / "after").mkdir(exist_ok=True)
    (root / "before/requirements.txt").write_text("demo==1.0.0\n")
    (root / "after/requirements.txt").write_text(f"demo=={after}\n")
    output = io.BytesIO()
    argv = ["init", "--harness", "claude-code", "--project-root", str(root),
            "--before", "before/requirements.txt", "--after", "after/requirements.txt"]
    if apply:
        argv.append("--apply")
    code = product_cli.main(argv, stdout=output)
    return code, json.loads(output.getvalue())


def lstate(root: Path) -> dict[Path, tuple[bytes | None, int | None, int | None]]:
    paths = [root / ".universal-docs/adapter.json", root / ".claude/settings.json",
             root / ".universal-docs/install-state.json"]
    result = {}
    for path in paths:
        try:
            info = path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            result[path] = (None, None, None)
        else:
            result[path] = (path.read_bytes(), info.st_mode, info.st_mtime_ns)
    return result


def assert_no_generated_temps(root: Path) -> None:
    assert not [path for path in root.rglob("*") if path.name.startswith(
        (".adapter.json.", ".settings.json.", ".install-state.json.")) or ".restore." in path.name]


@pytest.mark.parametrize("topology", ["missing", "adapter", "settings", "both"])
def test_dry_run_is_zero_write_for_every_valid_topology(tmp_path: Path, topology: str) -> None:
    if topology in {"adapter", "both"}:
        (tmp_path / ".universal-docs").mkdir()
        (tmp_path / ".universal-docs/adapter.json").write_bytes(b'{"old":true}\n')
    if topology in {"settings", "both"}:
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude/settings.json").write_bytes(b'{"other":true}\n')
    sentinel = tmp_path / "outside-sentinel"
    sentinel.write_bytes(b"do not touch")
    before = lstate(tmp_path)
    all_before = {p: (p.lstat().st_mode, p.read_bytes()) for p in tmp_path.rglob("*") if p.is_file()}
    code, receipt = invoke(tmp_path, apply=False)
    assert code == 0 and receipt["status"] == "selected"
    assert lstate(tmp_path) == before
    after = {p: (p.lstat().st_mode, p.read_bytes()) for p in tmp_path.rglob("*") if p.is_file()}
    assert {p: v for p, v in after.items() if p.name != "requirements.txt"} == {p: v for p, v in all_before.items() if p.name != "requirements.txt"}
    assert not (tmp_path / ".universal-docs/backups").exists()


@pytest.mark.parametrize("bad", ["malformed-settings", "conflict", "symlink-parent", "fifo-parent"])
def test_invalid_paths_are_zero_write_in_dry_run_and_apply(tmp_path: Path, bad: str) -> None:
    if bad == "malformed-settings":
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude/settings.json").write_bytes(b"{not-json")
    elif bad == "conflict":
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude/settings.json").write_text('{"hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command","command":"/x/universal-docs-command-hook --wrong"}]}]}}')
    elif bad == "symlink-parent":
        external = tmp_path / "external"
        external.mkdir()
        (tmp_path / ".claude").symlink_to(external, target_is_directory=True)
        (external / "sentinel").write_bytes(b"external")
    else:
        os.mkfifo(tmp_path / ".universal-docs")
    before = lstate(tmp_path)
    code1, receipt1 = invoke(tmp_path, apply=False)
    code2, receipt2 = invoke(tmp_path, apply=True)
    assert code1 == code2 == 1
    assert receipt1["status"] == receipt2["status"] == "abstained"
    assert lstate(tmp_path) == before
    assert_no_generated_temps(tmp_path)
    assert len(json.dumps(receipt2).encode()) < product_cli.MAX_OUTPUT_BYTES
    assert "Traceback" not in json.dumps(receipt2)


def existing_project(root: Path) -> dict[Path, tuple[bytes, int, int]]:
    (root / ".universal-docs").mkdir(parents=True)
    (root / ".claude").mkdir()
    (root / ".universal-docs/adapter.json").write_bytes(b'{"old":true}\n')
    (root / ".claude/settings.json").write_bytes(b'{"permissions":{"allow":[]}}\n')
    (root / ".claude/unrelated.json").write_bytes(b'{"keep":true}\n')
    return {p: (p.read_bytes(), p.lstat().st_mode, p.lstat().st_mtime_ns) for p in (
        root / ".universal-docs/adapter.json", root / ".claude/settings.json", root / ".claude/unrelated.json")}


@pytest.mark.parametrize("boundary", ["backup", "adapter", "settings", "state", "cleanup"])
def test_each_forward_mutation_boundary_recovers_and_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    before = existing_project(tmp_path)
    original_backup, original_atomic = product_cli._write_backup, product_cli._atomic_write
    backup_calls = 0
    def fail_backup(path: Path, raw: bytes, *, kind: str) -> None:
        nonlocal backup_calls
        backup_calls += 1
        if boundary == "backup" and backup_calls == 1:
            raise OSError("injected backup /private/secret")
        return original_backup(path, raw, kind=kind)
    def fail_atomic(path: Path, raw: bytes) -> None:
        target = "state" if path.name == "install-state.json" else path.stem
        if boundary == target:
            raise OSError("injected atomic /private/secret")
        return original_atomic(path, raw)
    monkeypatch.setattr(product_cli, "_write_backup", fail_backup)
    monkeypatch.setattr(product_cli, "_atomic_write", fail_atomic)
    if boundary == "cleanup":
        original_unlink = product_cli.Path.unlink
        def fail_cleanup(path: Path, *args: object, **kwargs: object) -> None:
            if path.name == "rollback-tombstone.json":
                raise OSError("injected cleanup /private/secret")
            return original_unlink(path, *args, **kwargs)
        monkeypatch.setattr(product_cli.Path, "unlink", fail_cleanup)
    code, receipt = invoke(tmp_path, apply=True)
    assert code == 1 and receipt["reason"] in {"write_failed", "recovery_failed"}
    assert "/private/secret" not in json.dumps(receipt)
    assert lstate(tmp_path)[tmp_path / ".universal-docs/install-state.json"][0] is None
    assert_no_generated_temps(tmp_path)
    for path, (raw, mode, mtime) in before.items():
        assert (path.read_bytes(), path.lstat().st_mode, path.lstat().st_mtime_ns) == (raw, mode, mtime)
    assert (tmp_path / ".claude/unrelated.json").read_bytes() == b'{"keep":true}\n'
    for path in (tmp_path / ".universal-docs/backups").glob("*.json") if (tmp_path / ".universal-docs/backups").exists() else []:
        assert product_cli._sha256(path.read_bytes()) == path.stem.split("-", 1)[1]
    monkeypatch.undo()
    assert invoke(tmp_path, apply=True)[0] == 0
    assert invoke(tmp_path, apply=True)[1]["changed"] is False


def test_recovery_failure_is_bounded_and_never_claims_mismatched_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    existing_project(tmp_path)
    original_atomic = product_cli._atomic_write
    def fail_settings(path: Path, raw: bytes) -> None:
        if path.name == "settings.json":
            raise OSError("forward failure /Users/operator/private")
        return original_atomic(path, raw)
    monkeypatch.setattr(product_cli, "_atomic_write", fail_settings)
    monkeypatch.setattr(product_cli, "_restore_file", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("recovery /Users/operator/private")))
    code, receipt = invoke(tmp_path, apply=True)
    assert code == 1
    assert receipt == {"schema": product_cli._INIT_SCHEMA, "mode": "apply", "status": "abstained", "reason": "recovery_failed"}
    assert not (tmp_path / ".universal-docs/install-state.json").exists()
    assert_no_generated_temps(tmp_path)


@pytest.mark.parametrize("operation", ["backup_mkdir", "backup_open", "backup_fdopen", "backup_write", "backup_flush", "backup_fsync"])
def test_backup_mutation_boundaries_are_injectable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    backups = tmp_path / ".universal-docs/backups"
    backups.mkdir(parents=True)
    raw = b'{"old":true}\n'
    target = backups / f"adapter-{product_cli._sha256(raw)}.json"
    real_mkdir, real_open, real_fdopen, real_fsync = product_cli.Path.mkdir, product_cli.os.open, product_cli.os.fdopen, product_cli.os.fsync
    def fail_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path == backups and operation == "backup_mkdir":
            raise OSError("mkdir /private/secret")
        return real_mkdir(path, *args, **kwargs)
    def fail_open(path: object, *args: object, **kwargs: object) -> int:
        if Path(path) == target and operation == "backup_open":
            raise OSError("open /private/secret")
        return real_open(path, *args, **kwargs)
    def fail_fdopen(*args: object, **kwargs: object):
        if operation == "backup_fdopen":
            raise OSError("fdopen /private/secret")
        stream = real_fdopen(*args, **kwargs)  # type: ignore[arg-type]
        if operation not in {"backup_write", "backup_flush"}:
            return stream
        class FailingStream:
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return stream.__exit__(*exc)
            def write(self, value):
                if operation == "backup_write":
                    raise OSError("write /private/secret")
                return stream.write(value)
            def flush(self):
                if operation == "backup_flush":
                    raise OSError("flush /private/secret")
                return stream.flush()
            def fileno(self):
                return stream.fileno()
        return FailingStream()
    def fail_fsync(fd: int) -> None:
        if operation == "backup_fsync":
            raise OSError("fsync /private/secret")
        return real_fsync(fd)
    monkeypatch.setattr(product_cli.Path, "mkdir", fail_mkdir)
    monkeypatch.setattr(product_cli.os, "open", fail_open)
    monkeypatch.setattr(product_cli.os, "fdopen", fail_fdopen)
    monkeypatch.setattr(product_cli.os, "fsync", fail_fsync)
    with pytest.raises(OSError):
        product_cli._write_backup(target, raw, kind="adapter")
    assert not target.exists() and not any(backups.iterdir())


@pytest.mark.parametrize("target_name", ["adapter", "settings", "state"])
@pytest.mark.parametrize("operation", ["atomic_temp", "atomic_fdopen", "atomic_fchmod", "atomic_fsync", "atomic_replace"])
def test_atomic_boundaries_preserve_preimage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_name: str, operation: str
) -> None:
    names = {"adapter": ".universal-docs/adapter.json", "settings": ".claude/settings.json", "state": ".universal-docs/install-state.json"}
    target = tmp_path / names[target_name]
    target.parent.mkdir(parents=True, exist_ok=True)
    old = b'{"old":true}\n'
    target.write_bytes(old)
    real_mkstemp, real_fdopen, real_fchmod, real_fsync, real_replace = product_cli.tempfile.mkstemp, product_cli.os.fdopen, product_cli.os.fchmod, product_cli.os.fsync, product_cli.os.replace
    def fail_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        if operation == "atomic_temp":
            raise OSError("temp /private/secret")
        return real_mkstemp(*args, **kwargs)
    def fail_fdopen(*args: object, **kwargs: object):
        if operation == "atomic_fdopen":
            raise OSError("write /private/secret")
        return real_fdopen(*args, **kwargs)
    def fail_fchmod(fd: int, mode: int) -> None:
        if operation == "atomic_fchmod":
            raise OSError("fchmod /private/secret")
        return real_fchmod(fd, mode)
    def fail_fsync(fd: int) -> None:
        if operation == "atomic_fsync":
            raise OSError("fsync /private/secret")
        return real_fsync(fd)
    def fail_replace(source: object, destination: object) -> None:
        if Path(destination) == target and operation == "atomic_replace":
            raise OSError("replace /private/secret")
        return real_replace(source, destination)
    monkeypatch.setattr(product_cli.tempfile, "mkstemp", fail_mkstemp)
    monkeypatch.setattr(product_cli.os, "fdopen", fail_fdopen)
    monkeypatch.setattr(product_cli.os, "fchmod", fail_fchmod)
    monkeypatch.setattr(product_cli.os, "fsync", fail_fsync)
    monkeypatch.setattr(product_cli.os, "replace", fail_replace)
    with pytest.raises(OSError):
        product_cli._atomic_write(target, b'{"new":true}\n')
    assert target.read_bytes() == old
    assert_no_generated_temps(tmp_path)


@pytest.mark.parametrize("target_name", ["adapter.json", "settings.json", "install-state.json"])
def test_atomic_chmod_failure_recovers_transaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_name: str) -> None:
    existing_project(tmp_path)
    real_chmod = product_cli.os.chmod
    calls = 0
    def fail_once(path: object, mode: int, *args: object, **kwargs: object) -> None:
        nonlocal calls
        if Path(path).name == target_name and calls == 0:
            calls += 1
            raise OSError("chmod /private/secret")
        return real_chmod(path, mode, *args, **kwargs)
    monkeypatch.setattr(product_cli.os, "chmod", fail_once)
    code, receipt = invoke(tmp_path, apply=True)
    assert code == 1 and receipt["reason"] == "write_failed"
    assert lstate(tmp_path)[tmp_path / ".universal-docs/install-state.json"][0] is None
    assert (tmp_path / ".universal-docs/adapter.json").read_bytes() == b'{"old":true}\n'
    assert (tmp_path / ".claude/settings.json").read_bytes() == b'{"permissions":{"allow":[]}}\n'
    assert_no_generated_temps(tmp_path)
