"""Focused contract tests for the offline benchmark runner."""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

import scripts.version_guard_benchmark as benchmark
from scripts.version_guard_benchmark import (
    BenchmarkError,
    aggregate,
    markdown_summary,
    run_benchmark,
    run_case,
    validate_manifest,
)


def _command(source: str) -> list[str]:
    return [sys.executable, "-c", source]


def _case(fixture: str = "fixture") -> dict:
    return {
        "id": "demo-case",
        "ecosystem": "python",
        "package": "demo",
        "target_version": "2.0.0",
        "task": "update the configuration",
        "workspace_fixture": fixture,
        "test_command": _command("raise SystemExit(0)"),
    }


def _manifest(tmp_path: Path, **case_updates) -> tuple[dict, Path]:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "original.txt").write_text("original\n")
    case = _case()
    case.update(case_updates)
    return {"cases": [case]}, fixture.parent


def test_fresh_workspace_and_argv_only_execution(tmp_path):
    manifest, fixture_root = _manifest(
        tmp_path,
        test_command=_command("from pathlib import Path; raise SystemExit(0 if Path('changed.txt').read_text() == 'ok' else 1)"),
    )
    agent = _command("from pathlib import Path; Path('changed.txt').write_text('ok')")
    first = run_case(manifest["cases"][0], "control", fixture_root, tmp_path, agent)
    second = run_case(manifest["cases"][0], "control", fixture_root, tmp_path, agent)
    assert first["test"]["command_status"] == "passed"
    assert second["test"]["command_status"] == "passed"
    assert (fixture_root / "fixture" / "changed.txt").exists() is False


def test_context_hash_binding_and_control_has_no_context(tmp_path):
    manifest, fixture_root = _manifest(tmp_path, context_files={"manual": "context.md"})
    (tmp_path / "context.md").write_bytes(b"exact context\n")
    agent = _command("raise SystemExit(0)")
    control = run_case(manifest["cases"][0], "control", fixture_root, tmp_path, agent)
    manual = run_case(manifest["cases"][0], "manual", fixture_root, tmp_path, agent)
    assert control["context_sha256"] is None and control["context_bytes"] == 0
    assert manual["context_sha256"]
    assert manual["context_bytes"] == len(b"exact context\n")
    assert (tmp_path / "context.md").read_bytes() == b"exact context\n"


def test_oracle_pass_and_fail_are_recorded(tmp_path):
    manifest, fixture_root = _manifest(tmp_path)
    agent = _command("raise SystemExit(0)")
    passed = run_case(manifest["cases"][0], "automatic", fixture_root, tmp_path, agent)
    manifest["cases"][0]["test_command"] = _command("raise SystemExit(3)")
    failed = run_case(manifest["cases"][0], "automatic", fixture_root, tmp_path, agent)
    assert passed["test"]["command_status"] == "passed"
    assert failed["test"]["command_status"] == "failed"
    assert failed["test"]["exit_code"] == 3


def test_timeout_and_missing_executable_do_not_escape(tmp_path):
    manifest, fixture_root = _manifest(tmp_path)
    timeout = run_case(
        manifest["cases"][0],
        "control",
        fixture_root,
        tmp_path,
        _command("import time; time.sleep(1)"),
        timeout_seconds=0.02,
    )
    missing = run_case(manifest["cases"][0], "control", fixture_root, tmp_path, ["definitely-missing-executable"])
    assert timeout["agent"] == {"status": "timeout", "exit_code": None, "error": "agent_timeout"}
    assert missing["agent"]["status"] == "error"
    assert missing["agent"]["error"] == "agent_not_executable"


def test_malformed_cases_and_unsafe_paths_are_rejected():
    with pytest.raises(BenchmarkError):
        validate_manifest({"cases": []})
    with pytest.raises(BenchmarkError):
        validate_manifest({"cases": [{**_case(), "test_command": "python -c unsafe"}]})
    with pytest.raises(BenchmarkError):
        validate_manifest({"cases": [{**_case(), "workspace_fixture": "../outside"}]})


def test_redaction_and_receipt_omit_paths_and_context_body(tmp_path):
    manifest, fixture_root = _manifest(
        tmp_path,
        task="use token=SUPER_SECRET and /Users/operator/private.txt",
        test_command=_command("raise SystemExit(0)"),
        context_files={"manual": "context.md"},
    )
    (tmp_path / "context.md").write_text("do not store this context body")
    report = run_benchmark(
        manifest,
        fixture_root,
        tmp_path,
        _command("raise SystemExit(0)"),
        arms=("control", "manual"),
        provider="local",
        model="test-model",
        generation_id="generation-1",
    )
    encoded = json.dumps(report)
    assert "SUPER_SECRET" not in encoded
    assert "do not store this context body" not in encoded
    assert str(tmp_path) not in encoded
    assert report["results"][1]["context_sha256"]
    assert report["aggregate"]["cases"] == 2


def test_aggregation_is_sorted_and_summary_is_human_readable():
    results = [
        {"id": "b", "arm": "manual", "test": {"command_status": "failed"}, "agent": {"status": "passed"}},
        {"id": "a", "arm": "control", "test": {"command_status": "passed"}, "agent": {"status": "passed"}},
    ]
    summary = aggregate(results)
    assert summary["cases"] == 2
    assert summary["passed"] == 1
    report = {"schema": "universal-docs.benchmark/v1", "aggregate": summary, "results": results}
    assert "# Version Guard benchmark" in markdown_summary(report)
    assert "| a | control | passed |" in markdown_summary(report)


def test_manifest_task_and_case_shape_are_bounded(tmp_path):
    manifest, _ = _manifest(tmp_path, task="x" * (benchmark.MAX_STRING_BYTES + 1))
    with pytest.raises(BenchmarkError, match="task"):
        validate_manifest(manifest)

    malformed = _case()
    malformed["unexpected"] = "x"
    with pytest.raises(BenchmarkError, match="keys"):
        validate_manifest({"cases": [malformed]})


def test_cli_rejects_oversized_manifest_bytes(tmp_path, monkeypatch):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(b"{" + b" " * benchmark.MAX_MANIFEST_BYTES + b"}")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "version_guard_benchmark",
            "--manifest",
            str(manifest_path),
            "--fixture-root",
            str(tmp_path),
            "--output",
            str(tmp_path / "out.json"),
            "--summary-output",
            str(tmp_path / "summary.md"),
            "--agent-command-json",
            json.dumps([sys.executable, "-c", "pass"]),
        ],
    )
    with pytest.raises(BenchmarkError, match="manifest_too_large"):
        benchmark._main()


def test_context_file_is_bounded_before_copy(tmp_path):
    manifest, fixture_root = _manifest(tmp_path, context_files={"manual": "context.md"})
    (tmp_path / "context.md").write_bytes(b"c" * (benchmark.MAX_CONTEXT_BYTES + 1))
    with pytest.raises(BenchmarkError, match="context"):
        run_case(manifest["cases"][0], "manual", fixture_root, tmp_path, _command("pass"))


def test_fixture_tree_rejects_symlinks_and_oversized_files(tmp_path):
    manifest, fixture_root = _manifest(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (fixture_root / "fixture" / "linked.txt").symlink_to(outside)
    with pytest.raises(BenchmarkError, match="symlink"):
        run_case(manifest["cases"][0], "control", fixture_root, tmp_path, _command("pass"))

    large_root = tmp_path / "large"
    large_root.mkdir()
    manifest, fixture_root = _manifest(large_root)
    (fixture_root / "fixture" / "large.bin").write_bytes(
        b"x" * (benchmark.MAX_FIXTURE_FILE_BYTES + 1)
    )
    with pytest.raises(BenchmarkError, match="fixture"):
        run_case(manifest["cases"][0], "control", fixture_root, tmp_path / "large", _command("pass"))


def test_subprocess_output_is_streamed_capped_and_hashed(tmp_path):
    manifest, fixture_root = _manifest(
        tmp_path,
        test_command=_command("import sys; sys.stdout.buffer.write(b'o' * 65553)"),
    )
    payload = "o" * (benchmark.MAX_SUBPROCESS_OUTPUT_BYTES + 17)
    result = run_case(
        manifest["cases"][0],
        "control",
        fixture_root,
        tmp_path,
        _command("pass"),
        timeout_seconds=2,
    )
    expected = payload.encode()[: benchmark.MAX_SUBPROCESS_OUTPUT_BYTES]
    assert result["test"]["output_truncated"] is True
    assert result["test"]["output_sha256"] == hashlib.sha256(expected).hexdigest()
    assert result["test"]["output_bytes"] == benchmark.MAX_SUBPROCESS_OUTPUT_BYTES


def test_timed_out_process_is_cleaned_up(tmp_path):
    manifest, fixture_root = _manifest(tmp_path)
    pid_file = tmp_path / "child.pid"
    result = run_case(
        manifest["cases"][0],
        "control",
        fixture_root,
        tmp_path,
        _command(
            "import pathlib, subprocess, sys, time; "
            "p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']); "
            f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(10)"
        ),
        timeout_seconds=0.05,
    )
    assert result["agent"]["status"] == "timeout"
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
