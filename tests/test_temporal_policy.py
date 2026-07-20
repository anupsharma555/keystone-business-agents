from __future__ import annotations

from keystone_agents.temporal_policy import temporal_depth_policy


def test_temporal_depth_policy_detects_current_request_and_budget_blocker() -> None:
    policy = temporal_depth_policy(
        "Find the latest 2026 behavioral health AI partnerships.",
        tool_budget_exhausted=True,
    )

    assert policy["schema"] == "keystone.temporal_depth_policy.v1"
    assert policy["temporal_intent"] is True
    assert {"latest", "2026"}.issubset(set(policy["trigger_terms"]))
    assert policy["source_recency_requirement"] == "recent_or_current"
    assert policy["independent_validation"] == "required_when_available"
    assert policy["source_read_through"] == "read_selected_sources_before_synthesis"
    assert policy["tool_budget_exhausted"] is True
    assert "not enough evidence yet" in policy["completion_rule"]


def test_temporal_depth_policy_keeps_non_temporal_requests_on_standard_sufficiency() -> None:
    policy = temporal_depth_policy("Summarize the attached source bundle.")

    assert policy["temporal_intent"] is False
    assert policy["trigger_terms"] == []
    assert policy["source_recency_requirement"] == "as_requested"
    assert policy["completion_rule"] == "Use normal source sufficiency and blocker rules."


def test_provider_state_now_does_not_activate_external_research_policy() -> None:
    policy = temporal_depth_policy(
        "Is it on the calendar now?",
        provider_system="google_calendar",
        requires_live_search=False,
    )

    assert policy["temporal_intent"] is False
    assert policy["trigger_terms"] == []
    assert policy["source_recency_requirement"] == "as_requested"
