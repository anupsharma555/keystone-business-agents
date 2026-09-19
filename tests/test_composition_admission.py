from __future__ import annotations

import pytest

from keystone_agents.planning.composition_admission import (
    resolve_provider_free_composition_admission,
)
from keystone_agents.schemas.manual_request_plan import AskShapePolicy, ManualRequestPlan


def _draft_plan(**updates: object) -> ManualRequestPlan:
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="outreach_composer",
        target_agent="outreach_composer",
        intent="outreach_draft",
        task_objective="outreach_draft",
        expected_artifact_type="outreach_draft",
        provider_system="unspecified",
        provider_operations=[],
        requires_approved_context=True,
        side_effect_policy="draft_or_read_only",
        ask_shape=AskShapePolicy(
            output_form="draft",
            prior_context_dependency="selected_context",
            permission_state="draft_only",
            audience_scope="external",
        ),
    )
    return plan.model_copy(update=updates)


@pytest.mark.parametrize(
    "source_route",
    [
        "gmail_triage",
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
    ],
)
def test_completed_same_thread_evidence_admits_provider_free_composition(
    source_route: str,
) -> None:
    admission = resolve_provider_free_composition_admission(
        _draft_plan(),
        workflow_state={
            "prior_agent_runs": [
                {
                    "id": "run-1",
                    "route": source_route,
                    "status": "success",
                    "thread_correlation": "same_thread",
                    "summary": "Bounded prior result.",
                }
            ]
        },
    )

    assert admission.composition_allowed is True
    assert admission.external_use_approval_required is True
    assert admission.provider_action_allowed is False
    assert admission.same_thread_verified is True
    assert admission.source_route == source_route
    assert admission.source_run_id == "run-1"


def test_operator_approved_synthetic_facts_admit_show_only_composition() -> None:
    plan = _draft_plan(
        ask_shape=AskShapePolicy(
            output_form="draft",
            prior_context_dependency="selected_context",
            permission_state="draft_only",
            audience_scope="external",
            source_type_preference=["approved_synthetic"],
        )
    )

    admission = resolve_provider_free_composition_admission(
        plan,
        workflow_state=None,
    )

    assert admission.composition_allowed is True
    assert admission.context_kind == "operator_approved_synthetic_facts"
    assert admission.reason == "admitted_approved_synthetic_context"
    assert admission.external_use_approval_required is True
    assert admission.provider_action_allowed is False
    assert admission.same_thread_verified is False


@pytest.mark.parametrize("source_type", ["synthetic", "approved", "provided"])
def test_incomplete_synthetic_approval_marker_does_not_admit_composition(
    source_type: str,
) -> None:
    plan = _draft_plan(
        ask_shape=AskShapePolicy(
            output_form="draft",
            prior_context_dependency="selected_context",
            permission_state="draft_only",
            source_type_preference=[source_type],
        )
    )

    admission = resolve_provider_free_composition_admission(
        plan,
        workflow_state=None,
    )

    assert admission.composition_allowed is False
    assert admission.reason == "selected_context_missing"
    assert admission.provider_action_allowed is False


@pytest.mark.parametrize(
    ("prior_run", "reason"),
    [
        (
            {
                "route": "gmail_triage",
                "status": "completed",
                "summary": "Different thread.",
            },
            "selected_context_not_same_thread",
        ),
        (
            {
                "route": "gmail_triage",
                "status": "blocked",
                "thread_correlation": "same_thread",
                "summary": "Blocked result.",
            },
            "selected_context_not_completed",
        ),
        (
            {
                "route": "chief_of_staff",
                "status": "completed",
                "thread_correlation": "same_thread",
                "summary": "Unsupported source.",
            },
            "selected_context_route_not_supported",
        ),
    ],
)
def test_unverified_prior_result_does_not_admit_composition(
    prior_run: dict[str, str],
    reason: str,
) -> None:
    admission = resolve_provider_free_composition_admission(
        _draft_plan(),
        workflow_state={"prior_agent_runs": [prior_run]},
    )

    assert admission.composition_allowed is False
    assert admission.provider_action_allowed is False
    assert admission.reason == reason


@pytest.mark.parametrize(
    "updates",
    [
        {"provider_system": "gmail", "provider_operations": ["create"]},
        {"requires_live_search": True},
        {"requires_durable_state": True},
        {"workflow": ["gmail_triage", "outreach_composer"]},
        {"side_effect_policy": "internal_write_approval_required"},
    ],
)
def test_provider_or_stateful_plan_never_uses_composition_admission(
    updates: dict[str, object],
) -> None:
    admission = resolve_provider_free_composition_admission(
        _draft_plan(**updates),
        workflow_state={
            "prior_agent_runs": [
                {
                    "route": "gmail_triage",
                    "status": "completed",
                    "thread_correlation": "same_thread",
                    "summary": "Completed prior result.",
                }
            ]
        },
    )

    assert admission.composition_allowed is False
    assert admission.reason == "plan_not_provider_free_composition"
