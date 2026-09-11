from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

import pytest

from universal_docs_mcp import product_cli


def _call(root: Path, *args: str):
    out = io.BytesIO()
    rc = product_cli.main([*args, "--project-root", str(root)], stdout=out)
    assert out.getvalue().count(b"\n") == 1
    return rc, json.loads(out.getvalue())


def _project(
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
    return root


def _apply(root: Path):
    return _call(
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


def test_doctor_rejects_wrong_hook_timeout_and_extra_argv(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    assert (
        _call(
            root,
            "init",
            "--harness",
            "claude-code",
            "--before",
            "before/requirements.txt",
            "--after",
            "after/requirements.txt",
            "--apply",
        )[0]
        == 0
    )
    adapter = root / ".universal-docs/adapter.json"
    value = json.loads(adapter.read_text())
    value["timeout_ms"] = 29_999
    adapter.write_text(json.dumps(value))
    assert _call(root, "doctor")[0] == 1


def test_doctor_rejects_explicit_relative_cache(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    monkeypatch.setenv("UNIVERSAL_DOCS_CACHE_DIR", "relative-cache")
    info, status = product_cli._doctor_cache(root)
    assert status == "fail" and info["scope"] == "explicit"


def test_doctor_probe_bounds_descendant_stdout(tmp_path):
    hook = tmp_path / "hook"
    adapter = tmp_path / "adapter.json"
    hook.write_text(
        f"#!{sys.executable}\nimport os,sys,time\nos.fork()\nos.write(1, b'{{}}\\n')\ntime.sleep(10)\n"
    )
    hook.chmod(0o700)
    adapter.write_text(
        json.dumps(
            {"preflight_command": ["/bin/true"], "request": {}, "timeout_ms": 100}
        )
    )
    started = time.monotonic()
    meta, reason = product_cli._doctor_probe(hook, adapter, {})
    elapsed = time.monotonic() - started
    assert elapsed < 1.0
    assert reason in {
        "probe_timeout",
        "probe_malformed",
        "preflight_nonzero",
        "probe_output_too_large",
    }
    assert "stderr" not in meta and "context" not in meta


def test_preexisting_adapter_repeat_rollback_is_idempotent(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch, old_adapter=b'{"old":1}\n')
    assert _apply(root)[0] == 0
    assert _call(root, "rollback", "--apply")[0] == 0
    rc, receipt = _call(root, "rollback", "--apply")
    assert rc == 0 and receipt["changed"] is False


def test_tampered_tombstone_fails_closed(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    assert _apply(root)[0] == 0
    assert _call(root, "rollback", "--apply")[0] == 0
    tombstone = root / ".universal-docs/rollback-tombstone.json"
    tombstone.write_text('{"schema":"bad"}')
    rc, receipt = _call(root, "rollback", "--apply")
    assert rc == 1 and receipt["reason"] == "rollback_tombstone_invalid"


def test_doctor_receipt_never_contains_personal_path_or_body(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    assert _apply(root)[0] == 0
    monkeypatch.setattr(
        product_cli,
        "_doctor_installation",
        lambda: {
            "status": "fail",
            "reason": "identity_mismatch",
            "module_path": str(Path.home() / "secret"),
            "installed_version": None,
            "executable": str(Path.home() / "bin/python"),
        },
    )
    rc, receipt = _call(root, "doctor")
    assert rc == 1
    encoded = json.dumps(receipt)
    assert str(Path.home()) not in encoded


@pytest.mark.parametrize(
    "argv",
    [
        ["doctor"],
        ["rollback"],
        ["doctor", "--project-root", "relative"],
        ["rollback", "--project-root", "relative"],
    ],
)
def test_invalid_doctor_rollback_are_bounded_receipts(argv):
    out = io.BytesIO()
    assert product_cli.main(argv, stdout=out) == 1
    assert out.getvalue().count(b"\n") == 1
    payload = json.loads(out.getvalue())
    assert payload["schema"] in {
        "universal-docs.doctor/v1",
        "universal-docs.rollback/v1",
    }
    assert payload["status"] == "fail"
