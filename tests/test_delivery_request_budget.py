"""F-03: enforce the requested document budget before packet formatting."""

import pytest

from tests.test_context_cli import request, successful_result
from universal_docs_mcp.context_delivery import deliver_result


@pytest.mark.parametrize(
    "context,budget,reported",
    [
        ("x" * 488, 128, 488),
        ("é" * 80, 128, 160),
        ("small", 128, 1),
    ],
)
def test_rejects_over_budget_or_miscounted_context(context, budget, reported):
    req = request().model_copy(update={"context_max_bytes": budget})
    result = successful_result(context)
    result["receipt"]["selection"].update(budget_bytes=budget, context_bytes=reported)
    outcome = deliver_result(req, result)
    assert outcome.status == "unavailable"
    assert outcome.context == ""


def test_rejects_receipt_that_claims_a_different_budget():
    req = request().model_copy(update={"context_max_bytes": 128})
    result = successful_result("small")
    result["receipt"]["selection"]["budget_bytes"] = 2048
    assert deliver_result(req, result).status == "unavailable"
