"""No intent is not evidence of a relevant match."""

import pytest

from universal_docs_mcp.preflight import _select_context


@pytest.mark.parametrize("query", [None, "and for the"])
def test_missing_meaningful_intent_returns_outline_not_default_docs(query):
    context, selection = _select_context(
        "# Usage\nAnd the examples for this package.",
        package="demo",
        source="pypi_description",
        source_url="https://pypi.org/pypi/demo/1.0.0/json",
        version="1.0.0",
        query=query,
        requested_sections=[],
        budget_bytes=4000,
    )
    assert context == ""
    assert selection["no_match"] is True
    assert selection["section_map_total"] == 1
