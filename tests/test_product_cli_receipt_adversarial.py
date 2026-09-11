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


def test_invalid_apply_placement_is_not_a_success():
    rc, receipt = invoke(["doctor", "--apply"])
    assert rc != 0
    assert receipt["schema"] == "universal-docs.doctor/v1"
    assert receipt["status"] == "fail"


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
