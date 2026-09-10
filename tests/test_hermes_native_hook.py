"""Native installed-Hermes lifecycle proof for the plugin candidate."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
CANDIDATE = ROOT / "examples" / "hermes" / "universal-docs-preflight"
INSTALLED_HERMES_PYTHON = Path(
    os.environ.get(
        "UNIVERSAL_DOCS_HERMES_PYTHON",
        str(Path.home() / ".hermes/hermes-agent/.venv/bin/python"),
    )
)


_NATIVE_DRIVER = r"""
import contextlib
import io
import json
import sys


def main():
    from run_agent import AIAgent
    from agent.conversation_loop import (
        _install_safe_stdio,
        _ra,
        _restore_or_build_system_prompt,
        _sanitize_surrogates,
        _summarize_user_message_for_log,
    )
    from agent.turn_context import build_api_messages, build_turn_context
    from hermes_logging import set_session_context
    from tools.skill_provenance import set_current_write_origin

    agent = AIAgent(
        base_url="http://127.0.0.1:9/v1",
        api_key="synthetic-test-only",
        provider="openai",
        api_mode="chat_completions",
        model="installed-hermes-test-model",
        session_id="native-hook-proof",
        platform="cli",
        quiet_mode=True,
        skip_context_files=True,
        load_soul_identity=False,
        skip_memory=True,
        enabled_toolsets=[],
        disabled_toolsets=[],
        session_db=None,
    )
    system_before = "NATIVE BASE SYSTEM"

    def turn(user, task_id, history, system_message):
        context = build_turn_context(
            agent,
            user,
            system_message,
            history,
            task_id,
            None,
            None,
            restore_or_build_system_prompt=_restore_or_build_system_prompt,
            install_safe_stdio=_install_safe_stdio,
            sanitize_surrogates=_sanitize_surrogates,
            summarize_user_message_for_log=_summarize_user_message_for_log,
            set_session_context=set_session_context,
            set_current_write_origin=set_current_write_origin,
            ra=_ra,
        )
        api_messages, effective_system = build_api_messages(
            agent,
            context.messages,
            current_turn_user_idx=context.current_turn_user_idx,
            ext_prefetch_cache=context.ext_prefetch_cache,
            plugin_user_context=context.plugin_user_context,
            moa_config=None,
            active_system_prompt=context.active_system_prompt,
        )
        current = api_messages[-1]
        return {
            "turn_id": context.turn_id,
            "plugin_context": context.plugin_user_context,
            "current_user_api_content": current["content"],
            "durable_user_content": context.messages[context.current_turn_user_idx]["content"],
            "api_system": api_messages[0]["content"],
            "effective_system": effective_system,
            "current_user_role": current["role"],
            "sidecar_present": "api_content" in context.messages[context.current_turn_user_idx],
            "messages": context.messages,
        }

    first = turn("first user request", "task-first", [], "NATIVE BASE SYSTEM")
    system_before = first["api_system"]
    next_history = first.pop("messages") + [{"role": "assistant", "content": "prior reply"}]
    second = turn("fresh next-turn request", "task-next", next_history, "MUST NOT REPLACE SYSTEM")
    compacted_history = [{"role": "assistant", "content": "[compacted handoff: prior work retained]"}]
    compacted = turn("resumed after compaction", "task-resumed", compacted_history, "MUST NOT REPLACE SYSTEM")
    return {
        "installed_lifecycle": True,
        "turns": [first, second, compacted],
        "system_before": system_before,
        "system_unchanged": all(item["api_system"] == system_before for item in (first, second, compacted)),
        "user_durable_content_unchanged": all(
            item["durable_user_content"] in {
                "first user request", "fresh next-turn request", "resumed after compaction"
            }
            for item in (first, second, compacted)
        ),
    }


# Keep import/host diagnostics out of the machine-readable proof artifact.
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    result = main()
sys.__stdout__.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
"""


def _write_fixture_preflight(tmp_path: Path) -> tuple[Path, Path]:
    requests = tmp_path / "native-requests.jsonl"
    executable = tmp_path / "native-context"
    frame = {
        "schema": "universal-docs.context/v1",
        "status": "prepared",
        "error": None,
        "context": "UNIVERSAL-DOCS PREFLIGHT CONTEXT PACKET v1\nStatus: prepared\nReceipt: UDCTX:fixture-native\nSource URL: https://docs.example/native/9.9.9\nUNTRUSTED DOCUMENTATION DATA\nnative-fixture-documentation",
    }
    executable.write_text(
        "#!" + sys.executable + "\nimport json,sys\nfrom pathlib import Path\n"
        f"log = Path({str(requests)!r})\n"
        "request = json.loads(Path(sys.argv[2]).read_text())\n"
        "with log.open('a') as stream: stream.write(json.dumps(request)+'\\n')\n"
        f"print({json.dumps(frame)!r})\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable, requests


def test_installed_hermes_lifecycle_injects_current_user_context_in_isolated_process(
    tmp_path,
):
    if os.environ.get("UNIVERSAL_DOCS_HERMES_NATIVE_TESTS") != "1":
        pytest.skip("set UNIVERSAL_DOCS_HERMES_NATIVE_TESTS=1 for native host proof")
    assert INSTALLED_HERMES_PYTHON.is_file(), (
        "configured Hermes interpreter is unavailable"
    )
    executable, requests = _write_fixture_preflight(tmp_path)
    request_file = tmp_path / "request.json"
    request_file.write_text(
        json.dumps(
            {
                "package": "native-package",
                "ecosystem": "python",
                "selection": "requested",
                "requested_version": "9.9.9",
                "query": "native tools",
                "section_ids": ["usage"],
                "context_max_bytes": 4000,
                "freshness_mode": "require_check",
                "deadline_ms": 1000,
            }
        )
    )
    home = tmp_path / "hermes-home"
    plugin_dir = home / "plugins" / "universal-docs-preflight"
    shutil.copytree(CANDIDATE, plugin_dir, ignore=shutil.ignore_patterns("__pycache__"))
    config = {
        "plugins": {
            "enabled": ["universal-docs-preflight"],
            "hook_callback_timeout": 5,
            "entries": {
                "universal-docs-preflight": {
                    "settings": {
                        "executable": str(executable),
                        "request_file": str(request_file),
                        "timeout_ms": 2000,
                    }
                }
            },
        }
    }
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    driver = tmp_path / "native_driver.py"
    driver.write_text(_NATIVE_DRIVER, encoding="utf-8")

    env = {
        "HERMES_HOME": str(home),
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
    }
    completed = subprocess.run(
        [str(INSTALLED_HERMES_PYTHON), str(driver)],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    proof = json.loads(completed.stdout)
    turns = proof["turns"]
    assert proof["installed_lifecycle"] is True
    assert proof["system_unchanged"] is True
    assert proof["user_durable_content_unchanged"] is True
    assert [turn["current_user_role"] for turn in turns] == ["user", "user", "user"]
    assert all(turn["sidecar_present"] for turn in turns)
    assert all(
        "native-fixture-documentation" in turn["plugin_context"] for turn in turns
    )
    assert all(
        "https://docs.example/native/9.9.9" in turn["plugin_context"] for turn in turns
    )
    assert all(
        "native-fixture-documentation" in turn["current_user_api_content"]
        for turn in turns
    )
    assert all("NATIVE BASE SYSTEM" in turn["api_system"] for turn in turns)
    assert all("MUST NOT REPLACE SYSTEM" not in turn["api_system"] for turn in turns)

    child_requests = [
        json.loads(line) for line in requests.read_text(encoding="utf-8").splitlines()
    ]
    assert len(child_requests) == 3
    assert all(request["package"] == "native-package" for request in child_requests)
    assert all(request["requested_version"] == "9.9.9" for request in child_requests)
    assert all(request["query"] == "native tools" for request in child_requests)
    assert all(
        "first user request" not in json.dumps(request) for request in child_requests
    )
    assert all(
        "compacted handoff" not in json.dumps(request) for request in child_requests
    )
    assert all("HERMES_HOME" not in json.dumps(request) for request in child_requests)
    evidence = os.environ.get("UNIVERSAL_DOCS_HERMES_HOST_EVIDENCE_DIR")
    if evidence:
        import hashlib
        from datetime import datetime, timezone

        directory = Path(evidence)
        directory.mkdir(parents=True, exist_ok=True)
        artifact = {
            "schema": "universal-docs.hermes-host-proof/v1",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "scope": "native_lifecycle_message_assembly",
            "source_type": "fixture_context_cli",
            "provider_request_sent": False,
            "hermes_python": str(INSTALLED_HERMES_PYTHON),
            "adapter_sha256": hashlib.sha256(
                (CANDIDATE / "hermes_hook.py").read_bytes()
            ).hexdigest(),
            "child_invocations": len(child_requests),
            "proof": proof,
        }
        (directory / "hermes-native-assembly.json").write_text(
            json.dumps(artifact, indent=2) + "\n"
        )
