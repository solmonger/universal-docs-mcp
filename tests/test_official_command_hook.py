"""Every delivery entry point consumes the same official/package request parser."""

import json

from universal_docs_mcp.command_hook import load_request


def test_command_hook_accepts_the_official_preflight_contract(tmp_path):
    value = {
        "source_id": "mcp-tools",
        "selection": "requested",
        "requested_version": "2026-07-28",
        "query": "tools/list",
        "freshness_mode": "require_check",
        "context_max_bytes": 4000,
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(value))
    parsed = load_request(path)
    assert parsed.source_id == "mcp-tools"
    assert parsed.requested_version == "2026-07-28"
    assert not hasattr(parsed, "package")
