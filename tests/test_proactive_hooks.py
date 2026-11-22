"""RED→GREEN contract tests for proactive hook delivery."""

from __future__ import annotations

import io
import json

from tests.test_command_hook import preflight_success, request_data
from universal_docs_mcp.command_hook import main
from universal_docs_mcp.context_delivery import build_bundle_packet
from universal_docs_mcp.preflight import PreflightRequest


def test_bundle_packet_keeps_each_receipt_and_bounds_utf8():
    from universal_docs_mcp.context_delivery import build_context_packet

    packets = []
    for name, digest in (("pydantic", "a"), ("httpx", "b")):
        request = PreflightRequest.model_validate(
            {
                **request_data(),
                "package": name,
                "requested_version": "1.2.3",
                "context_max_bytes": 7000,
            }
        )
        result = preflight_success(context=f"{name} symbols " + "x" * 6000)
        result["receipt"]["target"]["package"] = name
        result["receipt"]["selection"]["budget_bytes"] = 7000
        result["receipt"]["selection"]["context_bytes"] = len(
            result["context"].encode()
        )
        result["receipt"]["source"]["url"] = f"https://pypi.org/pypi/{name}/1.2.3/json"
        result["receipt"]["source"]["content_sha256"] = digest * 64
        from universal_docs_mcp.context_integrity import context_integrity

        result["receipt"]["integrity"] = context_integrity(
            result["context"], result["receipt"]
        )
        packets.append(build_context_packet(request, result))
    packet = build_bundle_packet(packets)
    assert len(packet.encode()) <= 8192
    assert "UDCTX:" + "a" * 16 in packet
    assert "UDCTX:" + "b" * 16 in packet
    assert packet.count("BEGIN UNTRUSTED DOCUMENTATION DATA") == 2


def test_dynamic_command_hook_injects_for_technical_prompt(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "requirements.txt").write_text("click==8.1.7\n")
    (root / "app.py").write_text("import click\nclick.echo('hi')\n")
    executable = tmp_path / "preflight"
    executable.write_text(
        "#!" + __import__("sys").executable + "\n"
        "import json,sys\nrequest=json.load(sys.stdin)\n"
        "print(json.dumps({'schema':'universal-docs.preflight/v1','found':False,'error':'package_not_found'}))\n"
    )
    executable.chmod(0o755)
    output = io.StringIO()
    main(
        ["--harness", "claude", "--dynamic", "--preflight-executable", str(executable)],
        stdin=io.BytesIO(
            json.dumps({"prompt": "fix click bug", "cwd": str(root)}).encode()
        ),
        stdout=output,
    )
    assert json.loads(output.getvalue()) == {}


def test_dynamic_command_hook_delivers_exact_version_context(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace

    import universal_docs_mcp.command_hook as command_hook
    from universal_docs_mcp.context_integrity import context_integrity

    root = tmp_path / "project"
    root.mkdir()
    (root / "requirements.txt").write_text("click==8.1.7\n")
    (root / "app.py").write_text("import click\nclick.echo('hi')\n")
    executable = tmp_path / "preflight"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    result = preflight_success(context="click echo docs")
    result["receipt"]["target"].update(
        package="click", target_version="8.1.7", requested_version="8.1.7"
    )
    result["receipt"]["source"].update(url="https://pypi.org/pypi/click/8.1.7/json")
    result["receipt"]["freshness"].update(
        policy="allow_cache",
        state="cache_hit",
        fetched_at=time.time(),
        checked_at=None,
        age_seconds=0.0,
        cached=True,
    )
    result["receipt"]["selection"].update(
        context_bytes=len(result["context"].encode()), budget_bytes=6144
    )
    result["receipt"]["integrity"] = context_integrity(
        result["context"], result["receipt"]
    )
    monkeypatch.setattr(
        command_hook,
        "_invoke_preflight",
        lambda _config: SimpleNamespace(
            status="completed", returncode=0, stdout=b"{}", stderr=b""
        ),
    )
    monkeypatch.setattr(command_hook, "_parse_preflight_output", lambda _raw: result)
    output = io.StringIO()
    main(
        ["--harness", "claude", "--dynamic", "--preflight-executable", str(executable)],
        stdin=io.BytesIO(
            json.dumps({"prompt": "fix click bug", "cwd": str(root)}).encode()
        ),
        stdout=output,
    )
    payload = json.loads(output.getvalue())
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert 'Target version: "8.1.7"' in context
    assert "UDCTX:" in context


def test_dynamic_command_hook_fails_open_for_nontechnical_prompt(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    output = io.StringIO()
    main(
        [
            "--harness",
            "codex",
            "--dynamic",
            "--preflight-executable",
            str(tmp_path / "unused"),
        ],
        stdin=io.BytesIO(
            json.dumps({"prompt": "weather and news", "cwd": str(root)}).encode()
        ),
        stdout=output,
    )
    assert json.loads(output.getvalue()) == {}


def test_hermes_trigger_accepts_dependency_name_and_rejects_unrelated_turn(tmp_path):
    from tests.hermes_hook_support import load_plugin

    root = tmp_path / "project"
    root.mkdir()
    (root / "requirements.txt").write_text("pydantic==2.9.2\n")
    module = load_plugin()
    assert module._is_eligible_turn("fix the pydantic validator", root)
    assert module._is_eligible_turn("why is pydantic timing out?", root)
    assert not module._is_eligible_turn("summarize today's weather and news", root)


def test_frenchbot_is_disabled_even_without_explicit_opt_out(tmp_path):
    from tests.hermes_hook_support import Context, load_plugin

    profile = tmp_path / ".hermes" / "profiles" / "frenchbot"
    profile.mkdir(parents=True)
    plugin = load_plugin(profile)
    executable = tmp_path / "unused"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    ctx = Context({"mode": "native", "executable": str(executable)})
    plugin.register(ctx)
    assert ctx.hooks == {}


def test_hermes_error_hook_fetches_once_per_session_error_signature(
    tmp_path, monkeypatch
):
    from tests.hermes_hook_support import load_plugin
    from universal_docs_mcp.planner import Plan, PlanBundle

    module = load_plugin(tmp_path / "profile")
    plan = Plan(
        "selected",
        "error_package_matched",
        "click",
        "python",
        None,
        "8.1.7",
        "requirements",
        "click 8.1.7 AttributeError echo",
        {"mode": "error", "symbols": ["echo"]},
    )
    bundle = PlanBundle("selected", "error_package_matched", (plan,), True)
    monkeypatch.setattr(
        module, "_session_state", lambda _session: (tmp_path, tmp_path, "test")
    )
    monkeypatch.setattr(
        "universal_docs_mcp.planner.select_for_error", lambda *_a, **_kw: bundle
    )
    monkeypatch.setattr(
        module,
        "_read_child",
        lambda _settings: (
            b'{"schema":"universal-docs.context/v1","status":"prepared","context":"Receipt: UDCTX:fixture\\nTarget version: 8.1.7\\nSource URL: https://fixture.invalid/click-8.1.7\\nSource SHA-256: abc\\nDOCS","error":null}',
            None,
        ),
    )
    monkeypatch.setattr(module, "_cache_evidence", lambda *_a: {"cache_key": "fixture"})
    settings = {"timeout_ms": 1000, "cache_dir": tmp_path}
    seen = set()
    event = {
        "session_id": "s1",
        "tool_output": "AttributeError: module click has no attribute echo",
    }
    first = module._post_tool_callback(settings, event, seen)
    second = module._post_tool_callback(settings, event, seen)
    assert first is not None and "Target version: 8.1.7" in first["context"]
    assert second is None


def test_hermes_local_stub_fallback_delivers_when_upstream_misses(
    tmp_path, monkeypatch
):
    from tests.hermes_hook_support import load_plugin

    root = tmp_path / "project"
    stub_dir = root / ".venv/lib/python3.14/site-packages/demo"
    stub_dir.mkdir(parents=True)
    (root / "requirements.txt").write_text("demo==1.2.3\n")
    (root / "app.py").write_text("from demo import Client\nClient()\n")
    (stub_dir / "__init__.pyi").write_text(
        'class Client:\n    """Demo client."""\n    def run(self) -> int: ...\n'
    )
    profile = tmp_path / "profile"
    profile.mkdir()
    module = load_plugin(profile)
    monkeypatch.setattr(module, "_session_state", lambda _s: (root, root, "test"))
    monkeypatch.setattr(module, "_read_child", lambda _s: (b"", "delivery_nonzero"))
    settings = {
        "mode": "native",
        "executable": "/bin/true",
        "timeout_ms": 1000,
        "cache_dir": tmp_path / "cache",
        "project_root": None,
    }
    res = module._native_callback(
        settings, {"session_id": "s-stub", "user_message": "fix demo Client"}
    )
    assert res is not None
    assert "DOCS_PREFLIGHT_STATUS=prepared\n" in res["context"]
    assert "class Client:" in res["context"]
    assert "local_installed_artifact" in res["context"]


def test_dynamic_command_hook_accepts_timeout_and_falls_back_to_local_stub(tmp_path):
    root = tmp_path / "project"
    stub_dir = root / ".venv/lib/python3.14/site-packages/demo"
    stub_dir.mkdir(parents=True)
    (root / "requirements.txt").write_text("demo==1.2.3\n")
    (root / "app.py").write_text("from demo import Client\nClient()\n")
    (stub_dir / "__init__.pyi").write_text(
        'class Client:\n    """Demo client."""\n    def run(self) -> int: ...\n'
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    executable = tmp_path / "preflight"
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o755)
    output = io.StringIO()
    main(
        [
            "--harness",
            "claude",
            "--dynamic",
            "--preflight-executable",
            str(executable),
            "--project-root",
            str(root),
            "--timeout-ms",
            "2000",
        ],
        stdin=io.BytesIO(
            json.dumps({"prompt": "fix demo Client", "cwd": str(empty)}).encode()
        ),
        stdout=output,
    )
    payload = json.loads(output.getvalue())
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "class Client:" in context
    assert "local_installed_artifact" in context
