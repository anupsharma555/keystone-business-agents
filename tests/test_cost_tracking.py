from __future__ import annotations

import pytest

from keystone_agents.cost_tracking import (
    append_cost_tracking_directive,
    cost_tracking_requested,
    parse_cost_tracking_directive,
)


def test_parse_cost_tracking_directive_cleans_operator_phrase() -> None:
    directive = parse_cost_tracking_directive(
        "opportunity scout find 3 partnership leads. Also keep track of this run costs."
    )

    assert directive.requested is True
    assert directive.cleaned_text == "opportunity scout find 3 partnership leads"


def test_cost_tracking_directive_append_is_idempotent() -> None:
    request = "research Spring Health"

    with_directive = append_cost_tracking_directive(request)

    assert cost_tracking_requested(with_directive) is True
    assert append_cost_tracking_directive(with_directive) == with_directive


@pytest.mark.parametrize(
    "request_text",
    [
        "Record costs from this receipt in Airtable as a personal expense.",
        "Track costs from these invoices in the finance table.",
        "Show cost details from the vendor receipt.",
        "Report cost variance for Q2.",
        "Include costs in the Airtable record.",
    ],
)
def test_business_cost_language_is_not_a_run_cost_tracking_directive(
    request_text: str,
) -> None:
    directive = parse_cost_tracking_directive(request_text)

    assert directive.requested is False
    assert directive.cleaned_text == request_text
