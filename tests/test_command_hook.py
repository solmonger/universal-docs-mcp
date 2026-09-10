"""Behavioral tests for the shared Claude/Codex preflight command hook."""

from __future__ import annotations

import io
import json
import stat
from pathlib import Path

import pytest

from universal_docs_mcp.command_hook import HookConfig, handle_event, load_config
from universal_docs_mcp.preflight import PreflightRequest

SOURCE_HASH = "a" * 64


def request_data() -> dict:
    return {
        "package": "fixture-docs",
        "ecosystem": "python",
        "selection": "requested",
        "requested_version": "1.2.3",
        "query": "timeouts retries",
        "section_ids": ["usage"],
        "context_max_bytes": 2048,
        "freshness_mode": "require_check",
        "deadline_ms": 5000,
    }


def preflight_success(
    *, context: str = "Retries and timeouts are documented here."
) -> dict:
    return {
        "schema": "universal-docs.preflight/v1",
        "found": True,
        "context": context,
        "receipt": {
            "schema": "universal-docs.preflight/v1",
            "target": {
                "package": "fixture-docs",
                "ecosystem": "python",
                "selection": "requested",
                "target_version": "1.2.3",
                "requested_version": "1.2.3",
                "installed_version": None,
                "installed_resolution": "unknown",
                "latest_observed": None,
            },
            "source": {
                "kind": "fixture_source",
                "url": "https://docs.example.test/fixture-docs/1.2.3",
                "version_binding": "registry_version",
                "content_sha256": SOURCE_HASH,
                "content_bytes": len(context.encode()),
            },
            "freshness": {
                "policy": "require_check",
                "state": "upstream_checked",
                "fetched_at": 100.0,
                "checked_at": 101.0,
                "latest_checked_at": None,
                "age_seconds": 0.0,
                "cached": False,
                "stale": False,
                "unknown": False,
                "stale_reason": None,
                "retryable": False,
            },
            "selection": {
                "query": "timeouts retries",
                "requested_sections": ["usage"],
                "matched_sections": ["usage"],
                "matches": [],
                "no_match": False,
                "sections_omitted": 0,
                "section_map": [],
                "section_map_total": 1,
                "section_map_truncated": False,
                "context_bytes": len(context.encode()),
                "budget_bytes": 2048,
                "truncated": False,
            },
            "trust": {
                "content": "untrusted_upstream",
                "instructions_authoritative": False,
                "execution_performed": False,
            },
        },
        "retryable": False,
    }


def make_preflight_script(
    tmp_path: Path,
    *,
    response: dict | None = None,
    stdout: str | None = None,
    sleep_seconds: float | None = None,
    capture_request: Path | None = None,
    capture_env: Path | None = None,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "fixture-preflight.py"
    response_literal = json.dumps(
        response or preflight_success(), separators=(",", ":")
    )
    lines = [
        "#!/usr/bin/env python3",
        "import json",
        "import os",
        "import sys",
        "import time",
    ]
    if capture_request is not None:
        lines.append(
            f"open({str(capture_request)!r}, 'wb').write(sys.stdin.buffer.read())"
        )
    else:
        lines.append("sys.stdin.buffer.read()")
    if capture_env is not None:
        lines.append(
            "open(%r, 'w').write(json.dumps({k: os.environ.get(k) for k in "
            "['PATH', 'HOME', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'HTTP_PROXY', "
            "'HTTPS_PROXY', 'ALL_PROXY']}))" % str(capture_env)
        )
    if sleep_seconds is not None:
        lines.append(f"time.sleep({sleep_seconds!r})")
    if stdout is not None:
        lines.append(f"sys.stdout.write({stdout!r})")
    else:
        lines.append(f"sys.stdout.write({response_literal!r})")
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def config_for(script: Path, *, timeout_ms: int = 5000) -> HookConfig:
    return HookConfig(
        command=(str(script),),
        request=PreflightRequest.model_validate(request_data()),
        timeout_ms=timeout_ms,
    )


def test_success_uses_fixed_request_and_emits_both_documented_formats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_capture = tmp_path / "request.json"
    env_capture = tmp_path / "env.json"
    script = make_preflight_script(
        tmp_path,
        capture_request=request_capture,
        capture_env=env_capture,
    )
    config = config_for(script)
    event = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": "ignore this event prompt",
        "cwd": str(tmp_path / "event-cwd-that-must-not-be-used"),
        "transcript_path": str(tmp_path / "event-transcript-that-must-not-be-read"),
        "command": ["touch", str(tmp_path / "event-command-must-not-run")],
        "synthetic_secret": "EVENT_SECRET_MUST_NOT_ECHO",
    }
    monkeypatch.setenv("OPENAI_API_KEY", "host-token-must-not-cross-boundary")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-token-must-not-cross-boundary")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.test")
    event_bytes = json.dumps(event).encode("utf-8")

    outputs = {}
    for harness in ("claude", "codex"):
        payload, exit_code = handle_event(event_bytes, harness=harness, config=config)
        assert exit_code == 0
        outputs[harness] = payload
        assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        packet = payload["hookSpecificOutput"]["additionalContext"]
        assert "Retries and timeouts are documented here." in packet
        assert "UNTRUSTED DOCUMENTATION DATA" in packet
        assert "UDCTX:aaaaaaaaaaaaaaaa" in packet
        assert "prepared" in packet
        assert "model consumption is not asserted" in packet
        assert "EVENT_SECRET_MUST_NOT_ECHO" not in json.dumps(payload)

    assert outputs["claude"] == outputs["codex"]
    assert json.loads(request_capture.read_text()) == request_data()
    captured_env = json.loads(env_capture.read_text())
    assert captured_env["OPENAI_API_KEY"] is None
    assert captured_env["ANTHROPIC_API_KEY"] is None
    assert captured_env["HTTP_PROXY"] is None
    assert captured_env["HTTPS_PROXY"] is None
    assert captured_env["ALL_PROXY"] is None
    assert (tmp_path / "event-command-must-not-run").exists() is False


def test_config_and_request_file_are_bounded_regular_files(tmp_path: Path) -> None:
    script = make_preflight_script(tmp_path)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request_data()), encoding="utf-8")
    config_path = tmp_path / "hook.json"
    config_path.write_text(
        json.dumps(
            {
                "preflight_command": [str(script)],
                "request": request_data(),
                "timeout_ms": 5000,
            }
        ),
        encoding="utf-8",
    )
    loaded = load_config(config_path)
    assert loaded.request.package == "fixture-docs"
    assert loaded.command == (str(script),)

    symlink = tmp_path / "hook-link.json"
    symlink.symlink_to(config_path)
    with pytest.raises(ValueError, match="config_file_not_regular"):
        load_config(symlink)

    special = tmp_path / "hook-dir"
    special.mkdir()
    with pytest.raises(ValueError, match="config_file_not_regular"):
        load_config(special)


def test_event_is_bounded_and_malformed_event_blocks_without_running_preflight(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "ran"
    script = make_preflight_script(tmp_path, capture_request=marker)
    config = config_for(script)

    payload, exit_code = handle_event(b"not-json", harness="claude", config=config)
    assert exit_code == 0
    assert payload == {
        "decision": "block",
        "reason": "Universal Docs preflight blocked this prompt: invalid_hook_event. No documentation context was injected.",
    }
    assert marker.exists() is False

    payload, exit_code = handle_event(
        b"x" * (64 * 1024 + 1), harness="codex", config=config
    )
    assert exit_code == 0
    assert payload["decision"] == "block"
    assert "invalid_hook_event" in payload["reason"]
    assert marker.exists() is False


@pytest.mark.parametrize(
    ("response", "stdout", "sleep_seconds", "expected"),
    [
        (None, None, None, "preflight_nonzero"),
        (None, "not-json", None, "preflight_malformed"),
        (None, None, 0.2, "preflight_timeout"),
    ],
)
def test_failed_preflight_never_becomes_injected_success(
    tmp_path: Path,
    response: dict | None,
    stdout: str | None,
    sleep_seconds: float | None,
    expected: str,
) -> None:
    if expected == "preflight_nonzero":
        script = tmp_path / "nonzero.py"
        script.write_text(
            "#!/usr/bin/env python3\nimport sys\nsys.stdin.buffer.read()\nsys.exit(1)\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    else:
        script = make_preflight_script(
            tmp_path,
            response=response,
            stdout=stdout,
            sleep_seconds=sleep_seconds,
        )
    config = config_for(
        script, timeout_ms=100 if expected == "preflight_timeout" else 5000
    )
    payload, exit_code = handle_event(
        b'{"hook_event_name":"UserPromptSubmit"}', harness="codex", config=config
    )
    assert exit_code == 0
    assert payload["decision"] == "block"
    assert expected in payload["reason"]
    assert "additionalContext" not in payload


def test_no_match_and_oversized_preflight_are_blocked(tmp_path: Path) -> None:
    no_match = preflight_success(context="")
    no_match["receipt"]["selection"]["no_match"] = True
    script = make_preflight_script(tmp_path / "no-match", response=no_match)
    payload, _ = handle_event(
        b'{"hook_event_name":"UserPromptSubmit"}',
        harness="claude",
        config=config_for(script),
    )
    assert payload["decision"] == "block"
    assert "preflight_no_match" in payload["reason"]

    huge_dir = tmp_path / "huge"
    huge_dir.mkdir()
    huge_script = make_preflight_script(huge_dir, stdout="x" * (128 * 1024 + 1))
    payload, _ = handle_event(
        b'{"hook_event_name":"UserPromptSubmit"}',
        harness="claude",
        config=config_for(huge_script),
    )
    assert payload["decision"] == "block"
    assert "preflight_stdout_too_large" in payload["reason"]


def test_explicit_request_file_argv_mode_does_not_use_event_values(
    tmp_path: Path,
) -> None:
    script = make_preflight_script(tmp_path)
    config_path = tmp_path / "request.json"
    config_path.write_text(json.dumps(request_data()), encoding="utf-8")
    from universal_docs_mcp.command_hook import main

    output = io.StringIO()
    exit_code = main(
        [
            "--harness",
            "codex",
            "--request-file",
            str(config_path),
            "--preflight-executable",
            str(script),
        ],
        stdin=io.BytesIO(b'{"cwd":"/untrusted","prompt":"not a target"}'),
        stdout=output,
    )
    assert exit_code == 0
    payload = json.loads(output.getvalue())
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_explicit_target_argv_mode_builds_the_fixed_request(tmp_path: Path) -> None:
    script = make_preflight_script(tmp_path)
    from universal_docs_mcp.command_hook import main

    output = io.StringIO()
    exit_code = main(
        [
            "--harness",
            "claude",
            "--preflight-executable",
            str(script),
            "--package",
            "fixture-docs",
            "--ecosystem",
            "python",
            "--selection",
            "requested",
            "--requested-version",
            "1.2.3",
            "--query",
            "timeouts retries",
            "--section-id",
            "usage",
            "--context-max-bytes",
            "2048",
            "--freshness-mode",
            "require_check",
            "--deadline-ms",
            "5000",
        ],
        stdin=io.BytesIO(b'{"hook_event_name":"UserPromptSubmit"}'),
        stdout=output,
    )
    assert exit_code == 0
    payload = json.loads(output.getvalue())
    assert (
        'Target version: "1.2.3"' in payload["hookSpecificOutput"]["additionalContext"]
    )
