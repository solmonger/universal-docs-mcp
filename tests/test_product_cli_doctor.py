from __future__ import annotations

import io
import json
from pathlib import Path

from universal_docs_mcp import product_cli


def _fixture(tmp_path: Path, monkeypatch, *, packet: str | None = None):
    root = tmp_path / "project"
    (root / ".universal-docs").mkdir(parents=True)
    (root / ".claude").mkdir()
    hook = tmp_path / "hook"
    preflight = tmp_path / "preflight"
    packet = packet or (
        '--- BEGIN UNTRUSTED DOCUMENTATION DATA ---\n'
        'UNIVERSAL-DOCS PREFLIGHT CONTEXT PACKET v1\n'
        'Target package: "demo"\nTarget version: "1.0.1"\n'
        'Source version binding: "registry_version"\nSource SHA-256: ' + 'a' * 64 + '\n'
        'Freshness policy: "require_check"\nFreshness state: "upstream_checked"\n'
        '--- END UNTRUSTED DOCUMENTATION DATA ---\n'
    )
    output = tmp_path / "output.json"
    output.write_text(json.dumps({"hookSpecificOutput": {"additionalContext": packet}}))
    hook.write_text("#!/bin/sh\ncat " + str(output) + "\n")
    preflight.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o700)
    preflight.chmod(0o700)
    monkeypatch.setenv("UNIVERSAL_DOCS_INIT_UNIVERSAL_DOCS_COMMAND_HOOK", str(hook))
    monkeypatch.setenv("UNIVERSAL_DOCS_INIT_UNIVERSAL_DOCS_PREFLIGHT", str(preflight))
    monkeypatch.setenv("UNIVERSAL_DOCS_CACHE_DIR", str(root / ".universal-docs" / "cache"))
    adapter = {"preflight_command": [str(preflight)], "request": {"package": "demo", "ecosystem": "python", "selection": "requested", "requested_version": "1.0.1", "section_ids": [], "context_max_bytes": 4096, "freshness_mode": "require_check", "deadline_ms": 30000}, "timeout_ms": 30000}
    (root / ".universal-docs/adapter.json").write_text(json.dumps(adapter))
    command = product_cli._hook_command(hook, root / ".universal-docs/adapter.json")
    (root / ".claude/settings.json").write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": command}]}]}}))
    return root


def call(root: Path):
    out = io.BytesIO()
    rc = product_cli.main(["doctor", "--project-root", str(root)], stdout=out)
    return rc, json.loads(out.getvalue())


def test_clean_fixture_probe_is_source_bearing_and_omits_body(tmp_path, monkeypatch):
    root = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(product_cli, "_doctor_installation", lambda: {"status": "pass", "reason": "identity_match", "module_path": "/installed/universal_docs_mcp/__init__.py", "installed_version": "0.4.0rc2", "executable": "/installed/bin/python"})
    rc, receipt = call(root)
    assert rc == 0
    assert receipt["schema"] == "universal-docs.doctor/v1"
    assert receipt["status"] == "pass"
    assert receipt["source_probe"]["source_body_omitted"] is True
    assert "documentation" not in json.dumps(receipt)
    assert all(set(check) == {"id", "status", "reason", "metadata"} for check in receipt["checks"])


def test_malformed_settings_fails_closed_and_aggregates_status(tmp_path, monkeypatch):
    root = _fixture(tmp_path, monkeypatch)
    (root / ".claude/settings.json").write_text('{"hooks": {}, "hooks": {}}')
    rc, receipt = call(root)
    assert rc == 1
    assert receipt["status"] == "fail"
    assert receipt["checks"][2]["status"] == "fail"
    assert receipt["rollback"]["command"].startswith("universal-docs rollback")


def test_init_state_is_consumable_without_hand_edits(tmp_path, monkeypatch):
    root = tmp_path / "joined"
    (root / "before").mkdir(parents=True)
    (root / "after").mkdir()
    (root / "before/requirements.txt").write_text("demo==1.0.0\n")
    (root / "after/requirements.txt").write_text("demo==1.0.1\n")
    hook = tmp_path / "hook"
    preflight = tmp_path / "preflight"
    hook.write_text("#!/bin/sh\nexit 0\n")
    preflight.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o700)
    preflight.chmod(0o700)
    monkeypatch.setenv("UNIVERSAL_DOCS_INIT_UNIVERSAL_DOCS_COMMAND_HOOK", str(hook))
    monkeypatch.setenv("UNIVERSAL_DOCS_INIT_UNIVERSAL_DOCS_PREFLIGHT", str(preflight))
    out = io.BytesIO()
    assert product_cli.main(["init", "--harness", "claude-code", "--project-root", str(root), "--before", "before/requirements.txt", "--after", "after/requirements.txt", "--apply"], stdout=out) == 0
    assert (root / ".universal-docs/adapter.json").is_file()
    assert (root / ".claude/settings.json").is_file()
