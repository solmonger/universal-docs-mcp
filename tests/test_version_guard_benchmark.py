"""Focused contract tests for the offline benchmark runner."""

import json
import sys
from pathlib import Path

import pytest

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
