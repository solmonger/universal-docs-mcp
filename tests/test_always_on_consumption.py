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
        "import json,os,sqlite3,sys,time\n"
        f"time.sleep({sleep!r})\n"
        "request=json.load(open(sys.argv[sys.argv.index('--request-file')+1]))\n"
        "assert request['package']=='click' and request['requested_version']=='8.1.7'\n"
        "cache=os.environ['UNIVERSAL_DOCS_CACHE_DIR']; os.makedirs(cache,exist_ok=True)\n"
        "value=json.dumps({'content':'fixture docs','source':'fixture','source_url':'https://fixture.invalid/click-8.1.7','fetched_at':time.time(),'version':'8.1.7'})\n"
        "con=sqlite3.connect(os.path.join(cache,'cache.db'))\n"
        "con.execute('CREATE TABLE IF NOT EXISTS docs_cache (key TEXT PRIMARY KEY,value TEXT NOT NULL,fetched_at REAL NOT NULL,value_bytes INTEGER NOT NULL DEFAULT 0)')\n"
        "con.execute('INSERT OR REPLACE INTO docs_cache VALUES (?,?,?,?)',('docrequest-v4:python:click:8.1.7',value,time.time(),len(value.encode())))\n"
        "con.commit(); con.close()\n"
        "print(json.dumps({'schema':'universal-docs.context/v1','status':'prepared',"
        "'context':'UNIVERSAL-DOCS PREFLIGHT CONTEXT PACKET v1\\nSource URL: https://fixture.invalid/click-8.1.7\\nExact docs', 'error':None}))\n"
    )
    exe.write_text("#!" + sys.executable + "\n" + body)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def _native_context(exe: Path, timeout_ms: int = 1000) -> Context:
    return Context({"mode": "native", "executable": str(exe), "timeout_ms": timeout_ms})


def _frame_only_executable(tmp_path: Path) -> Path:
    exe = tmp_path / "frame-only-cli"
    exe.write_text(
        "#!" + sys.executable + "\n"
        "import json\n"
        "print(json.dumps({'schema':'universal-docs.context/v1','status':'prepared',"
        "'context':'UNIVERSAL-DOCS PREFLIGHT CONTEXT PACKET v1\\nSource URL: https://fixture.invalid/click-8.1.7\\nFake docs','error':None}))\n"
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


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
    assert "https://fixture.invalid/click-8.1.7" in result["context"]
    receipts = list((profile / "receipts" / "universal-docs").glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["status"] == "retrieved"
    assert receipt["package"] == "click"
    assert receipt["target_version"] == "8.1.7"
    assert receipt["context_sha256"]
    assert receipt["cache_evidence"]["cache_key"] == "docrequest-v4:python:click:8.1.7"
    assert (
        receipt["cache_evidence"]["source_url"] == "https://fixture.invalid/click-8.1.7"
    )
    assert not list(
        (profile / "receipts" / "universal-docs" / ".requests").glob("*.json")
    )


def test_prepared_frame_without_exact_cache_evidence_fails_closed(
    tmp_path, monkeypatch
):
    root = _project(tmp_path)
    profile = _profile(tmp_path, root)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    plugin = load_plugin(profile)
    ctx = _native_context(_frame_only_executable(tmp_path))
    plugin.register(ctx)
    result = ctx.hooks["pre_llm_call"](
        turn_id="turn-1", session_id="session-1", user_message="debug click API"
    )
    assert "STATUS=missing" in result["context"]
    receipt = json.loads(
        next((profile / "receipts" / "universal-docs").glob("*.json")).read_text()
    )
    assert receipt["status"] == "failed"
    assert receipt["error"] == "cache_evidence_missing_or_mismatched"


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


def test_native_mode_uses_terminal_cwd_when_session_row_has_blank_paths(
    tmp_path, monkeypatch
):
    root = _project(tmp_path)
    profile = tmp_path / "profile"
    profile.mkdir()
    with sqlite3.connect(profile / "state.db") as con:
        con.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, git_repo_root TEXT)"
        )
        con.execute("INSERT INTO sessions VALUES ('session-1', '', '')")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("TERMINAL_CWD", str(root))
    monkeypatch.chdir(tmp_path)
    plugin = load_plugin(profile)
    ctx = _native_context(_executable(tmp_path))
    plugin.register(ctx)
    result = ctx.hooks["pre_llm_call"](
        turn_id="turn-1", session_id="session-1", user_message="debug click API"
    )
    assert "STATUS=prepared" in result["context"]
    receipt = json.loads(
        next((profile / "receipts" / "universal-docs").glob("*.json")).read_text()
    )
    assert (receipt["package"], receipt["target_version"]) == ("click", "8.1.7")
    assert receipt["resolution"]["source"] == "terminal_cwd"


def _profile_with(
    tmp_path: Path, cwd: str, git_root: str | None, session_id: str = "session-1"
) -> Path:
    profile = tmp_path / "profile"
    profile.mkdir(exist_ok=True)
    with sqlite3.connect(profile / "state.db") as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, cwd TEXT, git_repo_root TEXT)"
        )
        con.execute(
            "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?)",
            (session_id, cwd, git_root),
        )
    return profile


def _receipt(profile: Path) -> dict:
    return json.loads(
        next((profile / "receipts" / "universal-docs").glob("*.json")).read_text()
    )


def test_native_mode_resolves_from_session_row_cwd_when_git_root_blank(
    tmp_path, monkeypatch
):
    """Hermes persists git metadata best-effort; a cwd-only row must still serve."""
    root = _project(tmp_path)
    profile = _profile_with(tmp_path, str(root), None)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    plugin = load_plugin(profile)
    ctx = _native_context(_executable(tmp_path))
    plugin.register(ctx)
    result = ctx.hooks["pre_llm_call"](
        turn_id="turn-1", session_id="session-1", user_message="debug the click API"
    )
    assert "STATUS=prepared" in result["context"]
    receipt = _receipt(profile)
    assert receipt["status"] == "retrieved"
    assert receipt["resolution"] == {
        "cwd": str(root),
        "root": str(root),
        "source": "session_row_cwd",
    }


def test_native_mode_derives_git_root_above_session_cwd(tmp_path, monkeypatch):
    root = _project(tmp_path)
    (root / ".git").mkdir()
    nested = root / "pkg"
    nested.mkdir()
    profile = _profile_with(tmp_path, str(nested), None)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    plugin = load_plugin(profile)
    ctx = _native_context(_executable(tmp_path))
    plugin.register(ctx)
    result = ctx.hooks["pre_llm_call"](
        turn_id="turn-1", session_id="session-1", user_message="debug the click API"
    )
    assert "STATUS=prepared" in result["context"]
    receipt = _receipt(profile)
    assert receipt["status"] == "retrieved"
    assert receipt["resolution"]["root"] == str(root)
    assert receipt["resolution"]["source"] == "session_row_cwd"


def test_native_mode_prefers_valid_stored_git_root(tmp_path, monkeypatch):
    root = _project(tmp_path)
    nested = root / "src"
    nested.mkdir()
    profile = _profile_with(tmp_path, str(nested), str(root))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    plugin = load_plugin(profile)
    ctx = _native_context(_executable(tmp_path))
    plugin.register(ctx)
    result = ctx.hooks["pre_llm_call"](
        turn_id="turn-1", session_id="session-1", user_message="debug the click API"
    )
    assert "STATUS=prepared" in result["context"]
    receipt = _receipt(profile)
    assert receipt["resolution"]["source"] == "session_row_git_root"
    assert receipt["resolution"]["root"] == str(root)


def test_unresolvable_session_state_writes_abstention_receipt(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    with sqlite3.connect(profile / "state.db") as con:
        con.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, git_repo_root TEXT)"
        )
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "missing-dir"))
    plugin = load_plugin(profile)
    ctx = _native_context(_executable(tmp_path))
    plugin.register(ctx)
    result = ctx.hooks["pre_llm_call"](
        turn_id="turn-1", session_id="session-1", user_message="debug click API"
    )
    assert "DOCS_PREFLIGHT_ERROR=session_state_unavailable" in result["context"]
    receipt = _receipt(profile)
    assert (receipt["status"], receipt["reason"]) == (
        "abstained",
        "session_state_unavailable",
    )
    assert receipt["resolution"] == {"cwd": None, "root": None, "source": None}


def test_current_selector_reads_single_requirements_variant(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "requirements-dev.txt").write_text("click==8.1.7\n")
    (root / "app.py").write_text("import click\nclick.echo('x')\n")
    plan = select_current_package(root, task="debug the click API")
    assert (plan.status, plan.package, plan.target_version) == (
        "selected",
        "click",
        "8.1.7",
    )
    assert plan.resolution_source == "requirements"
    assert plan.selection["mode"] == "source_backed"


def test_current_selector_keeps_canonical_precedence(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "requirements.txt").write_text("click==8.1.7\n")
    (root / "requirements-dev.txt").write_text("rich==13.7.1\n")
    (root / "app.py").write_text("import click\nclick.echo('x')\n")
    plan = select_current_package(root, task="debug the click API")
    assert (plan.status, plan.package, plan.target_version) == (
        "selected",
        "click",
        "8.1.7",
    )


def test_current_selector_abstains_on_ambiguous_requirements_variants(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "requirements-dev.txt").write_text("click==8.1.7\n")
    (root / "requirements-prod.txt").write_text("click==8.1.7\n")
    (root / "app.py").write_text("import click\nclick.echo('x')\n")
    plan = select_current_package(root, task="debug the click API")
    assert (plan.status, plan.reason) == ("abstained", "ambiguous_manifest")
