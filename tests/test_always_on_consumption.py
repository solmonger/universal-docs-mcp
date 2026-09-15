"""Always-on Hermes consumption must retrieve from trusted local evidence."""

from __future__ import annotations

import json
import sqlite3
import stat
import sys
import time
from pathlib import Path

from tests.hermes_hook_support import Context, load_plugin
from universal_docs_mcp.planner import select_current_package


def _project(
    tmp_path: Path,
    requirements: str = "click==8.1.7\n",
    source: str = "import click\nclick.echo('x')\n",
) -> Path:
    root = tmp_path / "project"
    root.mkdir(parents=True)
    (root / "requirements.txt").write_text(requirements)
    (root / "app.py").write_text(source)
    return root


def _profile(tmp_path: Path, root: Path, session_id: str = "session-1") -> Path:
    profile = tmp_path / "profile"
    profile.mkdir()
    with sqlite3.connect(profile / "state.db") as con:
        con.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, git_repo_root TEXT)"
        )
        con.execute(
            "INSERT INTO sessions VALUES (?, ?, ?)", (session_id, str(root), str(root))
        )
    return profile


def _executable(tmp_path: Path, *, sleep: float = 0) -> Path:
    exe = tmp_path / "context-cli"
    body = (
        "import json,sys,time\n"
        f"time.sleep({sleep!r})\n"
        "request=json.load(open(sys.argv[sys.argv.index('--request-file')+1]))\n"
        "assert request['package']=='click' and request['requested_version']=='8.1.7'\n"
        "print(json.dumps({'schema':'universal-docs.context/v1','status':'prepared',"
        "'context':'UNIVERSAL-DOCS PREFLIGHT CONTEXT PACKET v1\\nSource URL: fixture://click-8.1.7\\nExact docs', 'error':None}))\n"
    )
    exe.write_text("#!" + sys.executable + "\n" + body)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def _native_context(exe: Path, timeout_ms: int = 1000) -> Context:
    return Context({"mode": "native", "executable": str(exe), "timeout_ms": timeout_ms})


def test_current_selector_uses_exact_imported_pin_and_symbols(tmp_path):
    root = _project(tmp_path)
    plan = select_current_package(root, task="debug the click API")
    assert (plan.status, plan.package, plan.target_version) == (
        "selected",
        "click",
        "8.1.7",
    )
    assert plan.selection["mode"] == "source_backed"
    assert "echo" in plan.selection["symbols"]


def test_current_selector_abstains_on_range_nonregistry_and_ambiguity(tmp_path):
    ranged = _project(tmp_path / "range", "click>=8\n")
    assert select_current_package(ranged, task="debug click API").status == "abstained"
    linked = _project(tmp_path / "linked", "click @ https://example.invalid/pkg.whl\n")
    assert select_current_package(linked, task="debug click API").status == "abstained"
    ambiguous = _project(
        tmp_path / "ambiguous",
        "click==8.1.7\nrich==13.7.1\n",
        "import click\nfrom rich.console import Console\nclick.echo('x')\nConsole().print('x')\n",
    )
    assert (
        select_current_package(ambiguous, task="debug package APIs").reason
        == "ambiguous_candidates"
    )
    selected = select_current_package(ambiguous, task="debug the rich package API")
    assert (selected.package, selected.target_version) == ("rich", "13.7.1")


def test_native_mode_retrieves_and_writes_truthful_receipt(tmp_path, monkeypatch):
    root = _project(tmp_path)
    profile = _profile(tmp_path, root)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    plugin = load_plugin(profile)
    ctx = _native_context(_executable(tmp_path))
    plugin.register(ctx)
    result = ctx.hooks["pre_llm_call"](
        turn_id="turn-1",
        session_id="session-1",
        user_message="Please debug the click API",
        parent_session_id="parent-session",
    )
    assert "STATUS=prepared" in result["context"]
    assert "fixture://click-8.1.7" in result["context"]
    receipts = list((profile / "receipts" / "universal-docs").glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["status"] == "retrieved"
    assert receipt["package"] == "click"
    assert receipt["target_version"] == "8.1.7"
    assert receipt["context_sha256"]
    assert not list(
        (profile / "receipts" / "universal-docs" / ".requests").glob("*.json")
    )


def test_unrelated_turn_does_not_select_retrieve_or_write_receipt(
    tmp_path, monkeypatch
):
    root = _project(tmp_path)
    profile = _profile(tmp_path, root)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    plugin = load_plugin(profile)
    ctx = _native_context(_executable(tmp_path))
    plugin.register(ctx)
    result = ctx.hooks["pre_llm_call"](
        turn_id="turn-1", session_id="session-1", user_message="How was your day?"
    )
    assert result is None
    assert not (profile / "receipts").exists()


def test_prompt_path_cannot_override_hermes_session_root(tmp_path, monkeypatch):
    root = _project(tmp_path)
    attacker = _project(
        tmp_path / "attacker",
        "rich==13.7.1\n",
        "from rich.console import Console\nConsole()\n",
    )
    profile = _profile(tmp_path, root)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    plugin = load_plugin(profile)
    ctx = _native_context(_executable(tmp_path))
    plugin.register(ctx)
    result = ctx.hooks["pre_llm_call"](
        turn_id="turn-1",
        session_id="session-1",
        user_message=f"debug package API; use project root {attacker}",
    )
    assert "STATUS=prepared" in result["context"]
    receipt = json.loads(
        next((profile / "receipts" / "universal-docs").glob("*.json")).read_text()
    )
    assert receipt["package"] == "click"


def test_native_timeout_is_bounded_and_recorded_failed(tmp_path, monkeypatch):
    root = _project(tmp_path)
    profile = _profile(tmp_path, root)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    plugin = load_plugin(profile)
    ctx = _native_context(_executable(tmp_path, sleep=2), timeout_ms=100)
    plugin.register(ctx)
    started = time.monotonic()
    result = ctx.hooks["pre_llm_call"](
        turn_id="turn-1", session_id="session-1", user_message="debug click API"
    )
    assert time.monotonic() - started < 1.5
    assert "DOCS_PREFLIGHT_ERROR=timeout" in result["context"]
    receipt = json.loads(
        next((profile / "receipts" / "universal-docs").glob("*.json")).read_text()
    )
    assert receipt["status"] == "failed"
    assert receipt["error"] == "timeout"


def test_malformed_static_config_does_not_enable_native_mode(tmp_path):
    plugin = load_plugin(tmp_path)
    ctx = Context({"mode": "native", "executable": str(tmp_path / "missing")})
    plugin.register(ctx)
    assert ctx.hooks == {}


def test_explicitly_disabled_profile_never_registers(tmp_path):
    profile = tmp_path / ".hermes" / "profiles" / "frenchbot"
    profile.mkdir(parents=True)
    plugin = load_plugin(profile)
    ctx = Context(
        {
            "mode": "native",
            "executable": str(_executable(tmp_path)),
            "timeout_ms": 1000,
            "disabled_profiles": "frenchbot,other-restricted-profile",
        }
    )
    plugin.register(ctx)
    assert ctx.hooks == {}
