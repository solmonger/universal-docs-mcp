"""Model-facing packets must preserve the receipt's limits of authority."""

from tests.test_context_cli import request, successful_result
from universal_docs_mcp.context_delivery import build_context_packet, deliver_result


def test_packet_retains_unverified_binding_and_fetch_provenance():
    result = successful_result()
    receipt = result["receipt"]
    receipt["source"]["version_binding"] = "unverified_git_ref"
    packet = build_context_packet(request(), result)
    assert 'Source version binding: "unverified_git_ref"' in packet
    assert receipt["source"]["content_sha256"] in packet
    assert str(receipt["freshness"]["fetched_at"]) in packet
    assert str(receipt["freshness"]["checked_at"]) in packet
    assert "not proven to match the requested release" in packet


def test_malformed_error_is_an_unavailable_frame_not_an_exception():
    value = deliver_result(request(), {"found": None, "error": ["untrusted"]})
    assert value.status == "unavailable"
    assert value.context == ""
    assert value.error == "preflight_invalid_receipt"
