from __future__ import annotations

from keystone_agents.execution_admission import admit_provider_action
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan


def _plan(*, target_agent: str, intent: str) -> ManualRequestPlan:
    return ManualRequestPlan(
        target_agent=target_agent,
        intent=intent,
    )


def test_provider_keyword_or_action_evidence_cannot_bypass_semantic_plan() -> None:
    admission = admit_provider_action(
        provider="google_calendar",
        provider_action_bound=True,
        semantic_plan=_plan(
            target_agent="chief_of_staff",
            intent="route_request",
        ),
        allowed_agents={"chief_of_staff"},
    )

    assert admission.mode == "semantic_planning"
    assert admission.provider_action_bound is True
    assert admission.positive_intent is False
    assert admission.can_execute_provider_action is False
    assert admission.can_block_for_missing_fields is False


def test_semantic_write_intent_without_provider_bound_action_stays_shared() -> None:
    admission = admit_provider_action(
        provider="google_calendar",
        provider_action_bound=False,
        semantic_plan=_plan(
            target_agent="chief_of_staff",
            intent="business_system_write",
        ),
        allowed_agents={"chief_of_staff"},
    )

    assert admission.mode == "semantic_planning"
    assert admission.positive_intent is True
    assert admission.provider_action_bound is False
    assert admission.can_execute_provider_action is False
    assert admission.can_block_for_missing_fields is False


def test_matching_semantic_and_provider_action_evidence_admits_fast_path() -> None:
    admission = admit_provider_action(
        provider="google_calendar",
        provider_action_bound=True,
        semantic_plan=_plan(
            target_agent="chief_of_staff",
            intent="business_system_write",
        ),
        allowed_agents={"chief_of_staff"},
    )

    assert admission.mode == "provider_action"
    assert admission.can_execute_provider_action is True
    assert admission.can_block_for_missing_fields is True


def test_exact_typed_control_remains_deterministic() -> None:
    admission = admit_provider_action(
        provider="google_calendar",
        provider_action_bound=False,
        semantic_plan=None,
        allowed_agents={"chief_of_staff"},
        typed_control=True,
    )

    assert admission.mode == "typed_control"
    assert admission.intent_source == "typed_control"
    assert admission.can_execute_provider_action is True
    assert admission.can_block_for_missing_fields is True
