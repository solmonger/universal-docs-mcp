from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from universal_docs_mcp import product_cli


def invoke(argv: list[str], *, stream=None):
    stream = stream or io.BytesIO()
    rc = product_cli.main(argv, stdout=stream)
    raw = stream.getvalue()
    if isinstance(raw, str):
        raw = raw.encode()
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1
    assert len(raw) <= product_cli.MAX_OUTPUT_BYTES
    return rc, json.loads(raw)


@pytest.mark.parametrize(
    ("command", "schema"),
    [
        ("init", "universal-docs.init/v1"),
        ("doctor", "universal-docs.doctor/v1"),
        ("rollback", "universal-docs.rollback/v1"),
    ],
)
def test_missing_and_unknown_arguments_are_one_bounded_receipt(command, schema):
    for argv in ([command], [command, "--not-an-option"], [command, "--apply", "--apply"]):
        rc, receipt = invoke(list(argv))
        assert rc != 0
        assert receipt["schema"] == schema
        assert receipt["status"] in {"fail", "abstained"}


def test_root_matrix_is_command_scoped_and_write_free(tmp_path: Path):
    commands = {
        "init": "universal-docs.init/v1",
        "doctor": "universal-docs.doctor/v1",
        "rollback": "universal-docs.rollback/v1",
    }
    regular = tmp_path / "regular"
    regular.write_text("not a directory")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    roots = ["relative", str(tmp_path / "missing"), str(regular), str(link), str(fifo)]
    before = tmp_path / "before.txt"
    before.write_text("before")
    after = tmp_path / "after.txt"
    after.write_text("after")
    valid = tmp_path / "valid"
    valid.mkdir()
    for command, schema in commands.items():
        if command == "init":
            argv = ["init", "--harness", "claude-code", "--project-root", str(valid), "--before", "before.txt", "--after", "after.txt"]
        else:
            argv = [command, "--project-root", str(valid)]
        rc, receipt = invoke(argv)
        assert receipt["schema"] == schema

        for root in roots:
            if command == "init":
                argv = ["init", "--harness", "claude-code", "--project-root", root, "--before", str(before), "--after", str(after)]
            else:
                argv = [command, "--project-root", root]
            snapshot = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
            rc, receipt = invoke(argv)
            assert rc != 0
            assert receipt["schema"] == schema
            assert receipt["status"] == "fail" if command != "init" else receipt["status"] == "abstained"
            assert receipt["reason"] == "project_root_invalid"
            assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == snapshot


def test_duplicate_guard_runs_before_parser_for_same_and_different_values(tmp_path, monkeypatch):
    root = str(tmp_path)
    cases = [
        ["doctor", "--project-root", root, "--project-root", root + "-other"],
        ["rollback", "--project-root", root, "--project-root", root],
        ["rollback", "--project-root", root, "--apply", "--apply"],
        ["init", "--harness", "claude-code", "--harness", "claude-code", "--project-root", root, "--before", "a", "--after", "b"],
        ["init", "--harness", "claude-code", "--project-root", root, "--project-root", root, "--before", "a", "--after", "b"],
        ["init", "--harness", "claude-code", "--project-root", root, "--before", "a", "--after", "b", "--after", "c"],
        ["init", "--harness", "claude-code", "--project-root", root, "--before", "a", "--after", "b", "--apply", "--apply"],
        ["init", "--harness", "claude-code", "--project-root", root, "--before", "a", "--before", "b", "--after", "c"],
    ]
    def parser_must_not_run():
        raise AssertionError("argparse ran before duplicate guard")
    monkeypatch.setattr(product_cli, "_parser", parser_must_not_run)
    for argv in cases:
        rc, receipt = invoke(argv)
        assert rc == 1
        assert receipt["schema"] == {"init": "universal-docs.init/v1", "doctor": "universal-docs.doctor/v1", "rollback": "universal-docs.rollback/v1"}[argv[0]]
        assert receipt["reason"] == "arguments_invalid"


@pytest.mark.parametrize("root_kind", ["missing", "file", "symlink", "fifo"])
def test_invalid_project_roots_are_command_receipts(tmp_path: Path, root_kind: str):
    root = tmp_path / "root"
    if root_kind == "file":
        root.write_text("x")
    elif root_kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        root.symlink_to(target, target_is_directory=True)
    elif root_kind == "fifo":
        os.mkfifo(root)
    rc, receipt = invoke(["rollback", "--project-root", str(root)])
    assert rc != 0
    assert receipt["schema"] == "universal-docs.rollback/v1"
    assert receipt["status"] == "fail"
    assert str(tmp_path) not in json.dumps(receipt)


def test_owner_exception_messages_never_cross_receipt_boundary(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    secret = "SECRET" + ("x" * 100_000) + "\x00\x1b[31m"
    monkeypatch.setattr(product_cli, "_doctor_installation", lambda: (_ for _ in ()).throw(RuntimeError(secret)))
    rc, receipt = invoke(["doctor", "--project-root", str(root)])
    assert rc == 1
    assert receipt["reason"] == "doctor_invalid"
    assert "SECRET" not in json.dumps(receipt)


def test_doctor_drops_non_load_bearing_metadata_when_receipt_overflows(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    huge = "x" * 100_000
    monkeypatch.setattr(
        product_cli,
        "_doctor_installation",
        lambda: {"status": "fail", "reason": "identity_mismatch", "module_path": huge, "installed_version": huge, "executable": huge},
    )
    rc, receipt = invoke(["doctor", "--project-root", str(root)])
    assert rc == 1
    assert receipt["status"] == "fail"
    assert len(receipt["checks"]) >= 1
    assert all(set(item) == {"id", "status", "reason", "metadata"} for item in receipt["checks"])


def test_oversized_argv_is_constant_bounded_failure():
    rc, receipt = invoke(["doctor", "--project-root", "x" * (128 * 1024)])
    assert rc == 1
    assert receipt == {"schema": "universal-docs.doctor/v1", "status": "fail", "reason": "arguments_too_large"}


def test_malformed_path_token_is_bounded_failure():
    rc, receipt = invoke(["doctor", "--project-root", "bad\x00path"])
    assert rc == 1
    assert receipt["schema"] == "universal-docs.doctor/v1"
    assert "\x00" not in json.dumps(receipt)


def test_text_writer_is_supported_without_second_emission(tmp_path: Path):
    stream = io.StringIO()
    rc, receipt = invoke(["doctor", "--project-root", str(tmp_path)], stream=stream)
    assert rc == 1
    assert receipt["schema"] == "universal-docs.doctor/v1"


def test_short_writer_raises_without_claiming_success(tmp_path: Path):
    class Short:
        def write(self, value):
            return 0

        def flush(self):
            pass

    with pytest.raises(OSError, match="output_write_failed"):
        product_cli.main(["doctor", "--project-root", str(tmp_path)], stdout=Short())


_EVIL = "SECRET" + ("x" * 100_000) + " /private/absolute\x00\x1b[31m"


def _init_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, both: bool = False) -> Path:
    root = tmp_path / "project"
    (root / "before").mkdir(parents=True)
    (root / "after").mkdir()
    (root / "before/requirements.txt").write_text("demo==1.0.0\n")
    (root / "after/requirements.txt").write_text("demo==1.0.1\n")
    for name in ("universal-docs-command-hook", "universal-docs-preflight"):
        tool = tmp_path / name
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o700)
        monkeypatch.setenv("UNIVERSAL_DOCS_INIT_" + name.replace("-", "_").upper(), str(tool))
    if both:
        (root / ".universal-docs").mkdir()
        (root / ".claude").mkdir()
        (root / ".universal-docs/adapter.json").write_bytes(b'{"old":true}\n')
        (root / ".claude/settings.json").write_bytes(b'{"unrelated":{"keep":true}}\n')
    return root


def _init_argv(root: Path, *, apply: bool = False) -> list[str]:
    argv = ["init", "--harness", "claude-code", "--project-root", str(root),
            "--before", "before/requirements.txt", "--after", "after/requirements.txt"]
    return argv + (["--apply"] if apply else [])


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def _assert_safe(rc: int, payload: dict, schema: str, *, reason: str | None = None) -> None:
    assert rc != 0
    assert payload["schema"] == schema
    assert payload["status"] in {"fail", "abstained"}
    if reason is not None:
        assert payload["reason"] == reason
    encoded = json.dumps(payload)
    assert len(encoded.encode()) <= product_cli.MAX_OUTPUT_BYTES
    assert "SECRET" not in encoded and "/private/absolute" not in encoded
    assert "\\x1b" not in encoded and "\\x00" not in encoded


@pytest.mark.parametrize("exception", [OSError, RuntimeError, ValueError])
def test_each_doctor_owner_exception_is_contained(tmp_path: Path, monkeypatch, exception):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(product_cli, "_doctor_installation",
                        lambda: (_ for _ in ()).throw(exception(_EVIL)))
    rc, payload = invoke(["doctor", "--project-root", str(root)])
    _assert_safe(rc, payload, product_cli._DOCTOR_SCHEMA, reason="doctor_invalid")


@pytest.mark.parametrize("seam", ["read", "cache", "probe"])
@pytest.mark.parametrize("exception", [OSError, RuntimeError, ValueError])
def test_each_remaining_doctor_owner_exception_is_reached_and_contained(
    tmp_path: Path, monkeypatch, seam, exception
):
    root = tmp_path / "project"
    root.mkdir()
    # Keep the surrounding topology valid enough to reach the selected owner.
    monkeypatch.setattr(product_cli, "_doctor_installation", lambda: {
        "status": "fail", "reason": "fixture", "module_path": None,
        "installed_version": None, "executable": "python", "scripts": {},
    })
    if seam == "read":
        def bad_read(path, reason):
            raise exception(_EVIL)
        monkeypatch.setattr(product_cli, "_doctor_read_json", bad_read)
    elif seam == "cache":
        monkeypatch.setattr(product_cli, "_doctor_cache",
                            lambda root: (_ for _ in ()).throw(exception(_EVIL)))
    else:
        monkeypatch.setattr(product_cli, "_doctor_cache",
                            lambda root: ({"scope": "test", "root_status": "missing",
                                           "identity": "x", "available": False,
                                           "entries": {}, "reason": "test",
                                           "owner": "test", "provenance": "test"}, "pass"))
        monkeypatch.setattr(product_cli, "_doctor_probe",
                            lambda *args: (_ for _ in ()).throw(exception(_EVIL)))
        monkeypatch.setattr(product_cli, "_validate_executable", lambda path: path)
        monkeypatch.setattr(product_cli, "_installation_executable", lambda name: root / name)
        monkeypatch.setattr(product_cli, "_hook_command", lambda *args: "hook")
        monkeypatch.setattr(product_cli, "load_config", lambda path: type("Config", (), {
            "command": (str(root / "universal-docs-preflight"),), "timeout_ms": 30_000})())
        def valid_read(path, reason):
            if path.name == "adapter.json":
                return {"request": {}}, b"{}", None
            return {"hooks": {"UserPromptSubmit": [{"hooks": [{
                "type": "command", "command": "hook", "timeout": 30}]}]}}, b"{}", None
        monkeypatch.setattr(product_cli, "_doctor_read_json", valid_read)
    rc, payload = invoke(["doctor", "--project-root", str(root)])
    _assert_safe(rc, payload, product_cli._DOCTOR_SCHEMA, reason="doctor_invalid")


@pytest.mark.parametrize("seam", ["planner", "read_validation"])
@pytest.mark.parametrize("exception", [OSError, RuntimeError, ValueError])
def test_init_dry_run_owner_exceptions_are_abstentions(tmp_path, monkeypatch, seam, exception):
    root = _init_root(tmp_path, monkeypatch)
    if seam == "planner":
        monkeypatch.setattr(product_cli, "plan_dependency_changes",
                            lambda *args, **kwargs: (_ for _ in ()).throw(exception(_EVIL)))
    else:
        monkeypatch.setattr(product_cli, "_read_settings",
                            lambda path: (_ for _ in ()).throw(exception(_EVIL)))
    before = _snapshot_tree(root)
    rc, payload = invoke(_init_argv(root))
    _assert_safe(rc, payload, product_cli._INIT_SCHEMA, reason="init_invalid")
    assert _snapshot_tree(root) == before


@pytest.mark.parametrize("seam", ["state", "prevalidation", "backup"])
@pytest.mark.parametrize("exception", [OSError, RuntimeError, ValueError])
def test_rollback_owner_exceptions_are_contained(tmp_path, monkeypatch, seam, exception):
    root = _init_root(tmp_path, monkeypatch, both=True)
    assert invoke(_init_argv(root, apply=True))[0] == 0
    if seam == "state":
        monkeypatch.setattr(product_cli, "_read_state",
                            lambda root: (_ for _ in ()).throw(exception(_EVIL)))
    elif seam == "prevalidation":
        monkeypatch.setattr(product_cli, "_validate_executable",
                            lambda path: (_ for _ in ()).throw(exception(_EVIL)))
    else:
        monkeypatch.setattr(product_cli, "_validate_backup",
                            lambda *args, **kwargs: (_ for _ in ()).throw(exception(_EVIL)))
    before = _snapshot_tree(root)
    rc, payload = invoke(["rollback", "--project-root", str(root), "--apply"])
    _assert_safe(rc, payload, product_cli._ROLLBACK_SCHEMA, reason="rollback_invalid")
    assert _snapshot_tree(root) == before


@pytest.mark.parametrize("exception", [OSError, RuntimeError, ValueError])
def test_init_apply_mutation_fault_uses_transaction_recovery(tmp_path, monkeypatch, exception):
    root = _init_root(tmp_path, monkeypatch, both=True)
    before = _snapshot_tree(root)
    real_atomic = product_cli._atomic_write
    def fail_adapter(path, raw):
        if path.name == "adapter.json":
            raise exception(_EVIL)
        return real_atomic(path, raw)
    monkeypatch.setattr(product_cli, "_atomic_write", fail_adapter)
    rc, payload = invoke(_init_argv(root, apply=True))
    _assert_safe(rc, payload, product_cli._INIT_SCHEMA)
    assert payload["reason"] in {"write_failed", "recovery_failed"}
    assert _snapshot_tree(root) == before


@pytest.mark.parametrize("exception", [OSError, RuntimeError, ValueError])
def test_doctor_state_read_owner_exception_is_contained(tmp_path, monkeypatch, exception):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(product_cli, "_doctor_installation", lambda: {
        "status": "fail", "reason": "fixture", "module_path": None,
        "installed_version": None, "executable": "python", "scripts": {},
    })
    monkeypatch.setattr(product_cli, "_read_state",
                        lambda root: (_ for _ in ()).throw(exception(_EVIL)))
    rc, payload = invoke(["doctor", "--project-root", str(root)])
    _assert_safe(rc, payload, product_cli._DOCTOR_SCHEMA)


@pytest.mark.parametrize("command", ["init", "rollback"])
def test_response_too_large_is_before_any_project_mutation(tmp_path, monkeypatch, command):
    root = _init_root(tmp_path, monkeypatch, both=(command == "rollback"))
    if command == "rollback":
        assert invoke(_init_argv(root, apply=True))[0] == 0
        argv = ["rollback", "--project-root", str(root), "--apply"]
        schema = product_cli._ROLLBACK_SCHEMA
    else:
        argv = _init_argv(root, apply=True)
        schema = product_cli._INIT_SCHEMA
    before = _snapshot_tree(root)
    mutations = []
    for name in ("_write_backup", "_atomic_write", "_restore_file"):
        monkeypatch.setattr(product_cli, name, lambda *args, _name=name, **kwargs: mutations.append(_name))
    monkeypatch.setattr(product_cli.Path, "unlink",
                        lambda *args, **kwargs: mutations.append("unlink"))
    monkeypatch.setattr(product_cli, "_assert_receipt_fits",
                        lambda *args: (_ for _ in ()).throw(ValueError("response_too_large")))
    rc, payload = invoke(argv)
    _assert_safe(rc, payload, schema, reason="response_too_large")
    assert not mutations
    assert _snapshot_tree(root) == before


def test_real_maximum_reachable_success_receipts_fit_and_record_boundary(tmp_path, monkeypatch):
    root = _init_root(tmp_path, monkeypatch, both=True)
    package = "p" * 63
    (root / "before/requirements.txt").write_text(f"{package}==1.0.0\n")
    (root / "after/requirements.txt").write_text(f"{package}==9999.9999.9999\n")
    rc, init_receipt = invoke(_init_argv(root) + ["--package", package])
    assert rc == 0 and init_receipt["status"] == "selected"
    init_bytes = len(product_cli._receipt_bytes(init_receipt))
    assert init_bytes <= product_cli.MAX_OUTPUT_BYTES
    assert init_receipt["plan"]["section_ids"] == []  # CLI/planner exposes no section option.
    assert invoke(_init_argv(root, apply=True) + ["--package", package])[0] == 0
    rc, rollback_receipt = invoke(["rollback", "--project-root", str(root)])
    assert rc == 0 and rollback_receipt["status"] == "selected"
    rollback_bytes = len(product_cli._receipt_bytes(rollback_receipt))
    assert rollback_bytes <= product_cli.MAX_OUTPUT_BYTES
    assert init_bytes >= rollback_bytes or rollback_bytes > 0


@pytest.mark.parametrize("command", ["init", "doctor", "rollback"])
def test_recognized_command_routing_hides_huge_root_and_arg_lists(tmp_path, monkeypatch, command):
    root = _init_root(tmp_path, monkeypatch)
    if command == "init":
        argv = _init_argv(root)
        argv[argv.index("--project-root") + 1] = "R" * 100_000
    else:
        argv = [command, "--project-root", "R" * 100_000]
    rc, payload = invoke(argv)
    assert rc != 0 and payload["schema"] == getattr(product_cli, "_" + command.upper() + "_SCHEMA")
    assert "R" not in json.dumps(payload)
    rc, payload = invoke([command] + ["--noise"] * 513)
    assert rc != 0 and payload["schema"] == getattr(product_cli, "_" + command.upper() + "_SCHEMA")
    assert payload["reason"] == "arguments_too_large"


def test_non_string_token_and_unknown_huge_command_use_documented_fallback():
    output = io.BytesIO()
    rc = product_cli.main([object()], stdout=output)  # type: ignore[list-item]
    payload = json.loads(output.getvalue())
    assert rc != 0 and payload["schema"] == "universal-docs.plan/v1"
    rc, payload = invoke(["U" * 100_000])
    assert rc != 0 and payload["schema"] == "universal-docs.plan/v1"
    assert "U" not in json.dumps(payload)
