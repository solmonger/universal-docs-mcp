from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

from universal_docs_mcp import product_cli
from universal_docs_mcp.context_integrity import context_integrity


def response(package="demo", version="1.0.1"):
    context = "Real fixture documentation."
    now = time.time()
    receipt = {"schema": "universal-docs.preflight/v1", "target": {"package": package, "ecosystem": "python", "selection": "requested", "target_version": version, "requested_version": version, "installed_version": None, "installed_resolution": "unknown", "latest_observed": None}, "source": {"kind": "pypi_description", "url": f"https://pypi.org/pypi/{package}/{version}/json", "version_binding": "registry_version", "content_sha256": "a" * 64, "content_bytes": len(context.encode())}, "freshness": {"policy": "require_check", "state": "upstream_checked", "fetched_at": now - 1, "checked_at": now, "latest_checked_at": None, "age_seconds": 1.0, "cached": False, "stale": False, "unknown": False, "stale_reason": None, "retryable": False}, "selection": {"query": None, "requested_sections": [], "matched_sections": [], "matches": [], "no_match": False, "context_bytes": len(context.encode()), "budget_bytes": 4096}, "trust": {"content": "untrusted_upstream", "instructions_authoritative": False, "execution_performed": False}}
    receipt["integrity"] = context_integrity(context, receipt)
    return {"schema": "universal-docs.preflight/v1", "found": True, "context": context, "receipt": receipt, "retryable": False}


def fixture(tmp_path: Path, monkeypatch, *, package="demo", version="1.0.1") -> Path:
    root = tmp_path / "project"
    (root / "before").mkdir(parents=True)
    (root / "after").mkdir()
    (root / "before/requirements.txt").write_text("demo==1.0.0\n")
    (root / "after/requirements.txt").write_text(f"{package}=={version}\n")
    (root / ".universal-docs" / "cache").mkdir(parents=True)
    (root / ".claude").mkdir()
    (root / ".claude/settings.json").write_text(json.dumps({"unrelated": {"keep": True}}))
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps(response(package, version)))
    preflight = tmp_path / "preflight"
    preflight.write_text(f"#!/bin/sh\ncat {answer}\n")
    hook = tmp_path / "hook"
    hook.write_text(f"#!{sys.executable}\nimport sys\nfrom universal_docs_mcp.command_hook import main\nsys.exit(main())\n")
    for path in (hook, preflight):
        path.chmod(0o700)
    monkeypatch.setenv("UNIVERSAL_DOCS_INIT_UNIVERSAL_DOCS_COMMAND_HOOK", str(hook))
    monkeypatch.setenv("UNIVERSAL_DOCS_INIT_UNIVERSAL_DOCS_PREFLIGHT", str(preflight))
    monkeypatch.setenv("UNIVERSAL_DOCS_CACHE_DIR", str(root / ".universal-docs" / "cache"))
    out = io.BytesIO()
    assert product_cli.main(["init", "--harness", "claude-code", "--project-root", str(root), "--before", "before/requirements.txt", "--after", "after/requirements.txt", "--apply"], stdout=out) == 0
    return root


def call(root: Path):
    out = io.BytesIO()
    rc = product_cli.main(["doctor", "--project-root", str(root)], stdout=out)
    return rc, json.loads(out.getvalue())


def test_real_hook_source_bearing_fixture_omits_body(tmp_path, monkeypatch):
    root = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(product_cli, "_doctor_installation", lambda: {"status": "pass", "reason": "identity_match", "module_path": "/installed/universal_docs_mcp/__init__.py", "installed_version": "0.4.0rc2", "executable": "/installed/bin/python"})
    rc, receipt = call(root)
    assert rc == 0
    assert receipt["source_probe"]["probe_mode"] == "fixture"
    assert receipt["source_probe"]["source_body_omitted"] is True
    assert "Real fixture documentation" not in json.dumps(receipt)


def test_doctor_root_failures_are_doctor_receipts(tmp_path):
    for root in ("relative", str(tmp_path / "missing")):
        out = io.BytesIO()
        assert product_cli.main(["doctor", "--project-root", root], stdout=out) == 1
        payload = json.loads(out.getvalue())
        assert payload["schema"] == "universal-docs.doctor/v1" and payload["status"] == "fail"


def test_init_then_doctor_consumes_real_state(tmp_path, monkeypatch):
    root = tmp_path / "joined"
    (root / "before").mkdir(parents=True)
    (root / "after").mkdir()
    (root / "before/requirements.txt").write_text("demo==1.0.0\n")
    (root / "after/requirements.txt").write_text("demo==1.0.1\n")
    fixture(root.parent, monkeypatch)  # establishes executable seams in the parent temp dir
    # init's paths are explicit and the generated state is then read by doctor.
    out = io.BytesIO()
    assert product_cli.main(["init", "--harness", "claude-code", "--project-root", str(root), "--before", "before/requirements.txt", "--after", "after/requirements.txt", "--apply"], stdout=out) == 0
    monkeypatch.setenv("UNIVERSAL_DOCS_CACHE_DIR", str(root / ".universal-docs" / "cache"))
    (root / ".universal-docs" / "cache").mkdir(parents=True)
    monkeypatch.setattr(product_cli, "_doctor_installation", lambda: {"status": "pass", "reason": "identity_match", "module_path": "/installed/universal_docs_mcp/__init__.py", "installed_version": "0.4.0rc2", "executable": "/installed/bin/python"})
    rc, receipt = call(root)
    assert rc == 0
    assert receipt["schema"] == "universal-docs.doctor/v1"


def test_rollback_command_dry_run_and_apply_preserves_unrelated(tmp_path, monkeypatch):
    root = fixture(tmp_path, monkeypatch)
    out = io.BytesIO()
    assert product_cli.main(["rollback", "--project-root", str(root)], stdout=out) == 0
    assert json.loads(out.getvalue())["schema"] == "universal-docs.rollback/v1"
    before = json.loads((root / ".claude/settings.json").read_text())
    out = io.BytesIO()
    assert product_cli.main(["rollback", "--project-root", str(root), "--apply"], stdout=out) == 0
    assert json.loads((root / ".claude/settings.json").read_text())["unrelated"] == before["unrelated"]
    assert not (root / ".universal-docs/adapter.json").exists()


def test_malformed_adapter_and_conflicting_hook_fail_closed(tmp_path, monkeypatch):
    root = fixture(tmp_path, monkeypatch)
    (root / ".universal-docs/adapter.json").write_text('{"preflight_command": []}')
    rc, receipt = call(root)
    assert rc == 1 and receipt["status"] == "fail"
