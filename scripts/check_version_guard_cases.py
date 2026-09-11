#!/usr/bin/env python3
"""Validate and receipt the blind, offline Version Guard fixture corpus."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn, cast

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "benchmarks" / "version_guard"
CASES = CORPUS / "cases"
ORACLES = CORPUS / "oracles"
RECEIPT = CORPUS / "corpus_receipt.json"
REQUIRED = {
    "id", "ecosystem", "package", "target_version", "task", "workspace_fixture",
    "test_command", "context_profile", "expected_failure_class", "evidence_type",
    "evidence_note", "oracle_file",
}
FAILURE_CLASSES = {"wrong_version_api", "general_coding_error"}
ID_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
MAX_CASES = 64
MAX_MANIFEST_BYTES = 16 * 1024
MAX_SCALAR_BYTES = 4 * 1024
MAX_ARG_COUNT = 8
MAX_ARG_BYTES = 256
MAX_FIXTURE_FILES = 32
MAX_FILE_BYTES = 64 * 1024
MAX_CORPUS_BYTES = 512 * 1024
MAX_ORACLE_BYTES = 64 * 1024
MAX_ORACLE_OUTPUT_BYTES = 64 * 1024
ORACLE_TIMEOUT_SECONDS = 5


def fail(message: str) -> NoReturn:
    raise SystemExit(f"ERROR: {message}")


def digest(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for path in sorted(paths, key=lambda p: p.relative_to(ROOT).as_posix()):
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
        except ProcessLookupError:
            pass
    else:
        process.kill()


def run_bounded(command: list[str], cwd: Path) -> tuple[int, str, str, bool, bool]:
    """Run an oracle with the existing bounded output/timeout contract."""
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        kwargs: dict[str, Any] = {"stdout": stdout_file, "stderr": stderr_file, "cwd": cwd}
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
        return process.returncode, stdout[:MAX_ORACLE_OUTPUT_BYTES].decode("utf-8", "replace"), stderr[:MAX_ORACLE_OUTPUT_BYTES].decode("utf-8", "replace"), overflow, timed_out


def safe_path(raw: str, label: str, base: Path = ROOT) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        fail(f"{label}: absolute path or parent traversal is forbidden")
    current = base
    for part in candidate.parts:
        current /= part
        if current.is_symlink():
            fail(f"{label}: symlink path component is forbidden")
    resolved = (base / candidate).resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError:
        fail(f"{label}: path escapes repository")
    return resolved


def _regular_bounded(path: Path, label: str, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        fail(f"{label}: must be a regular non-symlink file")
    if path.stat().st_size > limit:
        fail(f"{label}: exceeds {limit}-byte bound")
    return path.read_bytes()


def expected_api_token(oracle: Path, case_id: str) -> str:
    data = _regular_bounded(oracle, f"{case_id} oracle", MAX_ORACLE_BYTES)
    try:
        tree = ast.parse(data.decode("utf-8"), filename=str(oracle))
    except (SyntaxError, UnicodeDecodeError) as exc:
        fail(f"{case_id}: oracle is not valid UTF-8 Python: {exc}")
    values = [
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "EXPECTED_API" for target in node.targets)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    if len(values) != 1 or not values[0]:
        fail(f"{case_id}: oracle must declare one non-empty EXPECTED_API string")
    return cast(str, values[0])


def fixture_files(fixture: Path, case_id: str) -> list[Path]:
    if not fixture.is_dir() or fixture.is_symlink():
        fail(f"{case_id}: fixture directory missing or unsafe")
    files: list[Path] = []
    for path in sorted(fixture.rglob("*")):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink() or not path.is_file():
            fail(f"{case_id}: symlink or special fixture entry {path.name}")
        if path.name in {"oracle.py", "versioned_api.py"}:
            fail(f"{case_id}: oracle/API stub file is visible in fixture")
        _regular_bounded(path, f"{case_id} fixture {path.name}", MAX_FILE_BYTES)
        files.append(path)
    if not files or len(files) > MAX_FIXTURE_FILES:
        fail(f"{case_id}: fixture file count outside 1..{MAX_FIXTURE_FILES}")
    return files


def check_case(manifest_path: Path) -> tuple[dict[str, Any], list[Path], Path, str, dict[str, Any]]:
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        fail(f"{manifest_path.name}: manifest exceeds size cap")
    try:
        case = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"{manifest_path.name}: invalid JSON: {exc}")
    if not isinstance(case, dict) or set(case) != REQUIRED:
        fail(f"{manifest_path.name}: manifest keys must equal the required contract")
    case_id = case["id"]
    if not isinstance(case_id, str) or not ID_RE.fullmatch(case_id):
        fail(f"{manifest_path.name}: invalid id")
    if case["ecosystem"] != "python" or case["expected_failure_class"] not in FAILURE_CLASSES:
        fail(f"{case_id}: invalid ecosystem or failure class")
    if case["evidence_type"] != "synthetic_stub" or not case["evidence_note"].startswith("Synthetic local API stub"):
        fail(f"{case_id}: only explicitly labelled synthetic_stub fixtures are permitted")
    if not all(isinstance(case[key], str) and 0 < len(case[key].encode()) <= MAX_SCALAR_BYTES for key in REQUIRED - {"test_command"}):
        fail(f"{case_id}: invalid or oversized scalar contract field")
    command = case["test_command"]
    if not isinstance(command, list) or not 2 <= len(command) <= MAX_ARG_COUNT or not all(isinstance(arg, str) and 0 < len(arg.encode()) <= MAX_ARG_BYTES for arg in command):
        fail(f"{case_id}: test_command must be a bounded argv list")
    if any(Path(arg).is_absolute() or ".." in Path(arg).parts for arg in command):
        fail(f"{case_id}: test_command contains unsafe path")
    fixture = safe_path(case["workspace_fixture"], f"{case_id}: fixture")
    visible = fixture_files(fixture, case_id)
    oracle = safe_path(case["oracle_file"], f"{case_id}: oracle")
    if not oracle.is_relative_to(ORACLES.resolve()):
        fail(f"{case_id}: oracle must live under benchmarks/version_guard/oracles")
    _regular_bounded(oracle, f"{case_id} oracle", MAX_ORACLE_BYTES)
    expected = expected_api_token(oracle, case_id)
    visible_data = [manifest_path.read_bytes(), case["task"].encode(), *(path.read_bytes() for path in visible)]
    if any(expected.encode() in data for data in visible_data):
        fail(f"{case_id}: EXPECTED_API token leaks into task or visible workspace")
    with tempfile.TemporaryDirectory(prefix="version-guard-corpus-") as temporary:
        workspace = Path(temporary) / "workspace"
        import shutil
        shutil.copytree(fixture, workspace)
        command = [sys.executable, str(oracle), str(workspace)]
        returncode, stdout, stderr, overflow, timed_out = run_bounded(command, workspace)
    if timed_out:
        fail(f"{case_id}: external oracle timed out")
    if overflow:
        fail(f"{case_id}: external oracle output exceeds byte cap")
    if returncode == 0:
        fail(f"{case_id}: oracle unexpectedly passed before agent edit")
    lines = [line for line in stdout.splitlines() if line.strip()]
    try:
        observed = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        fail(f"{case_id}: oracle output is not machine-readable: {exc}; stderr={stderr[-200:]}")
    if observed.get("case_id") != case_id or observed.get("failure_class") != case["expected_failure_class"] or observed.get("setup_failure") is not False or observed.get("status") != "initial_failure":
        fail(f"{case_id}: external oracle did not report declared baseline class")
    return case, [manifest_path, *visible], oracle, expected, observed


def main() -> int:
    if not CASES.is_dir() or CASES.is_symlink() or not ORACLES.is_dir() or ORACLES.is_symlink():
        fail("missing or unsafe cases/oracles directory")
    manifests = sorted(CASES.glob("*.json"))
    if not 10 <= len(manifests) <= MAX_CASES:
        fail(f"case count {len(manifests)} outside 10..{MAX_CASES}")
    cases: list[dict[str, Any]] = []
    tracked: list[Path] = []
    ids: set[str] = set()
    class_counts: dict[str, int] = {}
    corpus_bytes = 0
    for manifest_path in manifests:
        case, visible, oracle, expected, observed = check_case(manifest_path)
        if case["id"] in ids:
            fail(f"duplicate case id: {case['id']}")
        ids.add(case["id"])
        tracked.extend(visible)
        tracked.append(oracle)
        corpus_bytes += sum(path.stat().st_size for path in [*visible, oracle])
        if corpus_bytes > MAX_CORPUS_BYTES:
            fail("corpus exceeds total byte cap")
        class_counts[observed["failure_class"]] = class_counts.get(observed["failure_class"], 0) + 1
        print(f"{case['id']}: {observed['failure_class']}")
        cases.append({"id": case["id"], "evidence_type": case["evidence_type"], "initial_failure": observed})
    if class_counts.get("general_coding_error", 0) < 1:
        fail("corpus needs at least one general_coding_error no-lift control")
    if class_counts.get("wrong_version_api", 0) < 10:
        fail("corpus needs at least ten wrong_version_api cases")
    receipt = {
        "schema": "universal-docs.corpus-receipt/v1",
        "case_count": len(cases),
        "class_counts": class_counts,
        "case_ids": [case["id"] for case in cases],
        "cases": cases,
        "corpus_sha256": digest(tracked),
    }
    RECEIPT.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"CORPUS_RECEIPT={RECEIPT.relative_to(ROOT)}")
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
