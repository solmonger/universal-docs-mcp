"""Focused checks for the blind Slice 02 corpus contract."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
CHECKER = ROOT / "scripts" / "check_version_guard_cases.py"
CORPUS = ROOT / "benchmarks" / "version_guard"
RECEIPT = CORPUS / "corpus_receipt.json"


def run_checker() -> tuple[str, dict]:
    result = subprocess.run([sys.executable, str(CHECKER)], cwd=ROOT, capture_output=True, text=True, check=True)
    return result.stdout, json.loads(RECEIPT.read_text(encoding="utf-8"))


def test_external_oracles_are_blind_and_report_declared_classes():
    output, receipt = run_checker()
    manifests = sorted((CORPUS / "cases").glob("*.json"))
    assert receipt["case_count"] == len(manifests) >= 11
    assert receipt["class_counts"]["wrong_version_api"] >= 10
    assert receipt["class_counts"]["general_coding_error"] >= 1
    assert all(case["evidence_type"] == "synthetic_stub" for case in receipt["cases"])
    assert all(case["initial_failure"]["setup_failure"] is False for case in receipt["cases"])
    assert all(f"{case['id']}: {case['initial_failure']['failure_class']}" in output for case in receipt["cases"])


def test_visible_fixtures_have_no_oracle_or_api_stub_files_or_tokens():
    checker = __import__("scripts.check_version_guard_cases", fromlist=["check_case"])
    for manifest_path in sorted((CORPUS / "cases").glob("*.json")):
        case = json.loads(manifest_path.read_text(encoding="utf-8"))
        fixture = ROOT / case["workspace_fixture"]
        assert not list(fixture.rglob("oracle.py"))
        assert not list(fixture.rglob("versioned_api.py"))
        oracle = ROOT / case["oracle_file"]
        token = checker.expected_api_token(oracle, case["id"]).encode()
        visible = [
            case["task"].encode(),
            *(
                p.read_bytes()
                for p in fixture.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
            ),
        ]
        assert all(token not in data for data in visible)


def test_corpus_digest_is_deterministic():
    _, first = run_checker()
    _, second = run_checker()
    assert first["corpus_sha256"] == second["corpus_sha256"]
    assert first["case_ids"] == second["case_ids"]


def test_real_target_version_api_mapping_is_exact():
    checker = __import__("scripts.check_version_guard_cases", fromlist=["expected_api_token"])
    expected = {
        "attrs-21-to-23-slots": "define",
        "click-7-to-8-parameter": "option",
        "httpx-0-to-1-client": "Client",
        "packaging-22-to-24-version": "Version",
        "pydantic-v1-to-v2-model-dump": "model_dump",
        "pydantic-v1-to-v2-validator": "field_validator",
        "pytest-6-to-8-raises": "raises",
        "rich-12-to-13-console": "Console",
        "sqlalchemy-14-to-2-execute": "execute",
        "sqlalchemy-14-to-2-select": "select",
        "urllib3-1-to-2-timeout": "Timeout",
    }
    for case_id, token in expected.items():
        manifest = json.loads((CORPUS / "cases" / f"{case_id}.json").read_text(encoding="utf-8"))
        assert checker.expected_api_token(ROOT / manifest["oracle_file"], case_id) == token
        checker.validate_expected_api(token, case_id, "wrong_version_api")


def test_version_sensitive_placeholder_tokens_are_rejected():
    checker = __import__("scripts.check_version_guard_cases", fromlist=["validate_expected_api"])
    for token in ("attr_new", "api_replacement"):
        with pytest.raises(SystemExit, match="placeholder"):
            checker.validate_expected_api(token, "synthetic-case", "wrong_version_api")


def test_deliberately_leaked_expected_token_is_rejected(tmp_path, monkeypatch):
    checker = __import__("scripts.check_version_guard_cases", fromlist=["check_case"])
    source = (CORPUS / "cases" / "attrs-21-to-23-slots.json").read_text(encoding="utf-8")
    case = json.loads(source)
    oracle = ROOT / case["oracle_file"]
    token = checker.expected_api_token(oracle, case["id"])
    case["task"] += f" {token}"
    path = tmp_path / "leaked.json"
    path.write_text(json.dumps(case), encoding="utf-8")
    with pytest.raises(SystemExit, match="leaks"):
        checker.check_case(path)


def test_unsafe_external_oracle_path_symlink_and_oversize_rejected(tmp_path):
    checker = __import__("scripts.check_version_guard_cases", fromlist=["check_case"])
    case = json.loads((CORPUS / "cases" / "attrs-21-to-23-slots.json").read_text(encoding="utf-8"))
    case["oracle_file"] = "benchmarks/version_guard/oracles/../cases/attrs-21-to-23-slots.json"
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(case), encoding="utf-8")
    with pytest.raises(SystemExit, match="parent traversal"):
        checker.check_case(path)
    link = CORPUS / "oracles" / "unsafe-link.py"
    case["oracle_file"] = "benchmarks/version_guard/oracles/unsafe-link.py"
    link.symlink_to(CORPUS / "oracles" / "pydantic-v1-to-v2-model-dump.py")
    linked_manifest = tmp_path / "linked.json"
    linked_manifest.write_text(json.dumps(case), encoding="utf-8")
    try:
        with pytest.raises(SystemExit, match="symlink"):
            checker.check_case(linked_manifest)
    finally:
        link.unlink()
    oversized = tmp_path / "huge.py"
    oversized.write_bytes(b"#" * (checker.MAX_ORACLE_BYTES + 1))
    with pytest.raises(SystemExit, match="exceeds"):
        checker.expected_api_token(oversized, "oversized")
