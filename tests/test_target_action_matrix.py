from __future__ import annotations

import pytest

from keystone_agents.agents.manual_request_planner import resolve_manual_request_plan
from keystone_agents.manual_request import infer_manual_request_plan
from keystone_agents.target_action_matrix import (
    ContextualTargetActionCase,
    TargetActionCase,
    contextual_target_action_scorecard,
    target_action_scorecard,
)

REQUIRED_CATEGORIES = {
    "artifact-backed",
    "compare",
    "crm",
    "draft",
    "label",
    "post",
    "read",
    "research",
    "save-plan",
    "schedule",
    "summarize",
    "workflow",
}


def test_target_action_scorecard_covers_anu_120_acceptance_surface() -> None:
    cases = target_action_scorecard()

    assert len(cases) >= 20
    assert len({case.case_id for case in cases}) == len(cases)
    assert REQUIRED_CATEGORIES <= {case.category for case in cases}
    assert sum(1 for case in cases if case.artifact_backed) >= 3
    assert {
        "airtable_context_agent",
        "business_research_analyst",
        "chief_of_staff",
        "gmail_triage",
        "google_workspace_context_agent",
        "opportunity_scout",
        "outreach_composer",
        "preprints_context_agent",
        "rss_context_agent",
        "zotero_context_agent",
    } <= {case.owner_agent for case in cases}
    for case in cases:
        assert case.request
        assert case.source_system
        assert case.target_system
        assert case.required_context
        assert case.allowed_tool_tier
        assert case.approval_gate
        assert case.expected_proof
        assert case.fallback_blocker


@pytest.mark.parametrize(
    "case",
    target_action_scorecard(),
    ids=[case.case_id for case in target_action_scorecard()],
)
def test_manual_planner_resolves_scorecard_target_actions(case: TargetActionCase) -> None:
    plan = infer_manual_request_plan(case.request, requested_agent="orchestrator")

    assert plan.target_agent == case.owner_agent
    assert plan.intent == case.expected_intent
    assert plan.target_type == case.expected_target_type
    assert plan.task_objective == case.expected_task_objective
    assert plan.expected_artifact_type == case.expected_artifact_type
    assert plan.side_effect_policy == case.expected_side_effect_policy
    for expected_warning in case.expected_warning_substrings:
        assert any(expected_warning in warning for warning in plan.planner_warnings)


def test_approval_required_scorecard_cases_do_not_enable_external_side_effects() -> None:
    approval_cases = [
        case for case in target_action_scorecard() if case.approval_gate != "none"
    ]

    assert approval_cases
    for case in approval_cases:
        plan = infer_manual_request_plan(case.request, requested_agent="orchestrator")
        assert plan.side_effect_policy in {
            "draft_or_read_only",
            "internal_write_approval_required",
        }
        assert plan.side_effect_policy != "send_enabled"
        if case.expected_intent == "blocked_send":
            assert plan.task_objective == "blocked_side_effect"
            assert plan.planner_warnings


def test_contextual_scorecard_covers_supported_and_missing_tool_contracts() -> None:
    cases = contextual_target_action_scorecard()

    assert 5 <= len(cases) <= 11
    assert len({case.case_id for case in cases}) == len(cases)
    assert any(case.tool_contract_status.startswith("unsupported") for case in cases)
    assert any("supported" in case.tool_contract_status for case in cases)
    for case in cases:
        assert case.prior_context
        assert case.follow_up
        assert case.target_action
        assert case.required_tool_change
        assert case.expected_safety_boundary


@pytest.mark.parametrize(
    "case",
    contextual_target_action_scorecard(),
    ids=[case.case_id for case in contextual_target_action_scorecard()],
)
def test_contextual_scorecard_routes_follow_up_to_source_owner(
    case: ContextualTargetActionCase,
) -> None:
    plan = resolve_manual_request_plan(
        case.follow_up,
        requested_agent="chief_of_staff",
        workflow_state={
            "recent_slack_thread": [{"summary": case.prior_context}],
            "slack_context": {"thread_ts": "1715366400.000100"},
        },
    )

    assert plan.target_agent == case.owner_agent
    assert plan.intent == case.expected_intent
