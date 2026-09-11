"""Focused checks for the Slice 02 corpus contract."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
CHECKER = ROOT / "scripts" / "check_version_guard_cases.py"
RECEIPT = ROOT / "benchmarks" / "version_guard" / "corpus_receipt.json"


def test_corpus_checker_proves_all_initial_failures_are_version_errors():
    result = subprocess.run(
        [sys.executable, str(CHECKER)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    assert receipt["case_count"] >= 10
    assert len(receipt["case_ids"]) == receipt["case_count"]
    observations = [
        line.rsplit(": ", 1)[1]
        for line in result.stdout.splitlines()
        if line.startswith(tuple(f"{case_id}:" for case_id in receipt["case_ids"]))
    ]
    expected = [case["initial_failure"]["failure_class"] for case in receipt["cases"]]
    assert observations == expected
    assert all(case["evidence_type"] == "synthetic_stub" for case in receipt["cases"])
    assert all(
        case["initial_failure"]["setup_failure"] is False
        for case in receipt["cases"]
    )


def test_slice02_has_a_real_no_lift_control_and_version_cases():
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    classes = {
        case["initial_failure"]["failure_class"] for case in receipt["cases"]
    }
    assert classes == {"wrong_version_api", "general_coding_error"}
    assert sum(
        case["initial_failure"]["failure_class"] == "general_coding_error"
        for case in receipt["cases"]
    ) >= 1
    assert sum(
        case["initial_failure"]["failure_class"] == "wrong_version_api"
        for case in receipt["cases"]
    ) >= 10
    control = ROOT / "benchmarks" / "version_guard" / "fixtures" / "stable-api-no-lift-control"
    assert "LegacyVersion" not in (control / "app.py").read_text(encoding="utf-8")


def test_checker_contract_requires_evidence_fields_and_has_safety_caps():
    checker = (ROOT / "scripts" / "check_version_guard_cases.py").read_text(
        encoding="utf-8"
    )
    assert '"evidence_type", "evidence_note"' in checker.split("REQUIRED", 1)[1].split(
        "ALLOWED", 1
    )[0]
    for name in (
        "MAX_MANIFEST_BYTES",
        "MAX_CASES",
        "MAX_FIXTURE_FILES",
        "MAX_FILE_BYTES",
        "MAX_CORPUS_BYTES",
        "MAX_ORACLE_OUTPUT_BYTES",
    ):
        assert name in checker
    assert "resolve()" in checker
    assert "is_symlink()" in checker


def test_corpus_cases_use_shared_argv_contract_and_safe_paths():
    manifests = sorted((ROOT / "benchmarks" / "version_guard" / "cases").glob("*.json"))
    assert len(manifests) >= 10
    for path in manifests:
        case = json.loads(path.read_text(encoding="utf-8"))
        assert set(case) == {
            "id", "ecosystem", "package", "target_version", "task",
            "workspace_fixture", "test_command", "context_profile",
            "expected_failure_class", "evidence_type", "evidence_note",
        }
        assert case["test_command"] == ["python3", "oracle.py"]
        fixture = (ROOT / case["workspace_fixture"]).resolve()
        assert fixture.is_relative_to(ROOT.resolve())
        assert (fixture / "oracle.py").is_file()
        assert case["evidence_type"] == "synthetic_stub"
