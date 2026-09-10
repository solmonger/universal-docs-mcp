"""Opt-in: live docs through the installed OpenClaw embedded lifecycle."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.live
ROOT = Path(__file__).parents[1]
PREFIX = "UNIVERSAL-DOCS PREFLIGHT CONTEXT PACKET v1"


def text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text(part) for part in value)
    if isinstance(value, dict):
        return text(value.get("text", value.get("content", "")))
    return ""


def test_openclaw_embedded_first_and_next_prompt_receive_live_docs(tmp_path):
    cli = os.environ.get("UNIVERSAL_DOCS_OPENCLAW_CLI")
    if not cli:
        pytest.skip(
            "set UNIVERSAL_DOCS_OPENCLAW_CLI to the isolated official runtime entry"
        )
    assert Path(cli).is_file(), "OpenClaw runtime unavailable"
    executable = Path(sys.executable).parent / "universal-docs-context"
    assert executable.is_file(), "context CLI is not installed"
    node = shutil.which("node")
    assert node, "Node runtime unavailable"
    captures = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("content-length", "0")))
            captures.append(json.loads(raw))
            payload = json.dumps(
                {
                    "id": "fixture-response",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "fixture-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "FIXTURE_PROVIDER_OK",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    request_file = tmp_path / "request.json"
    request_file.write_text(
        json.dumps(
            {
                "package": "requests",
                "ecosystem": "python",
                "selection": "requested",
                "requested_version": "2.32.3",
                "query": "install",
                "freshness_mode": "require_check",
                "context_max_bytes": 12000,
                "deadline_ms": 8000,
            }
        )
    )
    plugin = tmp_path / "plugin"
    shutil.copytree(ROOT / "adapters" / "node", plugin)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {
        "plugins": {
            "enabled": True,
            "allow": ["universal-docs-openclaw"],
            "load": {"paths": [str(plugin)]},
            "entries": {
                "universal-docs-openclaw": {
                    "enabled": True,
                    "hooks": {
                        "allowConversationAccess": True,
                        "allowPromptInjection": True,
                    },
                    "config": {
                        "executable": str(executable),
                        "requestFile": str(request_file),
                        "cacheDir": str(tmp_path / "cache"),
                        "timeoutMs": 10000,
                    },
                }
            },
        },
        "models": {
            "providers": {
                "local": {
                    "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                    "apiKey": "fixture-only",
                    "api": "openai-completions",
                    "models": [
                        {
                            "id": "fixture-model",
                            "name": "Local fixture model",
                            "api": "openai-completions",
                            "input": ["text"],
                            "contextWindow": 128000,
                            "maxTokens": 256,
                        }
                    ],
                }
            }
        },
        "agents": {"defaults": {"model": {"primary": "local/fixture-model"}}},
    }
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(json.dumps(config))
    home = tmp_path / "home"
    home.mkdir()
    env = {
        key: os.environ[key]
        for key in ("PATH", "TMPDIR", "LANG", "LC_ALL")
        if key in os.environ
    }
    env.update(
        {
            "HOME": str(home),
            "OPENCLAW_CONFIG_PATH": str(config_path),
            "OPENCLAW_STATE_DIR": str(tmp_path / "state"),
        }
    )
    session_key = f"agent:main:docs-proof-{uuid.uuid4().hex}"
    runs = []
    try:
        for index in range(2):
            completed = subprocess.run(
                [
                    node,
                    cli,
                    "agent",
                    "--local",
                    "--session-key",
                    session_key,
                    "--message",
                    f"Native documentation fixture prompt {index + 1}",
                    "--json",
                    "--timeout",
                    "35",
                ],
                cwd=home,
                env=env,
                capture_output=True,
                text=True,
                timeout=50,
            )
            runs.append(
                {
                    "returncode": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                }
            )
            assert completed.returncode == 0, completed.stderr
            assert "FIXTURE_PROVIDER_OK" in completed.stdout, completed.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    if directory := os.environ.get("UNIVERSAL_DOCS_HOST_EVIDENCE_DIR"):
        path = Path(directory) / "openclaw-last-attempt.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"status": "attempt", "captures": captures, "runs": runs}, indent=2
            )
        )
    assert len(captures) == 2, captures
    fetched = []
    packets = []
    for index, capture in enumerate(captures):
        messages = capture["messages"]
        current = next(
            message
            for message in messages
            if message["role"] == "user"
            and f"Native documentation fixture prompt {index + 1}"
            in text(message.get("content", ""))
        )
        content = text(current["content"])
        assert PREFIX in content, content
        assert 'Target version: "2.32.3"' in content
        assert "registry_version" in content and "upstream_checked" in content
        assert (
            "Source SHA-256:" in content
            and "END UNTRUSTED DOCUMENTATION DATA" in content
        )
        assert all(
            PREFIX not in text(message.get("content", ""))
            for message in messages
            if message["role"] == "system"
        )
        match = re.search(r"Fetched at \(Unix seconds\): ([0-9.]+)", content)
        assert match, content
        fetched.append(float(match.group(1)))
        packets.append(content)
    assert len(set(fetched)) == 2, fetched
    directory = os.environ.get("UNIVERSAL_DOCS_HOST_EVIDENCE_DIR")
    if directory:
        path = Path(directory) / "openclaw-live-context.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": "universal-docs.openclaw-live-proof/v1",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "source_type": "real_public_preflight",
                    "provider_type": "loopback_fixture",
                    "paid_model_call": False,
                    "active_profile_touched": False,
                    "cli": cli,
                    "context_cli": str(executable),
                    "fetched_at": fetched,
                    "current_user_packets": packets,
                    "provider_requests": captures,
                    "runs": runs,
                },
                indent=2,
            )
        )
