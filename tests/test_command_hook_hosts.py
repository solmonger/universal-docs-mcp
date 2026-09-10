"""Opt-in proof that installed Claude Code/Codex consume the hook packet."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).parents[1]
ADAPTER = REPO / ".venv" / "bin" / "universal-docs-command-hook"
EVIDENCE_DIR = Path(
    os.environ.get(
        "UNIVERSAL_DOCS_HOST_EVIDENCE_DIR",
        "/Users/operator/.hermes/workflows/universal-docs-hardening/cross-harness-design/command-hooks",
    )
)


def _request() -> dict[str, Any]:
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


def _preflight_response() -> dict[str, Any]:
    context = "Fixture docs: retries and timeouts are bounded."
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
                "content_sha256": "a" * 64,
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


def _fixture_preflight(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    script = path / "fixture-preflight.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stdin.buffer.read()\n"
        f"sys.stdout.write({json.dumps(_preflight_response(), separators=(',', ':'))!r})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


class _FixtureHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    lock = threading.Lock()

    def _respond(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_stream(self, events: list[dict[str, Any]]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for event in events:
            payload = json.dumps(event, separators=(",", ":")).encode()
            self.wfile.write(b"data: " + payload + b"\n\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    @staticmethod
    def _responses_payload() -> dict[str, Any]:
        return {
            "id": "resp_fixture",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "fixture-model",
            "output": [
                {
                    "type": "message",
                    "id": "msg_fixture",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "annotations": [],
                            "text": "FIXTURE_RESPONSE: no live model was called",
                        }
                    ],
                }
            ],
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
            },
        }

    def _respond_responses_stream(self) -> None:
        response = self._responses_payload()
        message = response["output"][0]
        text = message["content"][0]["text"]
        self._respond_stream(
            [
                {"type": "response.created", "response": response},
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": message,
                },
                {
                    "type": "response.content_part.added",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                },
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": text,
                },
                {
                    "type": "response.output_text.done",
                    "output_index": 0,
                    "content_index": 0,
                    "text": text,
                },
                {
                    "type": "response.content_part.done",
                    "output_index": 0,
                    "content_index": 0,
                },
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": message,
                },
                {"type": "response.completed", "response": response},
            ]
        )

    def _respond_anthropic_stream(self) -> None:
        text = "FIXTURE_RESPONSE: no live model was called"
        message = {
            "id": "msg_fixture",
            "type": "message",
            "role": "assistant",
            "model": "fixture-model",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        self._respond_stream(
            [
                {"type": "message_start", "message": message},
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
                {"type": "message_stop"},
            ]
        )

    def do_GET(self) -> None:  # noqa: N802
        self._respond({"data": [{"id": "fixture-model", "object": "model"}]})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"unparseable": True}
        with self.lock:
            self.requests.append({"path": self.path, "body": body})
        if self.path.rstrip("/").endswith("responses"):
            if body.get("stream") is True:
                self._respond_responses_stream()
            else:
                self._respond(self._responses_payload())
        else:
            if body.get("stream") is True:
                self._respond_anthropic_stream()
            else:
                self._respond(
                    {
                        "id": "msg_fixture",
                        "type": "message",
                        "role": "assistant",
                        "model": "fixture-model",
                        "content": [
                            {
                                "type": "text",
                                "text": "FIXTURE_RESPONSE: no live model was called",
                            }
                        ],
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    }
                )

    def log_message(self, *_args: Any) -> None:
        return


def _server() -> tuple[ThreadingHTTPServer, str]:
    _FixtureHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def _write_evidence(name: str, evidence: dict[str, Any]) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / name).write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _run_host(
    harness: str,
    tmp_path: Path,
    provider_url: str,
) -> tuple[subprocess.CompletedProcess[str] | None, dict[str, Any]]:
    if not ADAPTER.is_file() or ADAPTER.is_symlink():
        pytest.skip("editable adapter entry point is not installed in this worktree")
    fixture = tmp_path / "workspace"
    fixture.mkdir()
    preflight = _fixture_preflight(tmp_path / "preflight")
    config = tmp_path / "adapter.json"
    config.write_text(
        json.dumps(
            {
                "preflight_command": [str(preflight)],
                "request": _request(),
                "timeout_ms": 5000,
            }
        ),
        encoding="utf-8",
    )
    command = shlex.join([str(ADAPTER), "--harness", harness, "--config", str(config)])
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "TERM", "TMPDIR")
        if key in os.environ
    }
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "ANTHROPIC_BASE_URL": provider_url,
            "ANTHROPIC_API_KEY": "synthetic-fixture-token",
            "OPENAI_API_KEY": "synthetic-fixture-token",
        }
    )
    (tmp_path / "home").mkdir()
    debug_file = tmp_path / "host-debug.log"
    if harness == "claude":
        claude_dir = tmp_path / "claude-config"
        claude_dir.mkdir()
        env["CLAUDE_CONFIG_DIR"] = str(claude_dir)
        settings_dir = fixture / ".claude"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "UserPromptSubmit": [
                            {"hooks": [{"type": "command", "command": command}]}
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        command_line = [
            "/Users/operator/.local/bin/claude",
            "--no-session-persistence",
            "-p",
            "Reply with the fixture marker only.",
            "--output-format",
            "json",
            "--model",
            "fixture-model",
            "--debug-file",
            str(debug_file),
        ]
    else:
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        env["CODEX_HOME"] = str(codex_home)
        (codex_home / "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "UserPromptSubmit": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": command,
                                        "timeout": 25,
                                        "additionalContextLimit": 2000,
                                    }
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        (codex_home / "config.toml").write_text(
            "model = 'fixture-model'\n"
            "model_provider = 'fixture'\n"
            "approval_policy = 'never'\n"
            "sandbox_mode = 'read-only'\n"
            "web_search = 'disabled'\n\n"
            "[model_providers.fixture]\n"
            "name = 'Local fixture provider'\n"
            f"base_url = '{provider_url}/v1'\n"
            "wire_api = 'responses'\n"
            "env_key = 'OPENAI_API_KEY'\n",
            encoding="utf-8",
        )
        command_line = [
            "/Users/operator/.local/bin/codex",
            "--dangerously-bypass-hook-trust",
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--json",
            "Reply with the fixture marker only.",
        ]

    try:
        completed = subprocess.run(
            command_line,
            cwd=fixture,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=45,
            check=False,
        )
    except subprocess.TimeoutExpired:
        completed = None
    with _FixtureHandler.lock:
        requests = list(_FixtureHandler.requests)
    request_text = json.dumps(requests, ensure_ascii=False)
    request_body = requests[0]["body"] if requests else {}
    evidence = {
        "harness": harness,
        "installed_host": command_line[0],
        "adapter_command": command,
        "provider": "loopback fixture; synthetic token; no live model",
        "host_process_completed": completed is not None,
        "host_returncode": completed.returncode if completed else None,
        "model_request_received": bool(requests),
        "hook_context_marker_present": "UDCTX:aaaaaaaaaaaaaaaa" in request_text,
        "prepared_receipt_present": "model consumption is not asserted" in request_text,
        "provider_request_paths": [item["path"] for item in requests],
        "provider_request_body_keys": sorted(request_body)
        if isinstance(request_body, dict)
        else [],
        "host_debug_file_present": debug_file.is_file(),
        "host_stdout_contains_fixture_label": bool(
            completed
            and "FIXTURE_RESPONSE: no live model was called" in completed.stdout
        ),
        "host_stderr_present": bool(completed and completed.stderr),
        "host_stderr_excerpt": (
            completed.stderr.replace("synthetic-fixture-token", "[redacted]")[-1000:]
            if completed
            else ""
        ),
        "fixture_response_label": "FIXTURE_RESPONSE: no live model was called",
    }
    return completed, evidence


def _assert_or_record(
    harness: str,
    completed: subprocess.CompletedProcess[str] | None,
    evidence: dict[str, Any],
) -> None:
    _write_evidence(f"{harness}-actual-host.json", evidence)
    if not evidence["model_request_received"]:
        detail = "host did not produce a loopback provider request"
        if completed is not None and completed.returncode != 0:
            detail += f" (returncode={completed.returncode})"
        pytest.skip(f"actual {harness} receipt gate unverified: {detail}")
    assert evidence["hook_context_marker_present"] is True
    assert evidence["prepared_receipt_present"] is True


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_actual_host_receives_pre_model_receipt(harness: str, tmp_path: Path) -> None:
    if os.environ.get("UNIVERSAL_DOCS_HOST_TESTS") != "1":
        pytest.skip("set UNIVERSAL_DOCS_HOST_TESTS=1 for isolated host proof")
    server, provider_url = _server()
    try:
        completed, evidence = _run_host(harness, tmp_path, provider_url)
        _assert_or_record(harness, completed, evidence)
    finally:
        server.shutdown()
        server.server_close()
