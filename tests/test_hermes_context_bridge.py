"""The Hermes plugin consumes one neutral, validated delivery frame."""

import json

from tests.hermes_hook_support import load_plugin, make_context


def test_hermes_bridge_uses_fixed_request_file_and_neutral_frame(tmp_path, monkeypatch):
    plugin = load_plugin(profile_home=tmp_path / "profile")
    ctx, log = make_context(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("BRIDGE_TEST_SECRET", "must-not-cross")
    plugin.register(ctx)
    result = ctx.hooks["pre_llm_call"](
        turn_id="turn-1",
        user_text="private prompt",
        cwd="/untrusted",
        executable="/untrusted",
    )
    assert "DOCS_PREFLIGHT_STATUS=prepared" in result["context"]
    assert "Fixture documentation" in result["context"]
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 1
    assert calls[0]["argv"] == ["--request-file", ctx.settings["request_file"]]
    assert "BRIDGE_TEST_SECRET" not in calls[0]["env"]
    assert calls[0]["env"]["UNIVERSAL_DOCS_CACHE_DIR"].startswith(
        str(tmp_path / "profile")
    )
    assert "private prompt" not in json.dumps(calls)
    assert ctx.hooks["pre_llm_call"](turn_id="turn-1") is None


def test_cache_scope_comes_from_hermes_context_local_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "process-default"))
    selected = tmp_path / "context-local-scope"
    module = load_plugin(profile_home=selected)
    assert module._environment()["UNIVERSAL_DOCS_CACHE_DIR"] == str(
        selected / "cache/universal-docs-preflight"
    )
