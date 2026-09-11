#!/usr/bin/env python3
"""Validate and receipt the offline Version Guard fixture corpus."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "benchmarks" / "version_guard" / "cases"
RECEIPT = ROOT / "benchmarks" / "version_guard" / "corpus_receipt.json"
REQUIRED = {
    "id", "ecosystem", "package", "target_version", "task",
    "workspace_fixture", "test_command", "context_profile",
    "expected_failure_class",
}
ALLOWED = REQUIRED | {"evidence_type", "evidence_note"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"ERROR: {message}")


def digest(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for path in paths:
        rel = path.relative_to(ROOT).as_posix().encode()
        data = path.read_bytes()
        h.update(len(rel).to_bytes(4, "big"))
        h.update(rel)
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


def main() -> int:
    if not CASES.is_dir():
        fail("missing case directory")
    manifests = sorted(CASES.glob("*.json"))
    if len(manifests) < 10:
        fail(f"only {len(manifests)} cases; need at least 10")
    cases = []
    ids: set[str] = set()
    tracked: list[Path] = []
    for manifest_path in manifests:
        tracked.append(manifest_path)
        try:
            case = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fail(f"{manifest_path.name}: invalid JSON: {exc}")
        if set(case) - ALLOWED or not REQUIRED <= set(case):
            fail(f"{manifest_path.name}: manifest keys must be the shared contract plus evidence fields")
        case_id = case["id"]
        if not isinstance(case_id, str) or not ID_RE.fullmatch(case_id) or case_id in ids:
            fail(f"{manifest_path.name}: invalid or duplicate id")
        ids.add(case_id)
        if case["ecosystem"] != "python" or not all(isinstance(case[k], str) for k in REQUIRED - {"test_command"}):
            fail(f"{case_id}: invalid scalar contract fields")
        if case["evidence_type"] != "synthetic_stub":
            fail(f"{case_id}: only explicitly labelled synthetic_stub fixtures are permitted here")
        if not case["evidence_note"].startswith("Synthetic local API stub"):
            fail(f"{case_id}: synthetic evidence note is missing")
        fixture = ROOT / case["workspace_fixture"]
        try:
            fixture.relative_to(ROOT)
        except ValueError:
            fail(f"{case_id}: fixture escapes repository")
        if not fixture.is_dir():
            fail(f"{case_id}: fixture directory missing")
        command = case["test_command"]
        if (not isinstance(command, list) or not command or
                not all(isinstance(arg, str) and arg and not arg.startswith("/") for arg in command)):
            fail(f"{case_id}: test_command must be a relative argv list")
        if command[0] not in {"python", "python3"}:
            fail(f"{case_id}: oracle must be invoked by Python")
        for path in sorted(fixture.rglob("*")):
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.is_file():
                rel = path.relative_to(ROOT)
                if any(part in {".", ".."} for part in rel.parts) or path.stat().st_size > 64 * 1024:
                    fail(f"{case_id}: unsafe or oversized fixture file {rel}")
                tracked.append(path)
        if "oracle.py" not in {p.name for p in fixture.iterdir()}:
            fail(f"{case_id}: missing oracle.py")
        print(f"{case_id}: ", end="", flush=True)
        result = subprocess.run(command, cwd=fixture, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            fail(f"{case_id}: oracle unexpectedly passed before agent edit")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        try:
            observed = json.loads(lines[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            fail(f"{case_id}: oracle output is not machine-readable: {exc}; stderr={result.stderr[-200:]}")
        if observed.get("case_id") != case_id:
            fail(f"{case_id}: oracle case id mismatch")
        if observed.get("failure_class") != case["expected_failure_class"]:
            fail(f"{case_id}: expected {case['expected_failure_class']}, got {observed.get('failure_class')}")
        if observed.get("setup_failure") is not False or observed.get("status") != "initial_failure":
            fail(f"{case_id}: oracle did not prove a bounded initial wrong-version failure")
        print(observed["failure_class"])
        cases.append({"id": case_id, "evidence_type": case["evidence_type"], "initial_failure": observed})
    tracked = sorted(set(tracked), key=lambda p: p.relative_to(ROOT).as_posix())
    receipt = {
        "schema": "universal-docs.corpus-receipt/v1",
        "case_count": len(cases),
        "case_ids": [case["id"] for case in cases],
        "cases": cases,
        "corpus_sha256": digest(tracked),
    }
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"CORPUS_RECEIPT={RECEIPT.relative_to(ROOT)}")
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
