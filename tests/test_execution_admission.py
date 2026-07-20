from __future__ import annotations

from keystone_agents.execution_admission import admit_provider_action
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan


def _plan(
    *,
    target_agent: str,
    intent: str,
    provider_system: str = "google_calendar",
) -> ManualRequestPlan:
    return ManualRequestPlan(
        source="llm",
        target_agent=target_agent,
        intent=intent,
        provider_system=provider_system,
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


def test_semantic_provider_intent_is_not_vetoed_by_phrase_detector() -> None:
    admission = admit_provider_action(
        provider="google_calendar",
        provider_action_bound=False,
        semantic_plan=_plan(
            target_agent="chief_of_staff",
            intent="business_system_write",
        ),
        allowed_agents={"chief_of_staff"},
    )

    assert admission.mode == "provider_action"
    assert admission.positive_intent is True
    assert admission.provider_action_bound is False
    assert admission.can_execute_provider_action is True
    assert admission.can_block_for_missing_fields is True


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


def test_provider_wording_cannot_override_semantic_provider_identity() -> None:
    admission = admit_provider_action(
        provider="google_calendar",
        provider_action_bound=True,
        semantic_plan=_plan(
            target_agent="chief_of_staff",
            intent="business_system_write",
            provider_system="airtable",
        ),
        allowed_agents={"chief_of_staff"},
    )

    assert admission.mode == "semantic_planning"
    assert admission.positive_intent is False
    assert admission.can_execute_provider_action is False


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
