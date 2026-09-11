from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from universal_docs_mcp import product_cli


def _receipt(root: Path, command: str, *args: str) -> tuple[int, dict]:
    import io

    out = io.BytesIO()
    rc = product_cli.main([command, *args, "--project-root", str(root)], stdout=out)
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
        lambda x: x["adapter"].update(
            backup=".universal-docs/backups/adapter-" + "0" * 64 + ".json", extra=True
        ),
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
    module = SimpleNamespace(
        __file__=str(module_path), __version__=product_cli.__version__
    )
    dist = SimpleNamespace(
        version=product_cli.__version__,
        files=[Path("universal_docs_mcp/__init__.py")],
        locate_file=lambda item: module_path,
    )
    monkeypatch.delenv("UNIVERSAL_DOCS_INIT_UNIVERSAL_DOCS_COMMAND_HOOK", raising=False)
    monkeypatch.delenv("UNIVERSAL_DOCS_INIT_UNIVERSAL_DOCS_PREFLIGHT", raising=False)
    monkeypatch.setattr(product_cli.sys, "executable", str(bin_dir / "python"))
    monkeypatch.setattr(product_cli.importlib, "import_module", lambda name: module)
    monkeypatch.setattr(
        product_cli.importlib.metadata, "distribution", lambda name: dist
    )
    assert product_cli._doctor_installation()["status"] == "pass"
    elsewhere = tmp_path / "elsewhere.py"
    elsewhere.write_text("__version__ = 'test'\n")
    module.__file__ = str(elsewhere)
    assert product_cli._doctor_installation()["reason"] == "identity_mismatch"


def test_identity_override_is_fixture_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "UNIVERSAL_DOCS_INIT_UNIVERSAL_DOCS_COMMAND_HOOK", str(tmp_path / "hook")
    )
    result = product_cli._doctor_installation()
    assert result["status"] == "fail"
    assert result["reason"] == "fixture_override"
    assert result["owned"] is False


def test_executable_validation_rejects_link_fifo_directory_and_non_executable(
    tmp_path: Path,
) -> None:
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


def _init_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, old_adapter: bytes | None = None
) -> Path:
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
    if old_adapter is not None:
        (root / ".universal-docs").mkdir()
        (root / ".universal-docs/adapter.json").write_bytes(old_adapter)
    rc, _ = _receipt_init(root)
    assert rc == 0
    return root


def _receipt_init(root: Path) -> tuple[int, dict]:
    import io

    out = io.BytesIO()
    rc = product_cli.main(
        [
            "init",
            "--harness",
            "claude-code",
            "--project-root",
            str(root),
            "--before",
            "before/requirements.txt",
            "--after",
            "after/requirements.txt",
            "--apply",
        ],
        stdout=out,
    )
    assert out.getvalue().count(b"\n") == 1
    return rc, json.loads(out.getvalue())


def _bytes_snapshot(root: Path) -> dict[str, tuple[str, bytes | None]]:
    snapshot: dict[str, tuple[str, bytes | None]] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        mode = path.lstat().st_mode
        if stat.S_ISREG(mode):
            snapshot[relative] = ("file", path.read_bytes())
        elif stat.S_ISLNK(mode):
            snapshot[relative] = ("symlink", os.readlink(path).encode())
        elif stat.S_ISFIFO(mode):
            snapshot[relative] = ("fifo", None)
        elif stat.S_ISDIR(mode):
            snapshot[relative] = ("dir", None)
    return snapshot


@pytest.mark.parametrize(
    "target", ["adapter", "settings"], ids=["adapter-json", "settings-json"]
)
@pytest.mark.parametrize(
    "case,raw",
    [
        ("missing", None),
        ("malformed-utf8", b"\xff"),
        ("malformed-json", b"{"),
        ("duplicate-keys", b'{"x":1,"x":2}'),
        ("over-64kib", b"x" * (64 * 1024 + 1)),
    ],
    ids=lambda item: item[0] if isinstance(item, tuple) else str(item),
)
def test_doctor_rejects_adapter_and_settings_file_permutations_without_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    case: str,
    raw: bytes | None,
) -> None:
    root = tmp_path / "project"
    (root / (".universal-docs" if target == "adapter" else ".claude")).mkdir(
        parents=True
    )
    path = root / (
        ".universal-docs/adapter.json"
        if target == "adapter"
        else ".claude/settings.json"
    )
    if raw is not None:
        path.write_bytes(raw)
    before = _bytes_snapshot(root)
    rc, receipt = _receipt(root, "doctor")
    assert (
        rc == 1
        and receipt["schema"] == "universal-docs.doctor/v1"
        and receipt["status"] == "fail"
    )
    assert _bytes_snapshot(root) == before


@pytest.mark.parametrize(
    "target", ["adapter", "settings"], ids=["adapter-json", "settings-json"]
)
@pytest.mark.parametrize("kind", ["symlink", "fifo"], ids=["final-symlink", "fifo"])
def test_doctor_rejects_final_special_file_without_touching_target(
    tmp_path: Path, target: str, kind: str
) -> None:
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"do-not-touch")
    parent = root / (".universal-docs" if target == "adapter" else ".claude")
    parent.mkdir(parents=True)
    target_path = parent / ("adapter.json" if target == "adapter" else "settings.json")
    if kind == "symlink":
        target_path.symlink_to(outside / "sentinel")
    else:
        os.mkfifo(target_path)
    before = _bytes_snapshot(root), _bytes_snapshot(outside)
    rc, receipt = _receipt(root, "doctor")
    assert rc == 1 and receipt["status"] == "fail"
    assert _bytes_snapshot(root) == before[0] and _bytes_snapshot(outside) == before[1]


@pytest.mark.parametrize(
    "parent",
    [".universal-docs", ".universal-docs/backups", ".claude"],
    ids=["docs", "backups", "claude"],
)
@pytest.mark.parametrize("kind", ["symlink", "fifo"], ids=["symlink", "fifo"])
def test_all_control_parents_fail_closed_for_doctor_rollback_and_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parent: str, kind: str
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "before").mkdir()
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
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"fixed")
    control = root / parent
    control.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        control.symlink_to(outside, target_is_directory=True)
    else:
        os.mkfifo(control)
    before = _bytes_snapshot(root), _bytes_snapshot(outside)
    for command in ("doctor", "rollback"):
        rc, receipt = _receipt(root, command)
        assert (
            rc == 1
            and receipt["status"] == "fail"
            and receipt["reason"] == "output_parent_invalid"
        )
    out = __import__("io").BytesIO()
    rc = product_cli.main(
        [
            "init",
            "--harness",
            "claude-code",
            "--project-root",
            str(root),
            "--before",
            "missing",
            "--after",
            "missing",
            "--apply",
        ],
        stdout=out,
    )
    assert rc == 1
    assert _bytes_snapshot(root) == before[0] and _bytes_snapshot(outside) == before[1]


@pytest.mark.parametrize(
    "case",
    [
        "missing-hook",
        "duplicate-exact-hook",
        "near-match-command",
        "wrong-type",
        "wrong-timeout",
        "malformed-shell-quoting",
        "wrong-config-path",
        "extra-argv",
    ],
    ids=lambda value: value,
)
def test_hook_matrix_doctor_and_rollback_fail_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    root = _init_project(tmp_path, monkeypatch)
    settings_path = root / ".claude/settings.json"
    settings = json.loads(settings_path.read_text())
    groups = settings["hooks"]["UserPromptSubmit"]
    expected = groups[0]["hooks"][0]
    if case == "missing-hook":
        groups[0]["hooks"] = []
    else:
        item = dict(expected)
        if case == "duplicate-exact-hook":
            groups[0]["hooks"].append(dict(expected))
        elif case == "near-match-command":
            item["command"] += " --near-match"
            groups[0]["hooks"] = [item]
        elif case == "wrong-type":
            item["type"] = "prompt"
            groups[0]["hooks"] = [item]
        elif case == "wrong-timeout":
            item["timeout"] = 31
            groups[0]["hooks"] = [item]
        elif case == "malformed-shell-quoting":
            item["command"] = expected["command"] + " '"
            groups[0]["hooks"] = [item]
        elif case == "wrong-config-path":
            item["command"] = item["command"].replace("adapter.json", "other.json")
            groups[0]["hooks"] = [item]
        else:
            item["command"] += " extra"
            groups[0]["hooks"] = [item]
    settings_path.write_bytes(json.dumps(settings).encode())
    before = _bytes_snapshot(root)
    for command in ("doctor", "rollback"):
        rc, receipt = _receipt(root, command)
        assert rc == 1 and receipt["status"] == "fail"
        assert _bytes_snapshot(root) == before


@pytest.mark.parametrize(
    "case",
    [
        "top-level-extra",
        "top-level-missing",
        "paths-extra",
        "paths-missing",
        "adapter-extra",
        "adapter-missing",
        "hook-extra",
        "hook-missing",
        "settings-extra",
        "settings-missing",
        "wrong-schema",
        "malformed-hash",
        "uppercase-hash",
        "short-hash",
        "true-nul",
        "absolute-path",
        "dotdot-path",
        "state-final-symlink",
        "state-fifo",
        "state-over-64kib",
        "generated-adapter-hash-mismatch",
        "hook-command-hash-mismatch",
        "argv-hash-mismatch",
    ],
    ids=lambda value: value,
)
def test_install_state_permutations_fail_closed_on_public_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    root = _init_project(tmp_path, monkeypatch)
    state_path = root / ".universal-docs/install-state.json"
    state = json.loads(state_path.read_text())
    if case == "top-level-extra":
        state["extra"] = True
    elif case == "top-level-missing":
        del state["hook"]
    elif case == "paths-extra":
        state["paths"]["extra"] = "x"
    elif case == "paths-missing":
        del state["paths"]["state"]
    elif case == "adapter-extra":
        state["adapter"]["extra"] = True
    elif case == "adapter-missing":
        del state["adapter"]["backup"]
    elif case == "hook-extra":
        state["hook"]["extra"] = True
    elif case == "hook-missing":
        del state["hook"]["argv_sha256"]
    elif case == "settings-extra":
        state["settings"]["extra"] = True
    elif case == "settings-missing":
        del state["settings"]["preimage_sha256"]
    elif case == "wrong-schema":
        state["schema"] = "wrong"
    elif case in {"malformed-hash", "uppercase-hash", "short-hash"}:
        state["hook"]["command_sha256"] = {
            "malformed-hash": "!" * 64,
            "uppercase-hash": "A" * 64,
            "short-hash": "a",
        }[case]
    elif case == "true-nul":
        state["paths"]["adapter"] = ".universal-docs/adapter\x00.json"
    elif case == "absolute-path":
        state["paths"]["adapter"] = "/tmp/adapter.json"
    elif case == "dotdot-path":
        state["paths"]["adapter"] = ".universal-docs/../adapter.json"
    elif case == "generated-adapter-hash-mismatch":
        state["adapter"]["sha256"] = "0" * 64
    elif case == "hook-command-hash-mismatch":
        state["hook"]["command_sha256"] = "0" * 64
    elif case == "argv-hash-mismatch":
        state["hook"]["argv_sha256"] = "0" * 64
    else:
        state_path.unlink()
        if case == "state-final-symlink":
            state_path.symlink_to(tmp_path / "outside-state")
        elif case == "state-fifo":
            os.mkfifo(state_path)
        else:
            state_path.write_bytes(b"x" * (64 * 1024 + 1))
        state = None
    if state is not None:
        state_path.write_bytes(json.dumps(state).encode())
    before = _bytes_snapshot(root)
    for command in ("doctor", "rollback"):
        rc, receipt = _receipt(root, command)
        assert rc == 1 and receipt["status"] == "fail"
        assert _bytes_snapshot(root) == before


@pytest.mark.parametrize(
    "case",
    ["missing", "tampered", "final-symlink", "fifo", "backup-parent-symlink"],
    ids=lambda value: f"pinned-backup-{value}",
)
def test_pinned_adapter_backup_permutations_fail_without_external_touch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    root = _init_project(tmp_path, monkeypatch, old_adapter=b'{"pinned":true}\n')
    backup = next((root / ".universal-docs/backups").glob("adapter-*.json"))
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"immutable")
    if case == "missing":
        backup.unlink()
    elif case == "tampered":
        backup.write_bytes(b"tampered")
    elif case == "final-symlink":
        backup.unlink()
        backup.symlink_to(sentinel)
    elif case == "fifo":
        backup.unlink()
        os.mkfifo(backup)
    else:
        backups = root / ".universal-docs/backups"
        moved = root / ".universal-docs/backups.saved"
        backups.rename(moved)
        backups.symlink_to(outside, target_is_directory=True)
    before = _bytes_snapshot(root), _bytes_snapshot(outside)
    rc, receipt = _receipt(root, "rollback", "--apply")
    assert rc == 1 and receipt["status"] == "fail"
    assert _bytes_snapshot(root) == before[0] and _bytes_snapshot(outside) == before[1]


def test_unrelated_historical_backups_do_not_change_pinned_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _init_project(tmp_path, monkeypatch, old_adapter=b'{"pinned":true}\n')
    backups = root / ".universal-docs/backups"
    (backups / "adapter-deadbeef.json").write_bytes(b"wrong history")
    (backups / f"adapter-{product_cli._sha256(b'other')}.json").write_bytes(b"other")
    rc, receipt = _receipt(root, "rollback", "--apply")
    assert rc == 0 and receipt["status"] == "selected"
    assert (root / ".universal-docs/adapter.json").read_bytes() == b'{"pinned":true}\n'


@pytest.mark.parametrize(
    "case",
    [
        "extra",
        "missing",
        "malformed-hash",
        "tamper-after-rollback",
        "inconsistent-live-adapter",
        "inconsistent-live-hook",
    ],
    ids=lambda value: f"tombstone-{value}",
)
def test_tombstone_nested_schema_and_consistency_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    root = _init_project(tmp_path, monkeypatch)
    assert _receipt(root, "rollback", "--apply")[0] == 0
    tombstone = root / ".universal-docs/rollback-tombstone.json"
    value = json.loads(tombstone.read_text())
    if case == "extra":
        value["extra"] = True
    elif case == "missing":
        del value["preimage_sha256"]
    elif case == "malformed-hash":
        value["adapter_sha256"] = "BAD"
    elif case == "tamper-after-rollback":
        tombstone.write_bytes(b"tampered")
    elif case == "inconsistent-live-hook":
        (root / ".claude/settings.json").write_text(
            '{"hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command",'
            '"command":"universal-docs-command-hook --harness claude --config x",'
            '"timeout":30}]}]}}'
        )
    else:
        (root / ".universal-docs/adapter.json").write_bytes(b"recreated")
    if case in {"extra", "missing", "malformed-hash"}:
        tombstone.write_bytes(json.dumps(value).encode())
    before = _bytes_snapshot(root)
    for command in ("doctor", "rollback"):
        if command == "rollback":
            rc, receipt = _receipt(root, command, "--apply")
        else:
            rc, receipt = _receipt(root, command)
        assert rc == 1 and receipt["status"] == "fail"
        assert _bytes_snapshot(root) == before
