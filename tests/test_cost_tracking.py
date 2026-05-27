from __future__ import annotations

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
