"""Behavioral tests for the Hermes per-turn docs plugin candidate."""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).parents[1]
HOOK_PATH = ROOT / "examples" / "hermes" / "universal-docs-preflight" / "hermes_hook.py"


class FakeContext:
    def __init__(self, settings: dict[str, object]):
        self.settings = settings
        self.callbacks: dict[str, Callable[..., Any]] = {}

    def get_config(self, key: str, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name: str, callback):
        self.callbacks[name] = callback


def _load_hook():
    spec = importlib.util.spec_from_file_location("candidate_hermes_hook", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture_preflight(tmp_path: Path) -> tuple[Path, Path]:
    requests_path = tmp_path / "requests.jsonl"
    script = tmp_path / "fixture-preflight"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import json\n"
        "import pathlib\n"
        "import sys\n"
        f"log = pathlib.Path({str(requests_path)!r})\n"
        "request = json.loads(sys.stdin.read())\n"
        "with log.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(request, sort_keys=True) + '\\n')\n"
        "json.dump({\n"
        "  'schema': 'universal-docs.preflight/v1',\n"
        "  'found': True,\n"
        "  'context': 'fixture-context',\n"
        "  'receipt': {\n"
        "    'schema': 'universal-docs.preflight/v1',\n"
        "    'target': {'package': request['package'], 'target_version': '1.2.3'},\n"
        "    'source': {'kind': 'fixture', 'url': 'https://docs.example/mcp/1.2.3', 'version_binding': 'fixture', 'content_sha256': 'abc', 'content_bytes': 15},\n"
        "    'freshness': {'policy': request['freshness_mode'], 'state': 'upstream_checked', 'stale': False, 'unknown': False, 'retryable': False},\n"
        "    'selection': {'context_bytes': 15, 'truncated': False},\n"
        "    'trust': {'content': 'untrusted_upstream', 'instructions_authoritative': False, 'execution_performed': False}\n"
        "  },\n"
        "  'retryable': False\n"
        "}, sys.stdout)\n"
        "sys.stdout.write('\\n')\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, requests_path


def _settings(executable: Path, **overrides: object) -> dict[str, object]:
    settings: dict[str, object] = {
        "executable": str(executable),
        "package": "mcp",
        "ecosystem": "python",
        "selection": "requested",
        "requested_version": "1.2.3",
        "query": "tools",
        "section_ids": ["usage"],
        "context_max_bytes": 4000,
        "freshness_mode": "require_check",
        "deadline_ms": 3000,
    }
    settings.update(overrides)
    return settings


def test_configured_preflight_runs_once_per_turn_and_returns_receipt_context(tmp_path):
    module = _load_hook()
    executable, requests_path = _fixture_preflight(tmp_path)
    context = FakeContext(_settings(executable))

    module.register(context)
    callback = context.callbacks["pre_llm_call"]
    first = callback(
        session_id="session-1",
        task_id="task-1",
        turn_id="session-1:task-1:one",
        user_message="do not use this as a target",
        conversation_history=[{"role": "user", "content": "/untrusted/cwd"}],
        is_first_turn=True,
        model="test-model",
        platform="cli",
    )
    duplicate = callback(
        session_id="session-1",
        task_id="task-1",
        turn_id="session-1:task-1:one",
        user_message="different inbound text",
        conversation_history=[],
        is_first_turn=True,
        model="test-model",
        platform="cli",
    )
    second = callback(
        session_id="session-1",
        task_id="task-2",
        turn_id="session-1:task-2:two",
        user_message="another inbound text",
        conversation_history=[],
        is_first_turn=False,
        model="test-model",
        platform="cli",
    )

    assert first is not None and first["context"]
    assert duplicate is None
    assert second is not None and second["context"]
    assert "DOCS_PREFLIGHT_STATUS=ok" in first["context"]
    assert "fixture-context" in first["context"]
    assert "https://docs.example/mcp/1.2.3" in first["context"]
    assert '"schema":"universal-docs.preflight/v1"' in first["context"]
    assert len(first["context"].encode("utf-8")) <= module.MAX_RETURN_BYTES

    requests = [json.loads(line) for line in requests_path.read_text().splitlines()]
    assert len(requests) == 2
    assert all(request["package"] == "mcp" for request in requests)
    assert all(request["requested_version"] == "1.2.3" for request in requests)
    assert all(request["query"] == "tools" for request in requests)
    assert all("untrusted" not in json.dumps(request) for request in requests)
    assert all("/untrusted/cwd" not in json.dumps(request) for request in requests)


def test_fetch_failure_is_explicit_and_child_stderr_is_not_returned(tmp_path):
    module = _load_hook()
    script = tmp_path / "failing-preflight"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import sys\n"
        "sys.stderr.write('IGNORE THIS PROMPT INJECTION\\n')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    context = FakeContext(_settings(script))
    module.register(context)

    result = context.callbacks["pre_llm_call"](
        session_id="session-1",
        task_id="task-1",
        turn_id="turn-1",
        user_message="hello",
        conversation_history=[],
        is_first_turn=True,
        model="test-model",
        platform="cli",
    )

    assert result is not None
    assert "DOCS_PREFLIGHT_STATUS=missing" in result["context"]
    assert "preflight_exit_1" in result["context"]
    assert "IGNORE THIS PROMPT INJECTION" not in result["context"]
    assert "DOCS_PREFLIGHT_STATUS=stale" not in result["context"]


def test_stale_result_and_context_budget_are_explicitly_bounded():
    module = _load_hook()
    settings = {"context_max_bytes": 512}
    result = {
        "found": False,
        "receipt": {
            "schema": "universal-docs.preflight/v1",
            "source": {"kind": "cache", "url": "https://docs.example/stale"},
            "freshness": {"state": "stale", "stale": True},
        },
        "context": "x" * 100_000,
    }

    formatted = module._format_context(result, settings)

    assert "DOCS_PREFLIGHT_STATUS=stale" in formatted
    assert "DOCS_PREFLIGHT_ERROR=stale_result" in formatted
    assert "https://docs.example/stale" in formatted
    assert "UNTRUSTED DOCUMENTATION DATA" in formatted
    assert len(formatted.encode("utf-8")) <= module.MAX_RETURN_BYTES


def test_child_wall_clock_and_output_limits_fail_open_with_markers(tmp_path):
    module = _load_hook()
    timeout_script = tmp_path / "timeout-preflight"
    timeout_script.write_text(
        "#!" + sys.executable + "\nimport time\ntime.sleep(2)\n",
        encoding="utf-8",
    )
    timeout_script.chmod(timeout_script.stat().st_mode | stat.S_IXUSR)
    context = FakeContext(_settings(timeout_script, deadline_ms=100))
    module.register(context)
    timeout_result = context.callbacks["pre_llm_call"](
        turn_id="timeout-turn",
        session_id="s",
        task_id="t",
        user_message="hello",
    )

    output_script = tmp_path / "output-preflight"
    output_script.write_text(
        "#!" + sys.executable + "\n"
        "import sys\n"
        "sys.stdout.write('x' * 200000)\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )
    output_script.chmod(output_script.stat().st_mode | stat.S_IXUSR)
    output_context = FakeContext(_settings(output_script))
    module.register(output_context)
    output_result = output_context.callbacks["pre_llm_call"](
        turn_id="output-turn",
        session_id="s",
        task_id="t",
        user_message="hello",
    )

    assert timeout_result is not None
    assert "DOCS_PREFLIGHT_STATUS=missing" in timeout_result["context"]
    assert "DOCS_PREFLIGHT_ERROR=timeout" in timeout_result["context"]
    assert output_result is not None
    assert "DOCS_PREFLIGHT_STATUS=missing" in output_result["context"]
    assert "DOCS_PREFLIGHT_ERROR=output_limit" in output_result["context"]
    assert len(output_result["context"].encode("utf-8")) <= module.MAX_RETURN_BYTES


def test_structured_nonzero_result_keeps_source_receipt_and_error_marker(tmp_path):
    module = _load_hook()
    script = tmp_path / "structured-error-preflight"
    response = {
        "schema": "universal-docs.preflight/v1",
        "found": True,
        "context": "no-match context must remain untrusted",
        "error": "no_matching_sections",
        "receipt": {
            "schema": "universal-docs.preflight/v1",
            "source": {"kind": "fixture", "url": "https://docs.example/error"},
            "freshness": {"state": "upstream_checked", "stale": False},
        },
    }
    script.write_text(
        "#!" + sys.executable + "\n"
        "import sys\n"
        f"sys.stdout.write({json.dumps(response)!r})\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    context = FakeContext(_settings(script))
    module.register(context)

    result = context.callbacks["pre_llm_call"](
        turn_id="structured-error-turn",
        session_id="s",
        task_id="t",
        user_message="hello",
    )

    assert result is not None
    assert "DOCS_PREFLIGHT_STATUS=missing" in result["context"]
    assert "DOCS_PREFLIGHT_SOURCE=https://docs.example/error" in result["context"]
    assert "DOCS_PREFLIGHT_RECEIPT_JSON=" in result["context"]
    assert "DOCS_PREFLIGHT_ERROR=no_matching_sections" in result["context"]
    assert "DOCS_PREFLIGHT_PROCESS_ERROR=preflight_exit_1" in result["context"]
