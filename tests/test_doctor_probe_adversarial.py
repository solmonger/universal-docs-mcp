from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from universal_docs_mcp import product_cli
from universal_docs_mcp.command_hook import MAX_HOOK_OUTPUT_BYTES

REQUEST = {
    "package": "fixture-docs",
    "ecosystem": "python",
    "selection": "requested",
    "requested_version": "1.2.3",
    "query": "timeouts",
    "section_ids": ["usage"],
    "context_max_bytes": 2048,
    "freshness_mode": "require_check",
    "deadline_ms": 5000,
}


def _adapter(tmp_path: Path, timeout_ms: int = 1000) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "adapter.json"
    path.write_text(
        json.dumps(
            {
                "preflight_command": ["/usr/bin/true"],
                "request": REQUEST,
                "timeout_ms": timeout_ms,
            }
        ),
        encoding="utf-8",
    )
    return path


def _hook(tmp_path: Path, body: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "hook.py"
    path.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time, json\n"
        "sys.stdin.buffer.read()\n"
        + body
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _probe(tmp_path: Path, body: str, *, timeout_ms: int = 1000):
    return product_cli._doctor_probe(
        _hook(tmp_path, body), _adapter(tmp_path, timeout_ms), REQUEST
    )


@pytest.mark.parametrize(
    ("label", "body", "expected"),
    [
        ("no stdout", "pass", "probe_no_stdout"),
        ("whitespace", "sys.stdout.write(' \\n\\t')", "probe_whitespace_stdout"),
        ("malformed utf8", "os.write(1, b'\\xff\\xfe')", "probe_invalid_utf8"),
        ("malformed json", "sys.stdout.write('{')", "probe_malformed_json"),
        ("non-object", "sys.stdout.write('[]')", "probe_result_not_object"),
        (
            "duplicate keys",
            "sys.stdout.write('{\"a\":1,\"a\":2}')",
            "probe_duplicate_json_keys",
        ),
        (
            "multiple values",
            "sys.stdout.write('{} {}')",
            "probe_multiple_json_values",
        ),
        (
            "oversized line",
            f"sys.stdout.write('x' * {MAX_HOOK_OUTPUT_BYTES + 1})",
            "probe_stdout_too_large",
        ),
        (
            "unbounded no newline",
            "sys.stdout.write('x' * 4096); sys.stdout.flush(); time.sleep(10)",
            "probe_timeout",
        ),
        (
            "oversized stderr",
            "sys.stderr.write('e' * 16385); sys.stderr.flush()",
            "probe_stderr_too_large",
        ),
        (
            "nonzero",
            "sys.stdout.write('{}'); sys.exit(7)",
            "probe_nonzero_exit",
        ),
        (
            "signal",
            "os.kill(os.getpid(), signal.SIGTERM)",
            "probe_signal_exit",
        ),
    ],
)
def test_probe_runtime_result_classification(tmp_path: Path, label: str, body: str, expected: str):
    if "signal." in body:
        body = "import signal\n" + body
    started = time.monotonic()
    meta, reason = _probe(
        tmp_path / label.replace(" ", "-"),
        body,
        timeout_ms=100 if expected == "probe_timeout" else 1000,
    )
    elapsed = time.monotonic() - started
    assert reason == expected, label
    assert meta == {} or set(meta) <= {"timeout_ms", "returncode"}
    assert elapsed < 1.0


def test_probe_preflight_taxonomy_and_strict_result_shape(tmp_path: Path):
    for reason in (
        "preflight_not_found",
        "preflight_empty_context",
        "preflight_invalid_receipt",
        "preflight_no_match",
        "preflight_unavailable",
        "adapter_output_too_large",
        "response_too_large",
    ):
        body = (
            "sys.stdout.write(" + repr(
                json.dumps(
                    {
                        "decision": "block",
                        "reason": f"Universal Docs preflight blocked this prompt: {reason}.",
                    }
                )
            ) + ")"
        )
        meta, actual = _probe(tmp_path / reason, body)
        assert meta == {} and actual == reason

    for body, expected in (
        ("sys.stdout.write(json.dumps({'extra': 1, 'hookSpecificOutput': {}}))", "probe_result_extra_fields"),
        ("sys.stdout.write(json.dumps({'hookSpecificOutput': {'event': 'UserPromptSubmit', 'additionalContext': 'x'}}))", "probe_result_invalid_fields"),
        ("sys.stdout.write(json.dumps({'hookSpecificOutput': {'hookEventName': 'wrong', 'additionalContext': 'x'}}))", "probe_result_invalid_fields"),
        ("sys.stdout.write(json.dumps({'hookSpecificOutput': {}}))", "probe_result_invalid_fields"),
        ("sys.stdout.write(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': 3}}))", "probe_result_missing_required_fields"),
        ("sys.stdout.write(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': 'x'}}))", "probe_source_contract_invalid"),
    ):
        meta, actual = _probe(tmp_path / str(abs(hash(body))), body)
        assert meta == {} and actual == expected


def test_probe_kills_descendants_after_timeout_and_parent_exit(tmp_path: Path):
    pid_file = tmp_path / "child.pid"
    body = (
        f"child = __import__('subprocess').Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(30)"
    )
    started = time.monotonic()
    _, reason = _probe(tmp_path / "timeout", body, timeout_ms=500)
    assert reason == "probe_timeout"
    assert time.monotonic() - started < 1.0
    child_pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)

    pid_file = tmp_path / "parent-exit.pid"
    body = (
        f"child = __import__('subprocess').Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "sys.exit(0)"
    )
    started = time.monotonic()
    _, reason = _probe(tmp_path / "parent-exit", body, timeout_ms=1000)
    assert reason == "probe_no_stdout"
    assert time.monotonic() - started < 1.0
    child_pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_success_metadata_is_source_bearing_and_receipt_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    secret = "SOURCE_BODY_SECRET_MARKER"
    context = (
        "UNIVERSAL-DOCS PREFLIGHT CONTEXT PACKET v1\n"
        'Target package: "fixture-docs"\nTarget version: "1.2.3"\n'
        'Source kind: "pypi_description"\nSource version binding: "registry_version"\n'
        + "Source SHA-256: " + "a" * 64 + "\n"
        'Freshness policy: "require_check"\nFreshness state: "upstream_checked"\n'
        "--- BEGIN UNTRUSTED DOCUMENTATION DATA ---\n"
        + secret
        + "\n--- END UNTRUSTED DOCUMENTATION DATA ---\n"
    )
    body = "sys.stdout.write(" + repr(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}})) + ")"
    meta, reason = _probe(tmp_path / "success", body)
    assert reason == "source_bearing"
    assert meta["package"] == REQUEST["package"]
    assert meta["version"] == REQUEST["requested_version"]
    assert meta["section_ids"] == REQUEST["section_ids"]
    assert meta["source_body_omitted"] is True
    assert secret not in json.dumps(meta)
    assert len(json.dumps(meta).encode()) < product_cli.MAX_OUTPUT_BYTES
    monkeypatch.setenv("HOME", "/personal/scratch-home")
    monkeypatch.setenv("PYTHONPATH", "/personal/pythonpath")
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    # The probe's explicit environment is intentionally independent of all three.
    env_capture = tmp_path / "env.json"
    script = _hook(tmp_path / "env", f"open({str(env_capture)!r}, 'w').write(json.dumps(dict(os.environ)))")
    product_cli._doctor_probe(script, _adapter(tmp_path / "env"), REQUEST)
    env = json.loads(env_capture.read_text())
    assert env.get("HOME") is None and env.get("PYTHONPATH") is None
    assert env.get("OPENAI_API_KEY") is None


def test_doctor_emission_stays_bounded_for_probe_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "root"
    (root / ".universal-docs").mkdir(parents=True)
    (root / ".claude").mkdir()
    adapter = _adapter(root / ".universal-docs")
    hook = _hook(root / ".universal-docs", "sys.stdout.write('x' * 20000)")
    # Exercise the public doctor receipt around a deliberately failing probe.
    (root / ".universal-docs/adapter.json").write_text(adapter.read_text())
    monkeypatch.setenv("UNIVERSAL_DOCS_INIT_UNIVERSAL_DOCS_COMMAND_HOOK", str(hook))
    monkeypatch.setenv("UNIVERSAL_DOCS_INIT_UNIVERSAL_DOCS_PREFLIGHT", "/bin/true")
    import io
    out = io.BytesIO()
    rc = product_cli.main(["doctor", "--project-root", str(root)], stdout=out)
    assert rc == 1
    assert len(out.getvalue()) <= product_cli.MAX_OUTPUT_BYTES
    assert json.loads(out.getvalue())["schema"] == "universal-docs.doctor/v1"
