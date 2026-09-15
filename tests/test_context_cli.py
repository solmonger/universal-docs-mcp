"""Tests for the neutral, in-process context delivery boundary."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

from universal_docs_mcp import context_cli
from universal_docs_mcp.context_integrity import context_integrity
from universal_docs_mcp.preflight import PreflightRequest, build_error_response

SOURCE_HASH = "b" * 64


def request() -> PreflightRequest:
    return PreflightRequest.model_validate(
        {
            "package": "fixture-docs",
            "ecosystem": "python",
            "selection": "requested",
            "requested_version": "1.2.3",
            "query": "usage",
            "section_ids": ["usage"],
            "context_max_bytes": 2048,
            "freshness_mode": "require_check",
            "deadline_ms": 5000,
        }
    )


def successful_result(context: str = "Fixture documentation.") -> dict:
    now = time.time()
    response = {
        "schema": "universal-docs.preflight/v1",
        "found": True,
        "context": context,
        "receipt": {
            "schema": "universal-docs.preflight/v1",
            "target": {
                "package": "fixture-docs",
                "ecosystem": "python",
                "selection": "requested",
                "target_version": "1.2.3",
                "requested_version": "1.2.3",
                "installed_version": None,
                "installed_resolution": "unknown",
                "latest_observed": None,
            },
            "source": {
                "kind": "pypi_description",
                "url": "https://pypi.org/pypi/fixture-docs/1.2.3/json",
                "version_binding": "registry_version",
                "content_sha256": SOURCE_HASH,
                "content_bytes": len(context.encode()),
            },
            "freshness": {
                "policy": "require_check",
                "state": "upstream_checked",
                "fetched_at": now - 1.0,
                "checked_at": now,
                "latest_checked_at": None,
                "age_seconds": 1.0,
                "cached": False,
                "stale": False,
                "unknown": False,
                "stale_reason": None,
                "retryable": False,
            },
            "selection": {
                "query": "usage",
                "requested_sections": ["usage"],
                "matched_sections": ["usage"],
                "matches": [],
                "no_match": False,
                "sections_omitted": 0,
                "section_map": [],
                "section_map_total": 1,
                "section_map_truncated": False,
                "context_bytes": len(context.encode()),
                "budget_bytes": 2048,
                "truncated": False,
            },
            "trust": {
                "content": "untrusted_upstream",
                "instructions_authoritative": False,
                "execution_performed": False,
            },
        },
        "retryable": False,
    }
    response["receipt"]["integrity"] = context_integrity(
        response["context"], response["receipt"]
    )
    return response


def test_context_cli_uses_parse_and_run_cli_in_process(
    tmp_path: Path, monkeypatch
) -> None:
    request_path = tmp_path / "request.json"
    raw_request = b'{"request":"passed untouched to preflight"}'
    request_path.write_bytes(raw_request)
    seen: dict[str, object] = {}

    def parse(raw: bytes):
        seen["raw"] = raw
        return request()

    async def run(parsed, cache):
        seen["request"] = parsed
        seen["cache"] = cache
        return successful_result()

    monkeypatch.setattr(context_cli, "parse_request", parse)
    monkeypatch.setattr(context_cli, "_run_cli", run)
    output = io.BytesIO()

    exit_code = context_cli.main(["--request-file", str(request_path)], stdout=output)

    assert exit_code == 0
    assert seen["raw"] == raw_request
    assert seen["request"] == request()
    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert set(payload) == {"schema", "status", "context", "error"}
    assert payload["schema"] == "universal-docs.context/v1"
    assert payload["status"] == "prepared"
    assert payload["error"] is None
    assert "UDCTX:bbbbbbbbbbbbbbbb" in payload["context"]
    assert len(payload["context"].encode()) <= 8 * 1024
    assert len(lines[0]) <= 16 * 1024


def test_context_cli_reports_unavailable_without_context(
    tmp_path: Path, monkeypatch
) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_bytes(b"ignored by patched parser")

    monkeypatch.setattr(context_cli, "parse_request", lambda raw: request())

    async def run(parsed, cache):
        return build_error_response("documentation_not_found", retryable=False)

    monkeypatch.setattr(context_cli, "_run_cli", run)
    output = io.BytesIO()

    exit_code = context_cli.main(["--request-file", str(request_path)], stdout=output)
    payload = json.loads(output.getvalue())

    assert exit_code == 1
    assert payload == {
        "schema": "universal-docs.context/v1",
        "status": "unavailable",
        "context": "",
        "error": "preflight_not_found",
    }


def test_context_cli_rejects_symlink_request_file(tmp_path: Path) -> None:
    target = tmp_path / "request.json"
    target.write_bytes(b"{}")
    link = tmp_path / "request-link.json"
    link.symlink_to(target)
    output = io.BytesIO()

    exit_code = context_cli.main(["--request-file", str(link)], stdout=output)
    payload = json.loads(output.getvalue())

    assert exit_code == 1
    assert payload["status"] == "unavailable"
    assert payload["context"] == ""
    assert payload["error"] == "request_file_not_regular"


def test_official_receipt_does_not_invent_package_identity() -> None:
    official_request = SimpleNamespace(
        model_dump=lambda **kwargs: {
            "source_id": "mcp-tools",
            "context_max_bytes": 2048,
            "selection": "requested",
            "requested_version": "2026-07-28",
            "freshness_mode": "require_check",
        }
    )
    now = time.time()
    result = successful_result("Tools specification data.")
    result["receipt"]["source"].update(
        {
            "kind": "official_markdown",
            "version_binding": "versioned_url",
            "url": "https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md",
        }
    )
    result["receipt"]["target"] = {
        "package": None,
        "ecosystem": None,
        "source_id": "mcp-tools",
        "selection": "requested",
        "target_version": "2026-07-28",
        "requested_version": "2026-07-28",
        "installed_version": None,
        "installed_resolution": "not_applicable",
        "latest_observed": None,
    }
    result["receipt"]["freshness"].update(
        {"policy": "require_check", "checked_at": now}
    )

    from universal_docs_mcp.context_delivery import build_context_packet

    result["receipt"]["integrity"] = context_integrity(
        result["context"], result["receipt"]
    )
    packet = build_context_packet(official_request, result)

    assert 'Target source ID: "mcp-tools"' in packet
    assert 'Target package: "mcp-tools"' not in packet
    assert "Target package: null" in packet
    assert "Ecosystem: null" in packet
    assert "not applicable; this is not a package" in packet


def test_cli_exit_agrees_with_serialized_unavailable_frame(tmp_path, monkeypatch):
    request_path = tmp_path / "request.json"
    request_path.write_text("{}")
    selected = request().model_copy(update={"context_max_bytes": 8000})
    monkeypatch.setattr(context_cli, "parse_request", lambda raw: selected)

    async def run(parsed, cache):
        # Valid UTF-8 source controls expand during JSON serialization.
        result = successful_result("\x01" * 6000)
        result["receipt"]["selection"]["budget_bytes"] = 8000
        return result

    monkeypatch.setattr(context_cli, "_run_cli", run)
    output = io.BytesIO()
    code = context_cli.main(["--request-file", str(request_path)], stdout=output)
    frame = json.loads(output.getvalue())
    assert frame["status"] == "unavailable"
    assert frame["error"] == "response_too_large"
    assert len(output.getvalue()) <= context_cli.MAX_OUTPUT_BYTES
    assert code == 1
