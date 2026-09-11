#!/usr/bin/env python3
"""Validate and receipt the offline Version Guard fixture corpus."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "benchmarks" / "version_guard" / "cases"
RECEIPT = ROOT / "benchmarks" / "version_guard" / "corpus_receipt.json"
REQUIRED = {
    "id", "ecosystem", "package", "target_version", "task",
    "workspace_fixture", "test_command", "context_profile",
    "expected_failure_class", "evidence_type", "evidence_note",
}
ALLOWED = REQUIRED
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
FAILURE_CLASSES = {"wrong_version_api", "general_coding_error"}
MAX_CASES = 64
MAX_MANIFEST_BYTES = 16 * 1024
MAX_SCALAR_BYTES = 4 * 1024
MAX_ARG_COUNT = 8
MAX_ARG_BYTES = 256
MAX_FIXTURE_FILES = 32
MAX_FILE_BYTES = 64 * 1024
MAX_CORPUS_BYTES = 512 * 1024
MAX_ORACLE_OUTPUT_BYTES = 64 * 1024
ORACLE_TIMEOUT_SECONDS = 5


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


def _kill_process_group(process: subprocess.Popen[Any]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
    process.kill()


def run_bounded(command: list[str], cwd: Path) -> tuple[int, str, str, bool, bool]:
    """Run an oracle with bounded disk capture and process-group cleanup."""
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        kwargs = {"stdout": stdout_file, "stderr": stderr_file, "cwd": cwd}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
        timed_out = False
        try:
            process.wait(timeout=ORACLE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_group(process)
            process.wait()
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read(MAX_ORACLE_OUTPUT_BYTES + 1)
        stderr = stderr_file.read(MAX_ORACLE_OUTPUT_BYTES + 1)
        overflow = len(stdout) > MAX_ORACLE_OUTPUT_BYTES or len(stderr) > MAX_ORACLE_OUTPUT_BYTES
        return (
            process.returncode,
            stdout[:MAX_ORACLE_OUTPUT_BYTES].decode("utf-8", "replace"),
            stderr[:MAX_ORACLE_OUTPUT_BYTES].decode("utf-8", "replace"),
            overflow,
            timed_out,
        )


def safe_path(raw: str, label: str) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        fail(f"{label}: absolute path or parent traversal is forbidden")
    resolved = (ROOT / candidate).resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError:
        fail(f"{label}: path escapes repository")
    current = ROOT
    for part in candidate.parts:
        current /= part
        if current.is_symlink():
            fail(f"{label}: symlink path component is forbidden")
    return resolved


def main() -> int:
    if not CASES.is_dir() or CASES.is_symlink():
        fail("missing or unsafe case directory")
    manifests = sorted(CASES.glob("*.json"))
    if len(manifests) < 10 or len(manifests) > MAX_CASES:
        fail(f"case count {len(manifests)} outside 10..{MAX_CASES}")
    cases = []
    ids: set[str] = set()
    tracked: list[Path] = []
    corpus_bytes = 0
    class_counts: dict[str, int] = {}
    for manifest_path in manifests:
        manifest_bytes = manifest_path.stat().st_size
        if manifest_bytes > MAX_MANIFEST_BYTES:
            fail(f"{manifest_path.name}: manifest exceeds size cap")
        corpus_bytes += manifest_bytes
        tracked.append(manifest_path)
        try:
            case = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fail(f"{manifest_path.name}: invalid JSON: {exc}")
        if not isinstance(case, dict) or set(case) != REQUIRED:
            fail(f"{manifest_path.name}: manifest keys must equal the required contract")
        case_id = case["id"]
        if not isinstance(case_id, str) or not ID_RE.fullmatch(case_id) or case_id in ids:
            fail(f"{manifest_path.name}: invalid or duplicate id")
        ids.add(case_id)
        scalar_keys = REQUIRED - {"test_command"}
        if case["ecosystem"] != "python" or not all(
            isinstance(case[k], str) and 0 < len(case[k].encode()) <= MAX_SCALAR_BYTES
            for k in scalar_keys
        ):
            fail(f"{case_id}: invalid or oversized scalar contract fields")
        if case["expected_failure_class"] not in FAILURE_CLASSES:
            fail(f"{case_id}: unsupported expected failure class")
        if case["evidence_type"] != "synthetic_stub":
            fail(f"{case_id}: only explicitly labelled synthetic_stub fixtures are permitted here")
        if not case["evidence_note"].startswith("Synthetic local API stub"):
            fail(f"{case_id}: synthetic evidence note is missing")
        fixture = safe_path(case["workspace_fixture"], f"{case_id}: fixture")
        if not fixture.is_dir() or fixture.is_symlink():
            fail(f"{case_id}: fixture directory missing or unsafe")
        command = case["test_command"]
        if (
            not isinstance(command, list)
            or not 2 <= len(command) <= MAX_ARG_COUNT
            or not all(isinstance(arg, str) and 0 < len(arg.encode()) <= MAX_ARG_BYTES for arg in command)
            or any(Path(arg).is_absolute() or ".." in Path(arg).parts for arg in command)
        ):
            fail(f"{case_id}: test_command must be a bounded relative argv list")
        if command[0] != "python3" or command[1] != "oracle.py":
            fail(f"{case_id}: oracle must use portable python3 oracle.py argv")
        fixture_files = []
        for path in sorted(fixture.rglob("*")):
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.is_symlink() or not path.is_file():
                fail(f"{case_id}: symlink or special fixture entry {path.name}")
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                fail(f"{case_id}: oversized fixture file {path.name}")
            fixture_files.append(path)
            corpus_bytes += size
            tracked.append(path)
        if len(fixture_files) == 0 or len(fixture_files) > MAX_FIXTURE_FILES:
            fail(f"{case_id}: fixture file count exceeds cap")
        if not (fixture / "oracle.py").is_file() or (fixture / "oracle.py").is_symlink():
            fail(f"{case_id}: missing or unsafe oracle.py")
        if corpus_bytes > MAX_CORPUS_BYTES:
            fail("corpus exceeds total byte cap")
        print(f"{case_id}: ", end="", flush=True)
        returncode, stdout, stderr, overflow, timed_out = run_bounded(command, fixture)
        if timed_out:
            fail(f"{case_id}: oracle timed out and was terminated")
        if overflow:
            fail(f"{case_id}: oracle output exceeds byte cap")
        if returncode == 0:
            fail(f"{case_id}: oracle unexpectedly passed before agent edit")
        lines = [line for line in stdout.splitlines() if line.strip()]
        try:
            observed = json.loads(lines[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            fail(f"{case_id}: oracle output is not machine-readable: {exc}; stderr={stderr[-200:]}")
        if observed.get("case_id") != case_id:
            fail(f"{case_id}: oracle case id mismatch")
        if observed.get("failure_class") != case["expected_failure_class"]:
            fail(f"{case_id}: expected {case['expected_failure_class']}, got {observed.get('failure_class')}")
        if observed.get("setup_failure") is not False or observed.get("status") != "initial_failure":
            fail(f"{case_id}: oracle did not prove a bounded initial failure")
        class_counts[observed["failure_class"]] = class_counts.get(observed["failure_class"], 0) + 1
        print(observed["failure_class"])
        cases.append({"id": case_id, "evidence_type": case["evidence_type"], "initial_failure": observed})
    if class_counts.get("general_coding_error", 0) < 1:
        fail("corpus needs at least one general_coding_error no-lift control")
    if class_counts.get("wrong_version_api", 0) < 10:
        fail("corpus needs at least ten wrong_version_api cases")
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
