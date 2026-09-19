"""Scripted routing-schema controls; original model payloads are not retained."""

from __future__ import annotations

import json

import pytest
from agents.agent_output import AgentOutputSchema
from agents.exceptions import ModelBehaviorError
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from keystone_agents.agent_decision_contracts import orchestrator_decision_contract
from keystone_agents.runtime.decision_validation import validate_specialist_decision
from keystone_agents.schemas.decision_ownership import AgentDecisionRecord
from keystone_agents.schemas.feedback import OperatorFeedbackRequest
from keystone_agents.schemas.orchestrator import OrchestratorResult, OrchestratorRouteDecision


def _payload(*, route="business_research_analyst", workflow=None):
    workflow = workflow or []
    selected = list(dict.fromkeys([route, *workflow]))
    result = OrchestratorResult(
        route=route, target_agent=route, workflow=workflow, routing_mode="llm",
        clarification_request=(
            "Which synthetic evaluation is intended?" if route == "clarification" else None
        ),
        decision=OrchestratorRouteDecision(
            decision_owner="orchestrator", decision_stage="orchestrator_route_selection",
            selected_candidate_id=route, selected_candidate_ids=selected,
            candidate_assessments=[{
                "candidate_id": identity, "disposition": "selected",
                "rationale": "This owner performs a requested bounded stage.",
            } for identity in selected],
            reasoning="The chosen owners match the supplied request and evidence.",
        ),
    )
    return result.model_dump(mode="json", exclude={"retrieval_diagnostics"})


@pytest.mark.parametrize("case", [
    "duplicate_assessment", "selected_set_mismatch", "selected_marked_plausible",
    "send_enabled", "can_send_email", "feedback_send_enabled", "whitespace_candidate",
])
def test_json_schema_valid_controls_can_still_fail_python_validators(case):
    payload = _payload(workflow=["business_research_analyst", "opportunity_scout"])
    decision = payload["decision"]
    expected = ""
    if case == "duplicate_assessment":
        decision["candidate_assessments"].append(dict(decision["candidate_assessments"][0]))
        expected = "same candidate twice"
    elif case == "selected_set_mismatch":
        decision["selected_candidate_ids"] = ["business_research_analyst"]
        expected = "must match selected assessments"
    elif case == "selected_marked_plausible":
        for assessment in decision["candidate_assessments"]:
            assessment["disposition"] = "plausible"
        expected = "must have disposition='selected'"
    elif case in {"send_enabled", "can_send_email"}:
        payload[case] = True
        expected = "cannot enable sending"
    elif case == "feedback_send_enabled":
        feedback = OperatorFeedbackRequest(
            object_type="other", object_id="synthetic-evaluation", source_agent="orchestrator",
        )
        payload["operator_feedback_request"] = feedback.model_dump(mode="json")
        payload["operator_feedback_request"]["send_enabled"] = True
        expected = "must not enable sending"
    elif case == "whitespace_candidate":
        decision["candidate_assessments"][0]["candidate_id"] = "   "
        expected = "at least 1 character"
    native = AgentOutputSchema(OrchestratorResult)
    assert Draft202012Validator(native.json_schema()).is_valid(payload)
    with pytest.raises(ValidationError) as caught:
        OrchestratorResult.model_validate_json(json.dumps(payload), strict=True)
    messages = [item["msg"] for item in caught.value.errors(include_input=False)]
    assert any(expected in message for message in messages)
    with pytest.raises(ModelBehaviorError):
        native.validate_json(json.dumps(payload))


@pytest.mark.parametrize("route,workflow", [
    ("business_research_analyst", []),
    ("clarification", []),
    ("business_research_analyst", ["business_research_analyst", "opportunity_scout"]),
])
def test_main_false_context_flag_has_valid_single_multi_and_clarification_forms(route, workflow):
    payload = _payload(route=route, workflow=workflow)
    payload["provider_context_decisions"] = [AgentDecisionRecord(
        decision_owner="orchestrator", decision_stage="provider_context_selection",
        needs_more_context=True, reasoning="A provider record has not been selected yet.",
    ).model_dump(mode="json")]
    native = AgentOutputSchema(OrchestratorResult)
    assert Draft202012Validator(native.json_schema()).is_valid(payload)
    parsed = native.validate_json(json.dumps(payload))
    outcome, _ = validate_specialist_decision(parsed, orchestrator_decision_contract())
    assert outcome.status == "accepted"
    assert parsed.route == route and parsed.workflow == workflow
    assert parsed.decision.needs_more_context is False
    assert parsed.provider_context_decisions[0].needs_more_context is True
    assert not parsed.send_enabled and not parsed.can_send_email


@pytest.mark.parametrize("route,workflow", [
    ("business_research_analyst", []),
    ("business_research_analyst", ["business_research_analyst", "opportunity_scout"]),
    ("chief_of_staff", ["business_research_analyst", "opportunity_scout"]),
    ("clarification", []),
])
@pytest.mark.parametrize("reverse_candidates", [False, True])
def test_selection_set_examples_preserve_owner_and_workflow_independently_of_candidate_order(
    route, workflow, reverse_candidates
):
    payload = _payload(route=route, workflow=workflow)
    decision = payload["decision"]
    decision["candidate_assessments"].append({
        "candidate_id": "gmail_triage", "disposition": "plausible",
        "rationale": "Could own a different email-specific task, not the selected work.",
    })
    if reverse_candidates:
        decision["selected_candidate_ids"].reverse()
        decision["candidate_assessments"].reverse()
    parsed = AgentOutputSchema(OrchestratorResult).validate_json(json.dumps(payload))
    outcome, evidence = validate_specialist_decision(parsed, orchestrator_decision_contract())
    expected = set([route, *workflow])
    assert outcome.status == "accepted"
    assert set(evidence.required_selected_ids) == expected
    assert set(parsed.decision.selected_candidate_ids) == expected
    assert parsed.decision.selected_candidate_ids == decision["selected_candidate_ids"]
    assert parsed.route == route and parsed.workflow == workflow
    assert next(item for item in parsed.decision.candidate_assessments
                if item.candidate_id == "gmail_triage").disposition == "plausible"

    # A contradictory extra selection is rejected, never dropped or chosen for the model.
    decision["selected_candidate_ids"].append("gmail_triage")
    with pytest.raises(ValidationError, match="must match selected assessments"):
        OrchestratorResult.model_validate(payload)


def test_manager_primary_is_part_of_the_selection_even_when_workflow_contains_only_children():
    payload = _payload(
        route="chief_of_staff", workflow=["business_research_analyst", "opportunity_scout"],
    )
    payload["decision"]["candidate_assessments"] = [
        item for item in payload["decision"]["candidate_assessments"]
        if item["candidate_id"] != "chief_of_staff"
    ]
    with pytest.raises(ValidationError, match="must match selected assessments"):
        OrchestratorResult.model_validate(payload)


def test_legacy_consistent_scalar_list_union_remains_valid():
    payload = _payload(workflow=["business_research_analyst", "opportunity_scout"])
    # Legacy representations may split the primary scalar and additional list IDs.
    payload["decision"]["selected_candidate_ids"] = ["opportunity_scout"]
    parsed = OrchestratorResult.model_validate(payload)
    assert set(parsed.decision.selected_candidate_ids) == {
        "business_research_analyst", "opportunity_scout",
    }
    assert validate_specialist_decision(parsed, orchestrator_decision_contract())[0].status == (
        "accepted"
    )
