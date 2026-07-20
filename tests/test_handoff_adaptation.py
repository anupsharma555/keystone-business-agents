from __future__ import annotations

import pytest

from keystone_agents.schemas.handoff_types import assess_handoff_adaptation

SOURCE_PLAN = {
    "ask_shape": {
        "ask_breadth": "narrow",
        "evidence_depth": "quick",
        "source_type_preference": ["official"],
        "strict_filter_mode": "exact",
        "output_form": "table",
        "prior_context_dependency": "selected_context",
        "permission_state": "read_only",
        "cost_mode": "minimize",
        "stop_condition": "return_zero_without_broadening_if_no_exact_match",
    }
}


@pytest.mark.parametrize(
    ("source_agent", "target_agent"),
    (
        ("orchestrator", "gmail_triage"),
        ("gmail_triage", "business_research_analyst"),
        ("opportunity_scout", "business_research_analyst"),
        ("business_research_analyst", "outreach_composer"),
        ("chief_of_staff", "google_workspace_context_agent"),
    ),
)
def test_named_adapter_transitions_preserve_explicit_ask_shape(
    source_agent: str,
    target_agent: str,
) -> None:
    assessment = assess_handoff_adaptation(
        source_agent=source_agent,
        target_agent=target_agent,
        source_plan=SOURCE_PLAN,
        target_payload=SOURCE_PLAN,
    )

    assert assessment.status == "compatible"
    assert assessment.missing_required_fields == []
    assert assessment.lossy_fields == []
    assert set(assessment.required_fields) == set(SOURCE_PLAN["ask_shape"])


def test_adapter_blocks_lost_exact_filter_selected_context_or_permission() -> None:
    target = {"ask_shape": {**SOURCE_PLAN["ask_shape"]}}
    target["ask_shape"].update(
        {
            "strict_filter_mode": "flexible",
            "prior_context_dependency": "unspecified",
            "permission_state": "draft_only",
        }
    )

    assessment = assess_handoff_adaptation(
        source_agent="orchestrator",
        target_agent="outreach_composer",
        source_plan=SOURCE_PLAN,
        target_payload=target,
    )

    assert assessment.status == "blocked"
    assert assessment.missing_required_fields == ["prior_context_dependency"]
    assert assessment.lossy_fields == ["strict_filter_mode", "permission_state"]
    assert "Restore the lost safety" in assessment.safe_next_action


def test_adapter_requests_clarification_for_lost_output_form_only() -> None:
    source = {"ask_shape": {"output_form": "table"}}
    target = {"ask_shape": {"output_form": "unspecified"}}

    assessment = assess_handoff_adaptation(
        source_agent="business_research_analyst",
        target_agent="outreach_composer",
        source_plan=source,
        target_payload=target,
    )

    assert assessment.status == "needs_clarification"
    assert assessment.missing_required_fields == ["output_form"]


def test_adapter_reports_policy_defaults_without_claiming_loss() -> None:
    assessment = assess_handoff_adaptation(
        source_agent="chief_of_staff",
        target_agent="rss_context_agent",
        source_plan={"ask_shape": {}},
        target_payload={"ask_shape": {"cost_mode": "minimize"}},
    )

    assert assessment.status == "compatible_with_warnings"
    assert assessment.defaulted_policy_fields == ["cost_mode"]
