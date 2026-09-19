from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from keystone_agents.agent_decision_contracts import (
    business_research_decision_contract,
    context_agent_decision_contract,
    opportunity_scout_decision_contract,
    outreach_composer_decision_contract,
)
from keystone_agents.agents.airtable_context import build_airtable_context_agent
from keystone_agents.agents.business_research_analyst import run_business_research_analyst_sdk
from keystone_agents.agents.calendar_action_interpreter import (
    resolve_calendar_action_plan,
    resolve_calendar_lookup_synthesis,
)
from keystone_agents.agents.chief_of_staff import run_chief_of_staff_sdk
from keystone_agents.agents.gmail_triage import (
    GmailAgentDecisionError,
    run_gmail_triage_sdk,
)
from keystone_agents.agents.orchestrator import (
    run_orchestrator_preflight,
    run_orchestrator_sdk,
)
from keystone_agents.calendar_actions import infer_calendar_action_plan
from keystone_agents.gmail_triage.decision_ownership import (
    gmail_decision_evidence,
    validate_gmail_agent_decision,
    validate_verified_gmail_continuation_decision,
)
from keystone_agents.models import BusinessResearchSDKInput, GmailTriageSDKInput
from keystone_agents.planning.compatibility import infer_manual_request_plan
from keystone_agents.receipts.normalization import identity_fingerprints
from keystone_agents.run import run_typed_sdk_agent, sdk_run_failure_metadata
from keystone_agents.runtime.decision_validation import (
    AgentDecisionContract,
    AgentDecisionValidationError,
    SpecialistDecisionEvidence,
    bind_authoritative_tool_evidence,
    decision_validation_telemetry,
    pre_model_decision_context_telemetry,
    validate_specialist_decision,
    verified_candidate_fingerprints_from_receipts,
)
from keystone_agents.runtime.provider_context import (
    provider_context_handoff_text,
    provider_context_requirements_satisfied,
    validate_calendar_provider_context_decision,
)
from keystone_agents.runtime.request_budget import (
    ModelRequestBudgetExhausted,
    activate_model_request_budget,
)
from keystone_agents.runtime.signal_context import (
    run_signal_context_sdk,
    signal_decision_evidence,
    signal_decision_telemetry,
    validate_signal_agent_decision,
)
from keystone_agents.schemas.announcement_feed import (
    AnnouncementFeedEvidence,
    AnnouncementFeedItem,
)
from keystone_agents.schemas.decision_ownership import (
    AgentDecisionRecord,
    DecisionCandidateAssessment,
)
from keystone_agents.schemas.email_triage import EmailTriageResult
from keystone_agents.schemas.operational_context import (
    AirtableContextResult,
    GoogleWorkspaceContextResult,
    RssContextResult,
    ZoteroContextResult,
)
from keystone_agents.schemas.outreach import OutreachLLMDraftPayload
from keystone_agents.sdk import build_local_run_config
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools import (
    announcement_context_tools,
    gmail_query_tools,
    google_calendar_tool,
)

try:
    from agents.models.interface import Model, ModelProvider, ModelResponse
    from agents.usage import Usage
    from openai.types.responses import (
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
    )
except ImportError:
    pytestmark = pytest.mark.skip(reason="OpenAI Agents SDK fake-model hooks unavailable.")


class FakeModel(Model):
    def __init__(self, outputs: list[list[Any]]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[Any],
        model_settings: Any,
        tools: list[Any],
        output_schema: Any,
        handoffs: list[Any],
        tracing: Any,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any,
    ) -> ModelResponse:
        self.calls.append(
            {
                "input": input,
                "tool_names": [tool.name for tool in tools],
                "tool_choice": getattr(model_settings, "tool_choice", None),
                "system_instructions": system_instructions,
            }
        )
        return ModelResponse(
            output=self.outputs.pop(0),
            usage=Usage(requests=1),
            response_id=f"agent-owned-fake-{len(self.calls)}",
        )

    def stream_response(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        raise NotImplementedError


class FakeProvider(ModelProvider):
    def __init__(self, model: FakeModel) -> None:
        self.model = model

    def get_model(self, _model_name: str | None) -> Model:
        return self.model


def test_outreach_essential_context_is_pre_model_decision_evidence() -> None:
    allowed_source_ids = ["keystone_profile", "gmail_thread:thread-halo"]
    contract = outreach_composer_decision_contract(allowed_source_ids)
    missing_essential = OutreachLLMDraftPayload(
        company_name="Halo",
        email_subject="Re: Halo note",
        email_body="Thanks for the note. I look forward to following up.",
        personalization_rationale="Used the selected Gmail thread.",
        source_ids_used=["gmail_thread:thread-halo"],
        decision=AgentDecisionRecord(
            decision_stage="outreach_evidence_selection",
            selected_candidate_ids=["gmail_thread:thread-halo"],
            candidate_assessments=[
                DecisionCandidateAssessment(
                    candidate_id="gmail_thread:thread-halo",
                    disposition="selected",
                    rationale="Supports the requested thread reply.",
                ),
                DecisionCandidateAssessment(
                    candidate_id="keystone_profile",
                    disposition="excluded",
                    rationale="Incorrectly omitted in this first attempt.",
                ),
            ],
            reasoning="Selected only the thread context.",
        ),
    )

    invalid, evidence = validate_specialist_decision(missing_essential, contract)

    assert evidence.candidate_ids == tuple(allowed_source_ids)
    assert evidence.mandatory_selected_ids == ("keystone_profile",)
    assert invalid.status == "repair_required"
    assert invalid.reason_code == "essential_context_not_declared_in_output"

    repaired = missing_essential.model_copy(
        update={
            "source_ids_used": allowed_source_ids,
            "decision": AgentDecisionRecord(
                decision_stage="outreach_evidence_selection",
                selected_candidate_ids=allowed_source_ids,
                candidate_assessments=[
                    DecisionCandidateAssessment(
                        candidate_id=source_id,
                        disposition="selected",
                        rationale="Required approved context used by the reply.",
                    )
                    for source_id in allowed_source_ids
                ],
                reasoning="Selected the verified thread plus essential Keystone context.",
            ),
        }
    )
    accepted, _ = validate_specialist_decision(repaired, contract)
    assert accepted.status == "accepted"


def test_missing_essential_pre_model_context_is_a_harness_failure() -> None:
    contract = AgentDecisionContract(
        route="outreach_composer",
        decision_stage="outreach_evidence_selection",
        evidence_resolver=lambda _output: SpecialistDecisionEvidence.build(()),
        pre_model_candidate_ids=("gmail_thread:thread-halo",),
        mandatory_pre_model_context_ids=("keystone_profile",),
        pre_model_context_source="approved_outreach_context",
    )

    receipt, outcome = pre_model_decision_context_telemetry(contract)

    assert receipt["context_complete"] is False
    assert receipt["missing_mandatory_context_ids"] == ["keystone_profile"]
    assert outcome is not None
    assert outcome.status == "rejected"
    assert outcome.reason_code == "essential_context_not_supplied_to_model"


def test_zotero_abstract_summary_is_validated_not_rewritten_by_python() -> None:
    decision = AgentDecisionRecord(
        decision_stage="zotero_item_selection",
        selected_candidate_ids=["item-current"],
        candidate_assessments=[
            DecisionCandidateAssessment(
                candidate_id="item-current",
                disposition="selected",
                rationale="This is the verified current journal item.",
            )
        ],
        reasoning="Selected the verified item that satisfies the abstract request.",
    )
    procedural = ZoteroContextResult(
        summary="I found the latest item and summarized it in the requested format.",
        article_titles=["Current journal item"],
        zotero_item_keys=["item-current"],
        relevant_evidence=[
            "The abstract reports a substantive finding that Python must not promote."
        ],
        decision=decision,
    )
    contract = context_agent_decision_contract(
        "zotero_context_agent",
        require_substantive_summary=True,
    )

    invalid, _ = validate_specialist_decision(procedural, contract)

    assert invalid.status == "repair_required"
    assert invalid.reason_code == "zotero_substantive_summary_missing"
    assert procedural.summary.startswith("I found")

    repaired = procedural.model_copy(
        update={
            "summary": (
                "The study evaluates how measurement feedback changes cross-site "
                "behavioral-health reporting and identifies uneven implementation."
            )
        }
    )
    accepted, _ = validate_specialist_decision(repaired, contract)
    assert accepted.status == "accepted"


def test_declared_pre_model_candidate_must_be_in_actual_model_input() -> None:
    contract = AgentDecisionContract(
        route="outreach_composer",
        decision_stage="outreach_evidence_selection",
        evidence_resolver=lambda _output: SpecialistDecisionEvidence.build(()),
        pre_model_candidate_ids=("keystone_profile", "gmail_thread:thread-halo"),
        mandatory_pre_model_context_ids=("keystone_profile",),
        pre_model_context_source="approved_outreach_context",
    )

    receipt, outcome = pre_model_decision_context_telemetry(
        contract,
        model_input_text=(
            "Approved decision context includes keystone_profile but omitted the "
            "selected Gmail thread identity."
        ),
    )

    assert receipt["context_complete"] is False
    assert receipt["model_visible_candidate_ids"] == ["keystone_profile"]
    assert receipt["missing_model_visible_candidate_ids"] == ["gmail_thread:thread-halo"]
    assert outcome is not None
    assert outcome.status == "rejected"
    assert outcome.reason_code == "essential_context_not_model_visible"


def test_tool_candidate_aliases_reject_cross_candidate_pairing_and_stay_immutable() -> None:
    candidate_a = "candidate-a"
    candidate_b = "candidate-b"
    url_a = "https://example.test/a"
    url_b = "https://example.test/b"
    tool_evidence = SpecialistDecisionEvidence.build(
        (candidate_a, candidate_b),
        provider_identities_by_candidate={candidate_a: (url_a,), candidate_b: (url_b,)},
    )
    decision = AgentDecisionRecord(
        decision_stage="bounded_selection",
        selected_candidate_id=candidate_b,
        candidate_assessments=[
            DecisionCandidateAssessment(
                candidate_id=candidate_a,
                disposition="excluded",
                rationale="Lower fit.",
            ),
            DecisionCandidateAssessment(
                candidate_id=candidate_b,
                disposition="selected",
                rationale="Best fit.",
            ),
        ],
        reasoning="Compared both candidates.",
    )
    output = SimpleNamespace(decision=decision, model_fields_set={"decision"})
    contract = AgentDecisionContract(
        route="test",
        decision_stage="bounded_selection",
        evidence_resolver=lambda _output: SpecialistDecisionEvidence.build(()),
    )
    mismatched = bind_authoritative_tool_evidence(
        SpecialistDecisionEvidence.build(
            (candidate_b,),
            required_selected_ids=(candidate_b,),
            selection_required=True,
            provider_identities_by_candidate={candidate_b: (url_a,)},
        ),
        tool_evidence,
    )
    corrected = bind_authoritative_tool_evidence(
        SpecialistDecisionEvidence.build(
            (candidate_b,),
            required_selected_ids=(candidate_b,),
            selection_required=True,
            provider_identities_by_candidate={candidate_b: (url_b,)},
        ),
        tool_evidence,
    )

    mismatched_outcome, _ = validate_specialist_decision(
        output,
        contract,
        evidence_override=mismatched,
    )
    corrected_outcome, _ = validate_specialist_decision(
        output,
        contract,
        evidence_override=corrected,
    )
    mismatched_trace = decision_validation_telemetry(
        output,
        contract,
        mismatched,
        mismatched_outcome,
        attempt=1,
    )
    corrected_trace = decision_validation_telemetry(
        output,
        contract,
        corrected,
        corrected_outcome,
        attempt=2,
    )

    assert mismatched_outcome.reason_code == "candidate_identity_integrity_error"
    assert corrected_outcome.status == "accepted"
    assert (
        mismatched.provider_identities_by_candidate
        == tool_evidence.provider_identities_by_candidate
    )
    assert (
        corrected.provider_identities_by_candidate
        == tool_evidence.provider_identities_by_candidate
    )
    assert mismatched_trace["candidate_universe_fingerprint"] == corrected_trace[
        "candidate_universe_fingerprint"
    ]
    assert mismatched_trace["candidate_identity_integrity_errors"]
    assert corrected_trace["candidate_identity_integrity_errors"] == []


def test_candidate_alias_collision_fails_closed_before_selection() -> None:
    collision = SpecialistDecisionEvidence.build(
        ("candidate-a", "candidate-b"),
        required_selected_ids=("candidate-a",),
        selection_required=True,
        provider_identities_by_candidate={
            "candidate-a": ("shared-alias",),
            "candidate-b": ("shared-alias",),
        },
    )
    output = SimpleNamespace(
        decision=AgentDecisionRecord(
            decision_stage="bounded_selection",
            selected_candidate_id="candidate-a",
            candidate_assessments=[
                DecisionCandidateAssessment(
                    candidate_id="candidate-a",
                    disposition="selected",
                    rationale="Best fit.",
                ),
                DecisionCandidateAssessment(
                    candidate_id="candidate-b",
                    disposition="excluded",
                    rationale="Lower fit.",
                ),
            ],
            reasoning="Compared both candidates.",
        ),
        model_fields_set={"decision"},
    )
    contract = AgentDecisionContract(
        route="test",
        decision_stage="bounded_selection",
        evidence_resolver=lambda _output: collision,
    )

    outcome, _ = validate_specialist_decision(output, contract)

    assert outcome.reason_code == "candidate_identity_integrity_error"


def test_missing_model_visible_decision_context_stops_before_model_call() -> None:
    model = FakeModel(outputs=[])
    contract = AgentDecisionContract(
        route="airtable_context_agent",
        decision_stage="airtable_record_selection",
        evidence_resolver=lambda _output: SpecialistDecisionEvidence.build(()),
        pre_model_candidate_ids=("rec-verified",),
        pre_model_context_source="preacquired_airtable_records",
    )

    with pytest.raises(AgentDecisionValidationError) as exc_info:
        run_typed_sdk_agent(
            agent=build_airtable_context_agent(attach_tools=False),
            typed_input="Choose the relevant record from the supplied packet.",
            output_type=AirtableContextResult,
            run_config=build_local_run_config(FakeProvider(model)),
            decision_contract=contract,
        )

    assert model.calls == []
    metadata = sdk_run_failure_metadata(exc_info.value)
    assert metadata["attempt_count"] == 0
    assert metadata["usage"]["requests"] == 0
    context = metadata["request_cache"]["pre_model_decision_context"]
    assert context["context_complete"] is False
    assert context["missing_model_visible_candidate_ids"] == ["rec-verified"]
    assert exc_info.value.outcome.reason_code == "essential_context_not_model_visible"


def test_calendar_interpreter_owns_operation_and_preserves_reasoning() -> None:
    request = (
        "Add Q3 estimated payment due to Google Calendar as an all-day event on September 15, 2026."
    )
    fallback = infer_calendar_action_plan(request, today=date(2026, 8, 3))
    assert fallback is not None
    manual_plan = infer_manual_request_plan(
        request,
        requested_agent="chief_of_staff",
    ).model_copy(
        update={
            "source": "llm",
            "target_agent": "chief_of_staff",
            "intent": "business_system_write",
            "provider_system": "google_calendar",
            "provider_operations": ["create"],
            "primary_target": "Q3 estimated payment due",
        }
    )
    payload = {
        "operation": "create",
        "operation_source_text": "Add",
        "title": "Q3 estimated payment due",
        "title_source_text": "Q3 estimated payment due",
        "start_date": "2026-09-15",
        "date_source_text": "September 15, 2026",
        "all_day": True,
        "ambiguities": [],
        "decision": {
            "decision_owner": "specialist_agent",
            "decision_stage": "calendar_action_interpretation",
            "selected_candidate_ids": ["create"],
            "candidate_assessments": [
                {
                    "candidate_id": "create",
                    "disposition": "selected",
                    "rationale": "The current directive explicitly asks to add an event.",
                },
                {
                    "candidate_id": "none",
                    "disposition": "excluded",
                    "rationale": "The title, date, and all-day scope are complete.",
                },
            ],
            "reasoning": "The operator supplied one complete Calendar create request.",
            "limitations": ["Provider execution remains subject to Python approval gates."],
            "needs_more_context": False,
        },
    }
    model = FakeModel(outputs=[[_structured_message(payload)]])

    resolution = resolve_calendar_action_plan(
        request,
        fallback,
        manual_plan=manual_plan,
        semantic_candidate=True,
        run_config=build_local_run_config(FakeProvider(model)),
        today=date(2026, 8, 3),
    )

    assert resolution.plan is not None
    assert resolution.plan.operation == "create"
    assert resolution.plan.title == "Q3 estimated payment due"
    assert resolution.plan.start_date == "2026-09-15"
    assert resolution.decision_telemetry is not None
    decision = resolution.decision_telemetry["decision_ownership"]
    assert decision["validator_outcome"]["status"] == "accepted"
    assert decision["reasoning"] == ("The operator supplied one complete Calendar create request.")
    assert resolution.decision_telemetry["pre_model_decision_context"]["candidate_ids"] == [
        "create",
        "none",
    ]
    assert len(model.calls) == 1


@pytest.mark.parametrize(
    ("request_text", "status", "indexes"),
    [
        ("Find my medical appointment tomorrow.", "matched", [1]),
        ("Which appointment is mine?", "matched", [1]),
        ("Which of these could be my appointment?", "ambiguous", [0, 1]),
    ],
)
def test_calendar_lookup_model_owns_ordinary_vague_and_ambiguous_choices(
    request_text: str,
    status: str,
    indexes: list[int],
) -> None:
    payload = {
        "status": status,
        "selected_event_indexes": indexes,
        "related_event_groups": [],
        "selection_reason": "The selected bounded event metadata best answers the request.",
        "limitations": ["Only the supplied Calendar candidates were considered."],
    }
    model = FakeModel(outputs=[[_structured_message(payload)]])

    resolution = resolve_calendar_lookup_synthesis(
        request_text,
        [
            {"title": "Window service", "start_date": "2026-08-04"},
            {
                "title": "Medical appointment",
                "start_date": "2026-08-04",
                "location": "Example Clinic",
            },
        ],
        lookup_target="appointment",
        response_scope="focused",
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert resolution.synthesis is not None
    assert resolution.synthesis.status == status
    assert resolution.synthesis.selected_event_indexes == indexes
    assert resolution.openai_requests == 1
    assert model.calls[0]["tool_names"] == []


def test_invalid_calendar_lookup_choice_gets_one_evidence_preserving_repair() -> None:
    invalid = {
        "status": "matched",
        "selected_event_indexes": [9],
        "related_event_groups": [],
        "selection_reason": "Invalid out-of-range choice.",
        "limitations": [],
    }
    repaired = {
        "status": "matched",
        "selected_event_indexes": [1],
        "related_event_groups": [],
        "selection_reason": "The verified second candidate matches the appointment.",
        "limitations": [],
    }
    model = FakeModel(outputs=[[_structured_message(invalid)], [_structured_message(repaired)]])
    events = [
        {"title": "Window service", "start_date": "2026-08-04"},
        {
            "title": "Medical appointment",
            "start_date": "2026-08-04",
            "location": "Example Clinic",
        },
    ]

    resolution = resolve_calendar_lookup_synthesis(
        "Find the appointment in my Calendar.",
        events,
        lookup_target="appointment",
        response_scope="focused",
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert resolution.synthesis is not None
    assert resolution.synthesis.selected_event_indexes == [1]
    assert resolution.openai_requests == 2
    assert resolution.decision_telemetry is not None
    assert resolution.decision_telemetry["attempt_count"] == 2
    assert resolution.decision_telemetry["repair_attempted"] is True
    assert resolution.decision_telemetry["provider_calls_during_repair"] == 0
    assert resolution.decision_telemetry["terminal_status"] == "accepted"
    assert len(model.calls) == 2
    assert [call["tool_names"] for call in model.calls] == [[], []]
    assert "Window service" in str(model.calls[0]["input"])
    assert "Window service" in str(model.calls[1]["input"])
    assert "Medical appointment" in str(model.calls[1]["input"])
    assert "outside the verified candidate range" in str(model.calls[1]["input"])


def test_calendar_lookup_repair_exhaustion_fails_without_python_selection() -> None:
    invalid = {
        "status": "matched",
        "selected_event_indexes": [],
        "related_event_groups": [],
        "selection_reason": "No event index was returned.",
        "limitations": [],
    }
    model = FakeModel(outputs=[[_structured_message(invalid)], [_structured_message(invalid)]])

    resolution = resolve_calendar_lookup_synthesis(
        "Find my appointment.",
        [{"title": "Medical appointment", "start_date": "2026-08-04"}],
        lookup_target="appointment",
        response_scope="focused",
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert resolution.synthesis is None
    assert resolution.openai_requests == 2
    assert len(model.calls) == 2
    assert "remained invalid after one" in resolution.warnings[0]
    assert resolution.decision_telemetry is not None
    assert resolution.decision_telemetry["terminal_status"] == "rejected"
    assert resolution.decision_telemetry["attempts"][-1]["validator_status"] == ("repair_required")


def _tool_call(name: str, arguments: dict[str, Any], *, call_id: str) -> Any:
    return ResponseFunctionToolCall(
        type="function_call",
        name=name,
        call_id=call_id,
        arguments=json.dumps(arguments),
        status="completed",
    )


def _structured_message(payload: dict[str, Any]) -> Any:
    return ResponseOutputMessage(
        id="agent-owned-output",
        type="message",
        role="assistant",
        status="completed",
        content=[
            ResponseOutputText(
                type="output_text",
                text=json.dumps(payload),
                annotations=[],
            )
        ],
    )


def _research_decision_payload(*, selected_id: str) -> dict[str, Any]:
    candidate_ids = ["source-a", "source-b"]
    return {
        "name": "Synthetic Research Target",
        "description": "Bounded fixture profile.",
        "fit_summary": "Source A supports the returned profile.",
        "sources": [
            {
                "source_id": source_id,
                "title": f"Fixture {source_id}",
                "url": f"fixture://{source_id}",
                "source_type": "fixture",
                "supported_claims": [f"Claim supported by {source_id}."],
                "confidence": 0.8,
            }
            for source_id in candidate_ids
        ],
        "claims": [
            {
                "claim_text": "Claim supported by source-a.",
                "source_id": "source-a",
                "confidence": 0.8,
                "claim_type": "company_identity",
            }
        ],
        "decision": {
            "decision_owner": "specialist_agent",
            "decision_stage": "research_source_selection",
            "selected_candidate_ids": [selected_id],
            "candidate_assessments": [
                {
                    "candidate_id": candidate_id,
                    "disposition": ("selected" if candidate_id == selected_id else "excluded"),
                    "rationale": (
                        "Supports the returned claim."
                        if candidate_id == selected_id
                        else "Not used by the returned claim."
                    ),
                }
                for candidate_id in candidate_ids
            ],
            "reasoning": "Selected the source that supports the returned claim.",
            "limitations": ["Synthetic bounded evidence."],
            "needs_more_context": False,
        },
    }


def test_shared_decision_contract_returns_invalid_selection_for_one_agent_repair() -> None:
    invalid = _research_decision_payload(selected_id="source-a")
    invalid["decision"]["selected_candidate_ids"] = ["invented-source"]
    invalid["decision"]["candidate_assessments"] = [
        {
            "candidate_id": "invented-source",
            "disposition": "selected",
            "rationale": "Fabricated identity for validator coverage.",
        },
        {
            "candidate_id": "source-a",
            "disposition": "excluded",
            "rationale": "Incorrect first attempt.",
        },
        {
            "candidate_id": "source-b",
            "disposition": "excluded",
            "rationale": "Incorrect first attempt.",
        },
    ]
    valid = _research_decision_payload(selected_id="source-a")
    model = FakeModel(outputs=[[_structured_message(invalid)], [_structured_message(valid)]])

    result = run_business_research_analyst_sdk(
        BusinessResearchSDKInput(company_name="Synthetic Research Target"),
        run_config=build_local_run_config(FakeProvider(model)),
    )

    telemetry = result.request_cache["decision_ownership"]
    assert len(model.calls) == 2
    assert telemetry["attempt_count"] == 2
    assert telemetry["repair_attempted"] is True
    assert telemetry["attempts"][0]["validator_outcome"]["reason_code"] == (
        "selected_identity_not_in_candidate_set"
    )
    assert telemetry["validator_outcome"]["status"] == "accepted"
    assert result.request_cache["decision_repairs"] == 1
    assert "bounded repair attempt" in str(model.calls[1]["input"])


def test_shared_decision_contract_fails_after_second_invalid_selection() -> None:
    invalid = _research_decision_payload(selected_id="source-a")
    invalid["decision"]["selected_candidate_ids"] = ["invented-source"]
    invalid["decision"]["candidate_assessments"] = [
        {
            "candidate_id": "invented-source",
            "disposition": "selected",
            "rationale": "Fabricated identity for validator coverage.",
        },
        {
            "candidate_id": "source-a",
            "disposition": "excluded",
            "rationale": "Incorrect attempt.",
        },
        {
            "candidate_id": "source-b",
            "disposition": "excluded",
            "rationale": "Incorrect attempt.",
        },
    ]
    model = FakeModel(outputs=[[_structured_message(invalid)], [_structured_message(invalid)]])

    with pytest.raises(AgentDecisionValidationError) as exc_info:
        run_business_research_analyst_sdk(
            BusinessResearchSDKInput(company_name="Synthetic Research Target"),
            run_config=build_local_run_config(FakeProvider(model)),
        )

    metadata = sdk_run_failure_metadata(exc_info.value)
    assert len(model.calls) == 2
    assert metadata["failure_kind"] == "agentdecisionvalidationerror"
    assert metadata["request_cache"]["decision_ownership"]["attempt_count"] == 2
    assert (
        metadata["request_cache"]["decision_ownership"]["validator_outcome"]["status"]
        == "repair_required"
    )


@pytest.mark.parametrize(
    ("route", "output"),
    [
        (
            "airtable_context_agent",
            AirtableContextResult(
                candidate_record_ids=["returned-id", "fabricated-id"],
                recommended_record_identity="fabricated-id",
                decision={
                    "decision_stage": "airtable_record_selection",
                    "selected_candidate_id": "fabricated-id",
                    "candidate_assessments": [
                        {
                            "candidate_id": "returned-id",
                            "disposition": "excluded",
                            "rationale": "Not selected by the first model attempt.",
                        },
                        {
                            "candidate_id": "fabricated-id",
                            "disposition": "selected",
                            "rationale": "Appeared only in model output, not the provider read.",
                        },
                    ],
                    "reasoning": "Selected one claimed record identity.",
                },
            ),
        ),
        (
            "google_workspace_context_agent",
            GoogleWorkspaceContextResult(
                relevant_files=["returned-id", "fabricated-id"],
                recommended_target="fabricated-id",
                decision={
                    "decision_stage": "workspace_artifact_selection",
                    "selected_candidate_id": "fabricated-id",
                    "candidate_assessments": [
                        {
                            "candidate_id": "returned-id",
                            "disposition": "excluded",
                            "rationale": "Not selected by the first model attempt.",
                        },
                        {
                            "candidate_id": "fabricated-id",
                            "disposition": "selected",
                            "rationale": "Appeared only in model output, not the provider read.",
                        },
                    ],
                    "reasoning": "Selected one claimed Workspace identity.",
                },
            ),
        ),
        (
            "zotero_context_agent",
            ZoteroContextResult(
                zotero_item_keys=["returned-id", "fabricated-id"],
                decision={
                    "decision_stage": "zotero_item_selection",
                    "selected_candidate_id": "fabricated-id",
                    "candidate_assessments": [
                        {
                            "candidate_id": "returned-id",
                            "disposition": "excluded",
                            "rationale": "Not selected by the first model attempt.",
                        },
                        {
                            "candidate_id": "fabricated-id",
                            "disposition": "selected",
                            "rationale": "Appeared only in model output, not the provider read.",
                        },
                    ],
                    "reasoning": "Selected one claimed Zotero identity.",
                },
            ),
        ),
    ],
)
def test_context_agent_selection_must_come_from_provider_receipt(
    route: str,
    output: object,
) -> None:
    receipts = [
        {
            "status": "success",
            "identity_fingerprints": identity_fingerprints(["returned-id"]),
        }
    ]

    outcome, _evidence = validate_specialist_decision(
        output,
        context_agent_decision_contract(route),
        verified_candidate_fingerprints=(verified_candidate_fingerprints_from_receipts(receipts)),
    )

    assert outcome.status == "repair_required"
    assert outcome.reason_code == "selected_identity_not_in_provider_receipt"


@pytest.mark.parametrize(
    ("contract", "output"),
    [
        (
            business_research_decision_contract(),
            SimpleNamespace(
                sources=[
                    SimpleNamespace(
                        source_id="source-a",
                        url="https://fabricated.example/research",
                    )
                ],
                source_ids_used=["source-a"],
                claims=[],
                decision=AgentDecisionRecord(
                    decision_stage="research_source_selection",
                    selected_candidate_id="https://fabricated.example/research",
                    candidate_assessments=[
                        DecisionCandidateAssessment(
                            candidate_id="https://fabricated.example/research",
                            disposition="selected",
                            rationale="Used for the returned claim.",
                        )
                    ],
                    reasoning="Selected the only source returned in model output.",
                ),
                model_fields_set={"decision"},
            ),
        ),
        (
            opportunity_scout_decision_contract(),
            SimpleNamespace(
                records=[
                    SimpleNamespace(
                        canonical_entity_key="opportunity-a",
                        sources=[SimpleNamespace(url="https://fabricated.example/opportunity")],
                    )
                ],
                review_candidates=[],
                filtered_candidates=[],
                decision=AgentDecisionRecord(
                    decision_stage="opportunity_candidate_selection",
                    selected_candidate_id="https://fabricated.example/opportunity",
                    candidate_assessments=[
                        DecisionCandidateAssessment(
                            candidate_id="https://fabricated.example/opportunity",
                            disposition="selected",
                            rationale="Ranked as the strongest opportunity.",
                        )
                    ],
                    reasoning="Selected the only opportunity in model output.",
                ),
                model_fields_set={"decision"},
            ),
        ),
    ],
)
def test_search_backed_selection_requires_every_supporting_url_in_provider_receipt(
    contract: object,
    output: object,
) -> None:
    outcome, _evidence = validate_specialist_decision(
        output,
        contract,  # type: ignore[arg-type]
        verified_candidate_fingerprints=identity_fingerprints(
            ["https://returned.example/evidence"]
        ),
    )

    assert outcome.status == "repair_required"
    assert outcome.reason_code == "selected_identity_not_in_provider_receipt"


def test_opportunity_score_normalization_preserves_the_agent_selection() -> None:
    record = SimpleNamespace(
        company_name="Northstar Pilot",
        opportunity_type="partnership",
        source_signals=["current pilot", "official eligibility page"],
        priority_score=99,
        outside_consulting_likelihood=99,
        score_breakdown=SimpleNamespace(priority_score=99),
        score_rationale="Model-authored arithmetic.",
        handoff_to_business_research_analyst=False,
    )
    output = SimpleNamespace(records=[record])
    contract = opportunity_scout_decision_contract()

    assert contract.output_normalizer is not None
    normalization = contract.output_normalizer(output)

    assert normalization is not None
    assert normalization["adjusted_record_count"] == 1
    assert normalization["semantic_selection_changed"] is False
    assert record.priority_score != 99
    assert record.score_breakdown.priority_score == record.priority_score
    assert contract.output_consistency_validator is not None
    assert contract.output_consistency_validator(output) is None


def _orchestrator_route_payload(*, selected_route: str) -> dict[str, Any]:
    return {
        "route": "business_research_analyst",
        "target_agent": "Business Research Analyst",
        "workflow": ["business_research_analyst"],
        "routing_mode": "llm",
        "rationale": "Business Research owns the requested source-backed review.",
        "requires_human_review": True,
        "approval_required": True,
        "external_use_approval_required": True,
        "send_enabled": False,
        "can_send_email": False,
        "decision": {
            "decision_owner": "orchestrator",
            "decision_stage": "orchestrator_route_selection",
            "selected_candidate_id": selected_route,
            "candidate_assessments": [
                {
                    "candidate_id": selected_route,
                    "disposition": "selected",
                    "rationale": "Owns the requested bounded work.",
                },
                {
                    "candidate_id": "clarification",
                    "disposition": "excluded",
                    "rationale": "The request is sufficiently specified for the selected owner.",
                },
            ],
            "reasoning": "Selected the bounded owner for the full request.",
            "needs_more_context": False,
        },
    }


def _chief_delegation_payload(*, selected_workflow: str) -> dict[str, Any]:
    return {
        "agent_name": "chief_of_staff",
        "mode": "llm",
        "intent": "slack runtime review",
        "summary": "Review the bounded runtime state without mutation.",
        "recommended_route": {
            "workflow_type": "slack-runtime-review",
            "target_channel": "current-thread",
            "rationale": "The request asks for current runtime evidence.",
            "requires_human_approval_before_post": True,
        },
        "decision": {
            "decision_owner": "chief_of_staff",
            "decision_stage": "chief_delegation_selection",
            "selected_candidate_id": selected_workflow,
            "candidate_assessments": [
                {
                    "candidate_id": "workflow:slack-runtime-review",
                    "disposition": (
                        "selected"
                        if selected_workflow == "workflow:slack-runtime-review"
                        else "excluded"
                    ),
                    "rationale": "Matches the requested runtime inspection.",
                },
                {
                    "candidate_id": "workflow:clarification",
                    "disposition": (
                        "selected" if selected_workflow == "workflow:clarification" else "excluded"
                    ),
                    "rationale": "The target is already sufficiently bounded.",
                },
            ],
            "reasoning": "Selected a read-only Chief workflow from the full request.",
            "needs_more_context": False,
        },
    }


def test_ambiguous_front_door_request_uses_validated_orchestrator_decision() -> None:
    payload = {
        "route": "chief_of_staff",
        "target_agent": "Chief of Staff",
        "workflow": ["chief_of_staff"],
        "routing_mode": "llm",
        "rationale": "Chief should clarify and coordinate the underspecified request.",
        "requires_human_review": True,
        "approval_required": False,
        "external_use_approval_required": False,
        "send_enabled": False,
        "can_send_email": False,
        "decision": {
            "decision_owner": "orchestrator",
            "decision_stage": "orchestrator_route_selection",
            "selected_candidate_ids": ["chief_of_staff"],
            "candidate_assessments": [
                {
                    "candidate_id": "chief_of_staff",
                    "disposition": "selected",
                    "rationale": "The request needs bounded coordination before execution.",
                },
                {
                    "candidate_id": "clarification",
                    "disposition": "excluded",
                    "rationale": "Chief can ask the next targeted question without guessing.",
                },
            ],
            "reasoning": "Selected Chief to preserve agent-owned interpretation.",
            "limitations": ["No provider action is authorized by this routing decision."],
            "needs_more_context": False,
        },
    }
    model = FakeModel(outputs=[[_structured_message(payload)]])

    preflight = run_orchestrator_preflight(
        "Please look into this and tell me what we should do next.",
        live_manual_plan=False,
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert preflight.selected_agent == "chief_of_staff"
    assert preflight.route_result.routing_mode == "llm"
    assert preflight.route_result.decision.selected_candidate_ids == ["chief_of_staff"]
    assert len(preflight.sdk_usage_events) == 1
    assert preflight.sdk_usage_events[0]["agent_name"] == "orchestrator"
    decision = preflight.sdk_usage_events[0]["request_cache"]["decision_ownership"]
    assert decision["validator_outcome"]["status"] == "accepted"
    assert decision["reasoning"] == ("Selected Chief to preserve agent-owned interpretation.")
    assert len(model.calls) == 1


def test_ordinary_front_door_request_uses_orchestrator_before_specialist() -> None:
    request = (
        "Please research Northstar Care's current offering and give me a "
        "source-backed internal brief."
    )
    model = FakeModel(
        outputs=[
            [
                _structured_message(
                    _orchestrator_route_payload(selected_route="business_research_analyst")
                )
            ]
        ]
    )

    preflight = run_orchestrator_preflight(
        request,
        requested_agent="business_research_analyst",
        live_manual_plan=False,
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert preflight.selected_agent == "business_research_analyst"
    assert preflight.route_result.routing_mode == "llm"
    assert len(model.calls) == 1
    assert model.calls[0]["tool_names"] == []
    model_input, _ = json.JSONDecoder().raw_decode(model.calls[0]["input"][0]["content"])
    assert model_input["request"] == request
    assert model_input["routing_advice"]["deterministic_route"] == ("business_research_analyst")
    assert model_input["routing_advice"]["manual_request_plan"]
    assert preflight.sdk_usage_events[0]["run_stage"] == ("orchestrator_preflight.route_selection")


def test_named_opportunity_preflight_repairs_incompatible_company_route() -> None:
    request = (
        "Opportunity Scout, I'm looking for one live U.S. non-dilutive funding or "
        "pilot opening that a small behavioral-health AI consultancy could pursue "
        "before early November. Compare the strongest current options, choose one, "
        "and give me its deadline, why it fits Keystone, the biggest eligibility "
        "concern, and the official URL. Keep this read-only."
    )
    invalid = _orchestrator_route_payload(selected_route="business_research_analyst")
    repaired = _orchestrator_route_payload(selected_route="opportunity_scout")
    repaired.update(
        {
            "route": "opportunity_scout",
            "target_agent": "Opportunity Scout",
            "workflow": ["opportunity_scout"],
            "rationale": "Opportunity Scout owns live funding discovery and ranking.",
        }
    )
    model = FakeModel(
        outputs=[
            [_structured_message(invalid)],
            [_structured_message(repaired)],
        ]
    )

    preflight = run_orchestrator_preflight(
        request,
        requested_agent="opportunity_scout",
        live_manual_plan=False,
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert preflight.manual_request_plan.target_agent == "opportunity_scout"
    assert preflight.selected_agent == "opportunity_scout"
    assert len(model.calls) == 2
    decision = preflight.sdk_usage_events[0]["request_cache"]["decision_ownership"]
    assert decision["repair_attempted"] is True
    assert decision["validator_outcome"]["status"] == "accepted"
    assert decision["attempts"][0]["validator_outcome"]["reason_code"] == (
        "selected_identity_not_in_candidate_set"
    )


def test_orchestrator_execution_repairs_provider_selection_context_conflict() -> None:
    request = (
        "CoS, add an all-day Q3 estimated-payment event to Google Calendar on September 15, 2026."
    )
    invalid = _orchestrator_route_payload(selected_route="business_research_analyst")
    invalid.update(
        {
            "route": "chief_of_staff",
            "target_agent": "Chief of Staff",
            "workflow": ["chief_of_staff"],
            "provider_context_decisions": [
                {
                    "decision_owner": "orchestrator",
                    "decision_stage": "calendar_context_selection",
                    "selected_candidate_id": "google_calendar:create",
                    "candidate_assessments": [
                        {
                            "candidate_id": "google_calendar:create",
                            "disposition": "selected",
                            "rationale": "The request names one complete Calendar create.",
                        }
                    ],
                    "reasoning": "Selected the Calendar create context.",
                    "needs_more_context": True,
                }
            ],
        }
    )
    invalid["decision"].update(
        {
            "selected_candidate_id": "chief_of_staff",
            "selected_candidate_ids": ["chief_of_staff"],
            "candidate_assessments": [
                {
                    "candidate_id": "chief_of_staff",
                    "disposition": "selected",
                    "rationale": "Chief owns the bounded Calendar action.",
                }
            ],
            "needs_more_context": False,
        }
    )
    repaired = json.loads(json.dumps(invalid))
    repaired["provider_context_decisions"][0]["needs_more_context"] = False
    model = FakeModel(
        outputs=[
            [_structured_message(invalid)],
            [_structured_message(repaired)],
        ]
    )

    result = run_orchestrator_sdk(
        {
            "raw_request": request,
            "provider_candidates": [
                {
                    "candidate_id": "google_calendar:create",
                    "capability": "create one exact Calendar event",
                }
            ],
        },
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert result.output.route == "chief_of_staff"
    assert result.output.provider_context_decisions[0].needs_more_context is False
    assert len(model.calls) == 2
    ownership = result.request_cache["decision_ownership"]
    assert ownership["attempt_count"] == 2
    assert ownership["repair_attempted"] is True
    assert ownership["attempts"][0]["validator_outcome"]["reason_code"] == (
        "orchestrator_needs_more_context_selection_conflict"
    )
    assert ownership["validator_outcome"]["status"] == "accepted"
    repair_prompt = str(model.calls[1]["input"][0]["content"])
    assert "orchestrator_needs_more_context_selection_conflict" in repair_prompt
    assert "provider_context_decisions[0]" in repair_prompt


def test_orchestrator_preserves_valid_model_owned_workflow_order() -> None:
    request = "Research Northstar, assess the opportunity, then prepare internal copy."
    workflow = [
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
    ]
    payload = _orchestrator_route_payload(selected_route="business_research_analyst")
    payload["workflow"] = workflow
    payload["decision"]["selected_candidate_ids"] = workflow
    payload["decision"]["candidate_assessments"] = [
        {
            "candidate_id": route,
            "disposition": "selected",
            "rationale": f"{route} owns its ordered workflow stage.",
        }
        for route in workflow
    ]
    model = FakeModel(outputs=[[_structured_message(payload)]])

    preflight = run_orchestrator_preflight(
        request,
        requested_agent="chief_of_staff",
        live_manual_plan=False,
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert preflight.route_result.workflow == workflow
    assert preflight.route_result.route == "business_research_analyst"
    assert preflight.selected_agent == "business_research_analyst"
    ownership = preflight.sdk_usage_events[0]["request_cache"]["decision_ownership"]
    assert ownership["validator_outcome"]["status"] == "accepted"


def test_orchestrator_repairs_unsupported_workflow_owner_without_substitution() -> None:
    request = "Coordinate a source-backed Northstar review."
    invalid = _orchestrator_route_payload(selected_route="business_research_analyst")
    invalid["workflow"] = ["business_research_analyst", "calendar_magic"]
    repaired = _orchestrator_route_payload(selected_route="business_research_analyst")
    model = FakeModel(outputs=[[_structured_message(invalid)], [_structured_message(repaired)]])

    preflight = run_orchestrator_preflight(
        request,
        requested_agent="chief_of_staff",
        live_manual_plan=False,
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert len(model.calls) == 2
    assert preflight.route_result.workflow == ["business_research_analyst"]
    ownership = preflight.sdk_usage_events[0]["request_cache"]["decision_ownership"]
    assert ownership["attempts"][0]["validator_outcome"]["reason_code"] == (
        "orchestrator_workflow_contains_unsupported_owner"
    )
    assert ownership["validator_outcome"]["status"] == "accepted"
    repair_prompt = str(model.calls[1]["input"][0]["content"])
    assert "workflow is an ordered list of registered agent route IDs" in repair_prompt
    assert "currently selected registered route IDs are: business_research_analyst" in (
        repair_prompt
    )
    assert "never task actions, tool names, or prose" in repair_prompt


def test_orchestrator_repair_accepts_plausible_unselected_route() -> None:
    request = (
        "Could you inspect the Business Expenses schema and explain which fields "
        "are editable without reading any record values?"
    )
    invalid = _orchestrator_route_payload(selected_route="business_research_analyst")
    invalid.update(
        {
            "route": "airtable_context_agent",
            "target_agent": "Airtable Context Agent",
            "workflow": ["airtable_context_agent", "calendar_magic"],
        }
    )
    invalid["decision"].update(
        {
            "selected_candidate_id": "airtable_context_agent",
            "selected_candidate_ids": ["airtable_context_agent"],
            "candidate_assessments": [
                {
                    "candidate_id": "airtable_context_agent",
                    "disposition": "selected",
                    "rationale": "Owns the bounded Airtable schema inspection.",
                },
                {
                    "candidate_id": "chief_of_staff",
                    "disposition": "plausible",
                    "rationale": "Could coordinate, but is not needed for this direct read.",
                },
            ],
        }
    )
    repaired = json.loads(json.dumps(invalid))
    repaired["workflow"] = ["airtable_context_agent"]
    model = FakeModel(outputs=[[_structured_message(invalid)], [_structured_message(repaired)]])

    preflight = run_orchestrator_preflight(
        request,
        requested_agent="airtable_context_agent",
        live_manual_plan=False,
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert len(model.calls) == 2
    assert preflight.selected_agent == "airtable_context_agent"
    ownership = preflight.sdk_usage_events[0]["request_cache"]["decision_ownership"]
    assert ownership["attempts"][0]["validator_outcome"]["reason_code"] == (
        "orchestrator_workflow_contains_unsupported_owner"
    )
    assert ownership["validator_outcome"]["status"] == "accepted"
    assert ownership["attempts"][0]["candidate_assessments"][1]["disposition"] == ("plausible")
    assert ownership["attempts"][1]["candidate_assessments"][1]["disposition"] == ("plausible")
    assert "may remain disposition='plausible'" in str(model.calls[1]["input"][0]["content"])


def test_orchestrator_accepts_plausible_unselected_route_without_repair() -> None:
    request = "Review recent Gmail and prepare Slack-only reply copy."
    payload = _orchestrator_route_payload(selected_route="gmail_triage")
    payload.update(
        {
            "route": "gmail_triage",
            "target_agent": "Gmail Triage",
            "workflow": ["gmail_triage"],
        }
    )
    payload["decision"]["candidate_assessments"].extend(
        [
            {
                "candidate_id": "chief_of_staff",
                "disposition": "plausible",
                "rationale": (
                    "Chief could coordinate a cross-provider version, but Gmail Triage "
                    "directly owns this Gmail-only request."
                ),
            },
        ]
    )
    model = FakeModel(outputs=[[_structured_message(payload)]])

    preflight = run_orchestrator_preflight(
        request,
        requested_agent="gmail_triage",
        live_manual_plan=False,
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert len(model.calls) == 1
    assert preflight.selected_agent == "gmail_triage"
    ownership = preflight.sdk_usage_events[0]["request_cache"]["decision_ownership"]
    assert ownership["validator_outcome"]["status"] == "accepted"
    assert ownership["attempts"][0]["candidate_assessments"][-1]["disposition"] == ("plausible")


def test_orchestrator_stops_after_one_repair_even_for_a_new_validator_defect() -> None:
    request = "Find the recent Gmail conversation that likely needs my reply."
    unsupported = _orchestrator_route_payload(selected_route="gmail_triage")
    unsupported.update(
        {
            "route": "gmail_triage",
            "target_agent": "Gmail Triage",
            "workflow": ["gmail_triage", "gmail_query_step"],
        }
    )
    contradictory = json.loads(json.dumps(unsupported))
    contradictory["workflow"] = ["gmail_triage"]
    contradictory["provider_context_decisions"] = [{
        "decision_owner": "orchestrator",
        "decision_stage": "gmail_context_selection",
        "selected_candidate_id": "gmail:synthetic-thread",
        "needs_more_context": True,
    }]
    model = FakeModel(
        outputs=[
            [_structured_message(unsupported)],
            [_structured_message(contradictory)],
        ]
    )

    with pytest.raises(AgentDecisionValidationError) as exc_info:
        run_orchestrator_sdk(
            {
                "raw_request": request,
                "provider_candidates": [
                    {
                        "candidate_id": "gmail:synthetic-thread",
                        "capability": "read one bounded synthetic Gmail thread",
                    }
                ],
            },
            run_config=build_local_run_config(FakeProvider(model)),
        )

    assert len(model.calls) == 2
    assert exc_info.value.outcome.reason_code == (
        "orchestrator_needs_more_context_selection_conflict"
    )


def test_explicit_specialist_survives_repeated_non_safety_orchestrator_shape_failure() -> None:
    request = "Find the recent Gmail conversation that likely needs my reply."
    invalid = _orchestrator_route_payload(selected_route="gmail_triage")
    invalid.update(
        {
            "route": "gmail_triage",
            "target_agent": "Gmail Triage",
            "workflow": ["gmail_triage", "gmail_query_step"],
        }
    )
    model = FakeModel(outputs=[[_structured_message(invalid)], [_structured_message(invalid)]])

    preflight = run_orchestrator_preflight(
        request,
        requested_agent="gmail_triage",
        live_manual_plan=False,
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert len(model.calls) == 2
    assert preflight.selected_agent == "gmail_triage"
    assert preflight.route_result.route == "gmail_triage"
    assert preflight.sdk_usage_events[0]["status"] == "failed"
    assert preflight.sdk_usage_events[0]["advisory_fallback_used"] is True
    assert preflight.sdk_usage_events[0]["usage"]["requests"] == 2
    assert "non-authoritative" in preflight.route_result.audit_notes[-1]


def test_ambiguous_request_still_fails_closed_after_orchestrator_repair_exhaustion() -> None:
    request = "Find the recent Gmail conversation that likely needs my reply."
    invalid = _orchestrator_route_payload(selected_route="gmail_triage")
    invalid.update(
        {
            "route": "gmail_triage",
            "target_agent": "Gmail Triage",
            "workflow": ["gmail_triage", "gmail_query_step"],
        }
    )
    model = FakeModel(outputs=[[_structured_message(invalid)], [_structured_message(invalid)]])

    with pytest.raises(AgentDecisionValidationError):
        run_orchestrator_preflight(
            request,
            live_manual_plan=False,
            run_config=build_local_run_config(FakeProvider(model)),
        )

    assert len(model.calls) == 2


def test_specialist_still_must_exclude_unselected_assessed_candidates() -> None:
    output = SimpleNamespace(
        decision=AgentDecisionRecord(
            decision_stage="bounded_record_selection",
            selected_candidate_id="record-current",
            candidate_assessments=[
                DecisionCandidateAssessment(
                    candidate_id="record-current",
                    disposition="selected",
                    rationale="Best verified match.",
                ),
                DecisionCandidateAssessment(
                    candidate_id="record-other",
                    disposition="plausible",
                    rationale="Related, but not selected for the requested action.",
                ),
            ],
            reasoning="Selected the current verified record.",
        ),
        model_fields_set={"decision"},
    )
    contract = AgentDecisionContract(
        route="synthetic_specialist",
        decision_stage="bounded_record_selection",
        evidence_resolver=lambda _output: SpecialistDecisionEvidence.build(
            ("record-current", "record-other"),
            selection_required=True,
        ),
    )

    outcome, _evidence = validate_specialist_decision(output, contract)

    assert outcome.status == "repair_required"
    assert outcome.reason_code == "excluded_alternatives_unresolved"


@pytest.mark.parametrize(
    ("runner", "invalid_payload", "valid_payload", "expected_stage"),
    [
        (
            lambda model: run_orchestrator_sdk(
                "Research Northstar and return a source-backed company brief.",
                run_config=build_local_run_config(FakeProvider(model)),
            ),
            _orchestrator_route_payload(selected_route="opportunity_scout"),
            _orchestrator_route_payload(selected_route="business_research_analyst"),
            "orchestrator_route_selection",
        ),
        (
            lambda model: run_chief_of_staff_sdk(
                "Review the current KBA runtime status without changing anything.",
                run_config=build_local_run_config(FakeProvider(model)),
                force_sdk_interpretation=True,
            ),
            _chief_delegation_payload(selected_workflow="workflow:clarification"),
            _chief_delegation_payload(selected_workflow="workflow:slack-runtime-review"),
            "chief_delegation_selection",
        ),
    ],
)
def test_manager_agents_own_route_and_delegation_with_one_validated_repair(
    runner: Any,
    invalid_payload: dict[str, Any],
    valid_payload: dict[str, Any],
    expected_stage: str,
) -> None:
    model = FakeModel(
        outputs=[
            [_structured_message(invalid_payload)],
            [_structured_message(valid_payload)],
        ]
    )

    result = runner(model)

    telemetry = result.request_cache["decision_ownership"]
    assert len(model.calls) == 2
    assert telemetry["decision_stage"] == expected_stage
    assert telemetry["attempt_count"] == 2
    assert telemetry["repair_attempted"] is True
    assert telemetry["validator_outcome"]["status"] == "accepted"
    assert telemetry["attempts"][0]["validator_outcome"]["reason_code"] == (
        "output_identity_not_owned_by_decision"
    )


def test_orchestrator_rejects_contradictory_route_and_target_agent() -> None:
    output = SimpleNamespace(
        route="business_research_analyst",
        target_agent="opportunity_scout",
        workflow=["business_research_analyst"],
        decision=AgentDecisionRecord(
            decision_owner="orchestrator",
            decision_stage="orchestrator_route_selection",
            selected_candidate_id="business_research_analyst",
            candidate_assessments=[
                DecisionCandidateAssessment(
                    candidate_id="business_research_analyst",
                    disposition="selected",
                    rationale="Owns the requested company research.",
                )
            ],
            reasoning="Selected Business Research.",
        ),
        model_fields_set={"decision"},
    )

    from keystone_agents.agent_decision_contracts import orchestrator_decision_contract

    outcome, _evidence = validate_specialist_decision(
        output,
        orchestrator_decision_contract(),
    )

    assert outcome.status == "repair_required"
    assert outcome.reason_code == "orchestrator_route_target_conflict"


def test_chief_rejects_contradictory_workflow_and_durable_handoff() -> None:
    output = SimpleNamespace(
        recommended_route=SimpleNamespace(workflow_type="gmail-triage"),
        durable_handoff=SimpleNamespace(agent="opportunity_scout"),
        context_handoffs=[],
        decision=AgentDecisionRecord(
            decision_owner="chief_of_staff",
            decision_stage="chief_delegation_selection",
            selected_candidate_ids=[
                "workflow:gmail-triage",
                "opportunity_scout",
            ],
            candidate_assessments=[
                DecisionCandidateAssessment(
                    candidate_id="workflow:gmail-triage",
                    disposition="selected",
                    rationale="Selected a Gmail workflow.",
                ),
                DecisionCandidateAssessment(
                    candidate_id="opportunity_scout",
                    disposition="selected",
                    rationale="Contradictory durable owner from the same output.",
                ),
            ],
            reasoning="Returned incompatible workflow and handoff fields.",
        ),
        model_fields_set={"decision"},
    )

    from keystone_agents.agent_decision_contracts import chief_of_staff_decision_contract

    outcome, _evidence = validate_specialist_decision(
        output,
        chief_of_staff_decision_contract(),
    )

    assert outcome.status == "repair_required"
    assert outcome.reason_code == "chief_workflow_handoff_conflict"


def _gmail_result_payload(
    *,
    message_id: str,
    thread_id: str,
    assessments: list[dict[str, Any]],
    decision_stage: str = "gmail_candidate_selection",
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "thread_id": thread_id,
        "received_at": "2026-08-03T14:00:00Z",
        "subject": "G2i interview invitation for August 4 at 10:00 AM ET",
        "sender_name": "G2i Recruiting",
        "sender_email": "recruiting@example.com",
        "category": "collaboration_opportunity",
        "confidence": 0.94,
        "priority": "high",
        "summary": "Current G2i interview invitation.",
        "reasoning": "The current invitation matches the verified meeting time and is active.",
        "needs_reply": True,
        "recommended_labels": ["Keystone/Triage", "Keystone/Action Required"],
        "risk_flags": [],
        "recommended_next_agent": "human_review",
        "triage_limitations": ["Sanitized Gmail context only."],
        "recommended_action": "Review the Slack-only reply copy.",
        "draft_reply": "Looking forward to it.",
        "draft_created": False,
        "approval_required": True,
        "requires_human_review": True,
        "decision": {
            "decision_owner": "specialist_agent",
            "decision_stage": decision_stage,
            "selected_candidate_id": thread_id,
            "selected_candidate_ids": [thread_id] if thread_id else [],
            "candidate_assessments": assessments,
            "reasoning": "Selected the active invitation and excluded obsolete lifecycle variants.",
            "limitations": ["Only bounded sanitized thread context was read."],
            "needs_more_context": False,
        },
    }


def _gmail_selected_repair_payload(
    triage_result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "repair": {
            "resolution": "selected",
            "triage_result": triage_result,
        }
    }


def _gmail_unresolved_repair_payload() -> dict[str, Any]:
    return {
        "repair": {
            "resolution": "needs_more_context",
            "reasoning": ("The bounded contexts do not identify one conversation strongly enough."),
            "limitations": ["No Gmail identity was selected."],
            "requested_context": ["A participant name or exact interview date"],
        }
    }


def _gmail_capacity_block_payload() -> dict[str, Any]:
    return {
        "category": "unrelated",
        "confidence": 0.2,
        "priority": "normal",
        "summary": "The bounded evidence did not support one verified selection.",
        "reasoning": (
            "The remaining model-request capacity was reserved for this final response."
        ),
        "needs_reply": False,
        "recommended_labels": [],
        "risk_flags": [],
        "triage_limitations": [
            "A corrective query could not leave capacity for a context read and final response."
        ],
        "recommended_action": "Retry only with newly admitted model-request capacity.",
        "draft_reply": None,
        "draft_created": False,
        "approval_required": False,
        "requires_human_review": True,
        "decision": {
            "decision_owner": "specialist_agent",
            "decision_stage": "gmail_candidate_selection",
            "selected_candidate_id": "",
            "selected_candidate_ids": [],
            "candidate_assessments": [],
            "reasoning": (
                "The completed query was insufficient and the corrective query was "
                "capacity-blocked before provider execution."
            ),
            "limitations": ["No Gmail identity was selected."],
            "needs_more_context": True,
        },
    }


def _four_g2i_candidates() -> list[dict[str, Any]]:
    return [
        {
            "id": "msg-current",
            "threadId": "thread-current",
            "received_at": "2026-08-03T14:00:00Z",
            "sender_name": "G2i Recruiting",
            "sender_email": "recruiting@example.com",
            "subject": "G2i interview invitation for August 4 at 10:00 AM ET",
            "snippet": "Your interview is confirmed for tomorrow at 10:00 AM ET.",
            "labelIds": ["INBOX", "UNREAD"],
        },
        {
            "id": "msg-cancelled",
            "threadId": "thread-cancelled",
            "received_at": "2026-08-01T14:00:00Z",
            "sender_name": "G2i Recruiting",
            "sender_email": "recruiting@example.com",
            "subject": "Canceled: G2i interview August 3 at 9:00 AM ET",
            "snippet": "This earlier interview time was canceled.",
            "labelIds": ["INBOX"],
        },
        {
            "id": "msg-obsolete",
            "threadId": "thread-obsolete",
            "received_at": "2026-07-31T14:00:00Z",
            "sender_name": "G2i Recruiting",
            "sender_email": "recruiting@example.com",
            "subject": "G2i interview invitation for August 3 at 9:00 AM ET",
            "snippet": "Original time before rescheduling.",
            "labelIds": ["INBOX"],
        },
        {
            "id": "msg-reminder",
            "threadId": "thread-reminder",
            "received_at": "2026-08-03T16:00:00Z",
            "sender_name": "G2i Recruiting",
            "sender_email": "recruiting@example.com",
            "subject": "Reminder: G2i interview tomorrow",
            "snippet": "Reminder for your upcoming interview.",
            "labelIds": ["INBOX", "UNREAD"],
        },
    ]


def _ten_message_seven_thread_candidates() -> list[dict[str, Any]]:
    base = _four_g2i_candidates()
    return [
        *base,
        {
            **base[0],
            "id": "msg-current-follow-up",
            "snippet": "A follow-up in the current interview conversation.",
        },
        {
            **base[0],
            "id": "msg-current-transcript",
            "snippet": "A transcript share in the current interview conversation.",
        },
        {
            **base[1],
            "id": "msg-cancelled-follow-up",
            "snippet": "A second message in the canceled conversation.",
        },
        *[
            {
                "id": f"msg-unrelated-{index}",
                "threadId": f"thread-unrelated-{index}",
                "received_at": f"2026-08-03T1{index}:00:00Z",
                "sender_name": "Unrelated Sender",
                "sender_email": f"sender-{index}@example.com",
                "subject": f"Unrelated message {index}",
                "snippet": "Not plausibly related to the interview request.",
                "labelIds": ["INBOX"],
            }
            for index in range(3)
        ],
    ]


def _candidate_assessments() -> list[dict[str, Any]]:
    return [
        {
            "candidate_id": "thread-current",
            "disposition": "selected",
            "rationale": "Active invitation matching the current meeting time.",
        },
        {
            "candidate_id": "thread-cancelled",
            "disposition": "excluded",
            "rationale": "Explicitly canceled lifecycle state.",
        },
        {
            "candidate_id": "thread-obsolete",
            "disposition": "excluded",
            "rationale": "Obsolete meeting time before rescheduling.",
        },
        {
            "candidate_id": "thread-reminder",
            "disposition": "excluded",
            "rationale": "Reminder evidence, not the invitation thread to answer.",
        },
    ]


def _patch_gmail_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    values = _four_g2i_candidates()
    by_thread = {value["threadId"]: value for value in values}

    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        lambda **_kwargs: list(values),
    )

    def fake_thread(thread_id: str) -> dict[str, Any]:
        value = by_thread[thread_id]
        return {
            "thread_id": thread_id,
            "message_count": 1,
            "subject": value["subject"],
            "summary": value["snippet"],
            "thread_context": value["snippet"],
            "latest_received_at": value["received_at"],
            "participants": [value["sender_name"]],
            "triage_limitations": ["Synthetic provider fixture."],
        }

    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        fake_thread,
    )


def test_gmail_agent_owns_four_candidate_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gmail_provider(monkeypatch)
    calendar_context = (
        "Verified read-only provider context selected by Chief of Staff: "
        "event_id=event-current; title=G2i interview; "
        "start=2026-08-04 10:00 America/New_York; downstream_agent=gmail_triage."
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {"query": "G2i", "max_results": 10},
                    call_id="query-1",
                )
            ],
            [
                _tool_call(
                    "read_gmail_context",
                    {"resource_type": "thread", "resource_id": thread_id},
                    call_id=f"read-{index}",
                )
                for index, thread_id in enumerate(
                    (
                        "thread-current",
                        "thread-cancelled",
                        "thread-obsolete",
                        "thread-reminder",
                    ),
                    start=1,
                )
            ],
            [
                _structured_message(
                    _gmail_result_payload(
                        message_id="msg-current",
                        thread_id="thread-current",
                        assessments=_candidate_assessments(),
                    )
                )
            ],
        ]
    )

    result = run_gmail_triage_sdk(
        GmailTriageSDKInput(
            subject="",
            body="",
            request=(
                "Find the email tied to tomorrow's G2i interview and give me a short "
                "Slack reply saying I'm looking forward to it. Do not create or send a draft.\n\n"
                + calendar_context
            ),
        ),
        run_config=build_local_run_config(FakeProvider(model)),
        live=True,
        provider_selection_required=True,
    )

    assert result.final_output.thread_id == "thread-current"
    assert result.final_output.decision.decision_owner == "specialist_agent"
    assert len(result.final_output.decision.candidate_assessments) == 4
    telemetry = result.request_cache["decision_ownership"]
    assert telemetry["validator_outcome"]["status"] == "accepted"
    assert telemetry["excluded_candidate_ids"] == [
        "thread-cancelled",
        "thread-obsolete",
        "thread-reminder",
    ]
    assert result.request_cache["tool_execution"]["model_tool_call_count"] == 5
    assert [
        event["event_type"] for event in result.request_cache["decision_ownership"]["events"]
    ] == ["proposed", "validator_result", "terminal"]
    assert len(model.calls) == 3
    assert [call["tool_choice"] for call in model.calls] == [
        "query_gmail_message_summaries",
        None,
        None,
    ]
    assert "event_id=event-current" in str(model.calls[0]["input"])
    assert "2026-08-04 10:00 America/New_York" in str(model.calls[0]["input"])


def test_gmail_selection_assesses_only_the_bounded_contexts_the_agent_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _ten_message_seven_thread_candidates()
    by_thread = {value["threadId"]: value for value in values}
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        lambda **_kwargs: list(values),
    )
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        lambda thread_id: {
            "thread_id": thread_id,
            "message_count": 1,
            "subject": by_thread[thread_id]["subject"],
            "summary": by_thread[thread_id]["snippet"],
            "thread_context": by_thread[thread_id]["snippet"],
            "latest_received_at": by_thread[thread_id]["received_at"],
            "participants": [by_thread[thread_id]["sender_name"]],
            "triage_limitations": ["Synthetic provider fixture."],
        },
    )
    assessments = _candidate_assessments()
    assessments[1] = {
        **assessments[1],
        "candidate_id": "msg-cancelled",
    }
    model = FakeModel(
        [
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {"query": "interview", "max_results": 10},
                    call_id="query-1",
                )
            ],
            [
                _tool_call(
                    "read_gmail_context",
                    {"resource_type": "thread", "resource_id": thread_id},
                    call_id=f"read-{index}",
                )
                for index, thread_id in enumerate(
                    (
                        "thread-current",
                        "thread-cancelled",
                        "thread-obsolete",
                        "thread-reminder",
                    ),
                    start=1,
                )
            ],
            [
                _structured_message(
                    _gmail_result_payload(
                        message_id="msg-current",
                        thread_id="thread-current",
                        assessments=assessments,
                    )
                )
            ],
        ]
    )

    result = run_gmail_triage_sdk(
        GmailTriageSDKInput(
            subject="",
            body="",
            request=(
                "Find the current interview conversation, set aside stale times, "
                "and give me Slack-only reply copy."
            ),
        ),
        run_config=build_local_run_config(FakeProvider(model)),
        live=True,
        provider_selection_required=True,
    )

    telemetry = result.request_cache["decision_ownership"]
    assert telemetry["candidate_count"] == 10
    assert telemetry["query_message_count"] == 10
    assert telemetry["query_thread_count"] == 7
    assert telemetry["decision_candidate_count"] == 4
    assert telemetry["decision_candidate_ids"] == [
        "thread-current",
        "thread-cancelled",
        "thread-obsolete",
        "thread-reminder",
    ]
    assert telemetry["validator_outcome"]["status"] == "accepted"
    assert result.request_cache["tool_execution"]["model_tool_call_count"] == 5


def test_gmail_agent_recovers_in_loop_when_fifth_context_read_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _ten_message_seven_thread_candidates()
    by_thread = {value["threadId"]: value for value in values}
    provider_context_reads: list[str] = []
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        lambda **_kwargs: list(values),
    )

    def get_thread(thread_id: str) -> dict[str, Any]:
        provider_context_reads.append(thread_id)
        value = by_thread[thread_id]
        return {
            "thread_id": thread_id,
            "message_count": 1,
            "subject": value["subject"],
            "summary": value["snippet"],
            "thread_context": value["snippet"],
            "latest_received_at": value["received_at"],
            "participants": [value["sender_name"]],
            "triage_limitations": ["Synthetic provider fixture."],
        }

    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        get_thread,
    )
    bounded_threads = (
        "thread-current",
        "thread-cancelled",
        "thread-obsolete",
        "thread-reminder",
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {"query": "interview", "max_results": 10},
                    call_id="query-1",
                )
            ],
            [
                _tool_call(
                    "read_gmail_context",
                    {"resource_type": "thread", "resource_id": thread_id},
                    call_id=f"read-{index}",
                )
                for index, thread_id in enumerate(bounded_threads, start=1)
            ],
            [
                _tool_call(
                    "read_gmail_context",
                    {
                        "resource_type": "thread",
                        "resource_id": "thread-unrelated-0",
                    },
                    call_id="read-5",
                )
            ],
            [
                _structured_message(
                    _gmail_result_payload(
                        message_id="msg-current",
                        thread_id="thread-current",
                        assessments=_candidate_assessments(),
                    )
                )
            ],
        ]
    )

    result = run_gmail_triage_sdk(
        GmailTriageSDKInput(
            subject="",
            body="",
            request=(
                "Find the active interview conversation, set aside stale or canceled "
                "threads, and give me a short Slack-only reply."
            ),
        ),
        run_config=build_local_run_config(FakeProvider(model)),
        live=True,
        provider_selection_required=True,
    )

    telemetry = result.request_cache["decision_ownership"]
    assert len(provider_context_reads) == 4
    assert set(provider_context_reads) == set(bounded_threads)
    assert telemetry["context_read_call_count"] == 4
    assert telemetry["context_provider_read_count"] == 4
    assert telemetry["model_context_read_call_count"] == 5
    assert telemetry["blocked_context_read_call_count"] == 1
    assert telemetry["validator_outcome"]["status"] == "accepted"
    assert result.final_output.thread_id == "thread-current"
    assert result.request_cache["tool_execution"]["model_tool_call_count"] == 6
    assert "gmail_context_read_budget_exhausted" in str(model.calls[-1]["input"])


def test_gmail_ten_summary_repair_succeeds_without_provider_reread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _ten_message_seven_thread_candidates()
    by_thread = {value["threadId"]: value for value in values}
    query_calls: list[dict[str, Any]] = []
    context_reads: list[str] = []

    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")

    def search_message_summaries(**kwargs: Any) -> list[dict[str, Any]]:
        query_calls.append(dict(kwargs))
        return list(values)

    def get_thread(thread_id: str) -> dict[str, Any]:
        context_reads.append(thread_id)
        value = by_thread[thread_id]
        return {
            "thread_id": thread_id,
            "message_count": 1,
            "subject": value["subject"],
            "summary": value["snippet"],
            "thread_context": value["snippet"],
            "latest_received_at": value["received_at"],
            "participants": [value["sender_name"]],
            "triage_limitations": ["Synthetic provider fixture."],
        }

    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        search_message_summaries,
    )
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        get_thread,
    )
    incomplete = _gmail_result_payload(
        message_id="msg-current",
        thread_id="thread-current",
        assessments=_candidate_assessments()[:2],
    )
    repaired = _gmail_result_payload(
        message_id="msg-current",
        thread_id="thread-current",
        assessments=_candidate_assessments(),
    )
    read_threads = (
        "thread-current",
        "thread-cancelled",
        "thread-obsolete",
        "thread-reminder",
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {"query": "interview", "max_results": 10},
                    call_id="query-1",
                )
            ],
            [
                _tool_call(
                    "read_gmail_context",
                    {"resource_type": "thread", "resource_id": thread_id},
                    call_id=f"read-{index}",
                )
                for index, thread_id in enumerate(read_threads, start=1)
            ],
            [_structured_message(incomplete)],
            [_structured_message(_gmail_selected_repair_payload(repaired))],
        ]
    )

    result = run_gmail_triage_sdk(
        GmailTriageSDKInput(
            subject="",
            body="",
            request=(
                "Find the current interview conversation, ignore stale times, and "
                "write a short Slack-only thank-you reply."
            ),
        ),
        run_config=build_local_run_config(FakeProvider(model)),
        live=True,
        provider_selection_required=True,
    )

    telemetry = result.request_cache["decision_ownership"]
    assert len(query_calls) == 1
    assert sorted(context_reads) == sorted(read_threads)
    assert len(model.calls) == 4
    assert model.calls[-1]["tool_names"] == []
    assert telemetry["query_message_count"] == 10
    assert telemetry["query_thread_count"] == 7
    assert telemetry["decision_candidate_count"] == 4
    assert telemetry["validator_outcome"]["status"] == "accepted"
    assert telemetry["validator_outcome"]["repair_attempted"] is True
    assert result.request_cache["decision_repairs"] == 1
    assert result.request_cache["tool_execution"]["model_tool_call_count"] == 5


def test_gmail_corrective_query_evidence_survives_tool_free_selection_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {value["threadId"]: value for value in _four_g2i_candidates()}
    query_calls: list[str] = []
    context_reads: list[str] = []
    raw_request = (
        "Can you find the G2i conversation for the interview that is actually "
        "happening and give me a short Slack reply saying I am looking forward to it?"
    )
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")

    def search_message_summaries(**kwargs: Any) -> list[dict[str, Any]]:
        query = str(kwargs.get("query") or "")
        query_calls.append(query)
        if query == "G2i interview":
            return [values["thread-reminder"]]
        return [values["thread-current"]]

    def get_thread(thread_id: str) -> dict[str, Any]:
        context_reads.append(thread_id)
        value = values[thread_id]
        return {
            "thread_id": thread_id,
            "message_count": 1,
            "subject": value["subject"],
            "summary": value["snippet"],
            "thread_context": value["snippet"],
            "latest_received_at": value["received_at"],
            "participants": [value["sender_name"]],
            "triage_limitations": ["Synthetic provider fixture."],
        }

    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        search_message_summaries,
    )
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        get_thread,
    )
    incomplete = _gmail_result_payload(
        message_id="msg-current",
        thread_id="thread-current",
        assessments=[
            {
                "candidate_id": "thread-current",
                "disposition": "selected",
                "rationale": "The confirmed invitation is current.",
            }
        ],
    )
    repaired = _gmail_result_payload(
        message_id="msg-current",
        thread_id="thread-current",
        assessments=[
            {
                "candidate_id": "thread-current",
                "disposition": "selected",
                "rationale": "The confirmed invitation is the conversation to answer.",
            },
            {
                "candidate_id": "thread-reminder",
                "disposition": "excluded",
                "rationale": "The reminder is related but is not the invitation thread.",
            },
        ],
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {"query": "G2i interview", "max_results": 10},
                    call_id="query-1",
                )
            ],
            [
                _tool_call(
                    "read_gmail_context",
                    {"resource_type": "thread", "resource_id": "thread-reminder"},
                    call_id="read-reminder",
                )
            ],
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {"query": "G2i confirmed interview time", "max_results": 10},
                    call_id="query-2",
                )
            ],
            [
                _tool_call(
                    "read_gmail_context",
                    {"resource_type": "thread", "resource_id": "thread-current"},
                    call_id="read-current",
                )
            ],
            [_structured_message(incomplete)],
            [_structured_message(_gmail_selected_repair_payload(repaired))],
        ]
    )

    result = run_gmail_triage_sdk(
        GmailTriageSDKInput(subject="", body="", request=raw_request),
        run_config=build_local_run_config(FakeProvider(model)),
        live=True,
        provider_selection_required=True,
    )

    telemetry = result.request_cache["decision_ownership"]
    repair_input = json.dumps(model.calls[-1]["input"], default=str)
    assert query_calls == ["G2i interview", "G2i confirmed interview time"]
    assert context_reads == ["thread-reminder", "thread-current"]
    assert len(model.calls) == 6
    assert model.calls[-1]["tool_names"] == []
    assert raw_request in repair_input
    assert "thread-reminder" in repair_input
    assert "thread-current" in repair_input
    assert telemetry["query_call_count"] == 2
    assert telemetry["corrective_query_count"] == 1
    assert telemetry["decision_candidate_ids"] == [
        "thread-reminder",
        "thread-current",
    ]
    assert telemetry["validator_outcome"]["status"] == "accepted"
    assert telemetry["validator_outcome"]["repair_attempted"] is True


def test_gmail_loop_returns_capacity_partial_without_starting_unfunded_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_queries: list[str] = []
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")

    def search_message_summaries(**kwargs: Any) -> list[dict[str, Any]]:
        query = str(kwargs.get("query") or "")
        provider_queries.append(query)
        return [
            {
                "id": "msg-adjacent",
                "threadId": "thread-adjacent",
                "received_at": "2026-09-14T14:00:00Z",
                "sender_name": "Synthetic Sender",
                "sender_email": "sender@example.test",
                "subject": "Adjacent pilot note",
                "snippet": "This is not the requested ORBIT announcement.",
            }
        ]

    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        search_message_summaries,
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {"query": "Example Health pilot", "max_results": 10},
                    call_id="query-1",
                )
            ],
            [_structured_message(_gmail_capacity_block_payload())],
        ]
    )

    with activate_model_request_budget(3) as ledger:
        result = run_gmail_triage_sdk(
            GmailTriageSDKInput(
                subject="",
                body="",
                request="Find the Example Health ORBIT pilot announcement.",
                gmail_query_hint='subject:"Example Health joined the ORBIT pilot"',
            ),
            run_config=build_local_run_config(FakeProvider(model)),
            live=True,
            provider_selection_required=True,
        )
        snapshot = ledger.snapshot()

    assert len(model.calls) == 2
    assert provider_queries == ["Example Health pilot"]
    assert result.final_output.decision.needs_more_context is True
    telemetry = result.request_cache["decision_ownership"]
    assert telemetry["query_call_count"] == 1
    assert telemetry["model_query_call_count"] == 1
    assert telemetry["blocked_query_call_count"] == 0
    assert telemetry["validator_outcome"]["reason_code"] == "agent_requested_more_context"
    assert '"corrective_query_allowed": false' in str(model.calls[1]["input"])
    assert snapshot["consumed"] == 2
    assert snapshot["remaining"] == 1
    assert snapshot["exhausted"] is False


def test_gmail_loop_completes_straight_query_read_answer_in_three_child_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_queries: list[str] = []
    context_reads: list[str] = []
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")

    def search_message_summaries(**kwargs: Any) -> list[dict[str, Any]]:
        provider_queries.append(str(kwargs.get("query") or ""))
        return [
            {
                "id": "msg-current",
                "threadId": "thread-current",
                "received_at": "2026-09-14T16:00:00Z",
                "sender_name": "Example Health",
                "sender_email": "news@example.test",
                "subject": "Example Health ORBIT pilot announcement",
                "snippet": "Current outcomes-based synthetic pilot announcement.",
            }
        ]

    def get_thread(thread_id: str) -> dict[str, Any]:
        context_reads.append(thread_id)
        return {
            "thread_id": thread_id,
            "message_count": 1,
            "subject": "Example Health ORBIT pilot announcement",
            "summary": "Verified current announcement.",
            "thread_context": "Verified bounded context for the current announcement.",
            "latest_received_at": "2026-09-14T16:00:00Z",
            "participants": ["Example Health"],
        }

    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        search_message_summaries,
    )
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        get_thread,
    )
    selected = _gmail_result_payload(
        message_id="",
        thread_id="thread-current",
        assessments=[
            {
                "candidate_id": "thread-current",
                "disposition": "selected",
                "rationale": "The exact subject and read context match the request.",
            }
        ],
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {
                        "query": 'subject:"Example Health ORBIT pilot announcement"',
                        "max_results": 5,
                    },
                    call_id="query-1",
                )
            ],
            [
                _tool_call(
                    "read_gmail_context",
                    {"resource_type": "thread", "resource_id": "thread-current"},
                    call_id="read-current",
                )
            ],
            [_structured_message(selected)],
        ]
    )

    with activate_model_request_budget(3) as ledger:
        result = run_gmail_triage_sdk(
            GmailTriageSDKInput(
                subject="",
                body="",
                request="Find the exact Example Health ORBIT pilot announcement.",
                gmail_query_hint='subject:"Example Health ORBIT pilot announcement"',
            ),
            run_config=build_local_run_config(FakeProvider(model)),
            live=True,
            provider_selection_required=True,
        )
        snapshot = ledger.snapshot()

    assert len(model.calls) == 3
    assert provider_queries == ['subject:"Example Health ORBIT pilot announcement"']
    assert context_reads == ["thread-current"]
    assert result.final_output.thread_id == "thread-current"
    assert result.request_cache["decision_ownership"]["validator_outcome"]["status"] == (
        "accepted"
    )
    assert '"candidate_read_and_final_allowed": true' in str(model.calls[1]["input"])
    assert '"must_return_final_response_now": true' in str(model.calls[2]["input"])
    assert snapshot["consumed"] == 3
    assert snapshot["remaining"] == 0
    assert snapshot["exhausted"] is False


def test_saved_two_query_read_sequence_exhausts_three_child_requests_before_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_queries: list[str] = []
    context_reads: list[str] = []
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")

    def search_message_summaries(**kwargs: Any) -> list[dict[str, Any]]:
        query = str(kwargs.get("query") or "")
        provider_queries.append(query)
        suffix = "current" if "ORBIT" in query else "adjacent"
        return [
            {
                "id": f"msg-{suffix}",
                "threadId": f"thread-{suffix}",
                "subject": query,
                "snippet": f"Bounded {suffix} candidate.",
            }
        ]

    def get_thread(thread_id: str) -> dict[str, Any]:
        context_reads.append(thread_id)
        return {
            "thread_id": thread_id,
            "message_count": 1,
            "subject": "Example Health ORBIT",
            "summary": "Verified current context.",
            "thread_context": "Verified current context.",
        }

    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        search_message_summaries,
    )
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        get_thread,
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {"query": "Example Health pilot", "max_results": 10},
                    call_id="query-1",
                )
            ],
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {"query": "Example Health ORBIT", "max_results": 10},
                    call_id="query-2",
                )
            ],
            [
                _tool_call(
                    "read_gmail_context",
                    {"resource_type": "thread", "resource_id": "thread-current"},
                    call_id="read-current",
                )
            ],
            [_structured_message(_gmail_capacity_block_payload())],
        ]
    )

    with activate_model_request_budget(3) as ledger:
        with pytest.raises(ModelRequestBudgetExhausted) as exc_info:
            run_gmail_triage_sdk(
                GmailTriageSDKInput(
                    subject="",
                    body="",
                    request="Find the Example Health ORBIT pilot announcement.",
                ),
                run_config=build_local_run_config(FakeProvider(model)),
                live=True,
                provider_selection_required=True,
            )
        snapshot = ledger.snapshot()

    metadata = sdk_run_failure_metadata(exc_info.value)
    assert metadata["failure_kind"] == "modelrequestbudgetexhausted"
    assert len(model.calls) == 3
    assert provider_queries == ["Example Health pilot", "Example Health ORBIT"]
    assert context_reads == ["thread-current"]
    assert metadata["usage"]["requests"] == 3
    postcondition = metadata["tool_execution_postcondition"]
    assert postcondition["satisfied"] is True
    assert postcondition["missing_groups"] == []
    assert "query_gmail_message_summaries" in postcondition["receipt_tool_names"]
    assert snapshot["consumed"] == 3
    assert snapshot["remaining"] == 0
    assert snapshot["exhausted"] is True


def test_gmail_loop_completes_correction_read_and_synthesis_when_budget_admits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_queries: list[str] = []
    context_reads: list[str] = []
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")

    def search_message_summaries(**kwargs: Any) -> list[dict[str, Any]]:
        query = str(kwargs.get("query") or "")
        provider_queries.append(query)
        if query == "Example Health ORBIT":
            return [
                {
                    "id": "msg-current",
                    "threadId": "thread-current",
                    "received_at": "2026-09-14T16:00:00Z",
                    "sender_name": "Example Health",
                    "sender_email": "news@example.test",
                    "subject": "Example Health ORBIT pilot announcement",
                    "snippet": "Current outcomes-based synthetic pilot announcement.",
                }
            ]
        return [
            {
                "id": "msg-adjacent",
                "threadId": "thread-adjacent",
                "received_at": "2026-09-14T14:00:00Z",
                "sender_name": "Synthetic Sender",
                "sender_email": "sender@example.test",
                "subject": "Adjacent pilot note",
                "snippet": "Not the requested ORBIT announcement.",
            }
        ]

    def get_thread(thread_id: str) -> dict[str, Any]:
        context_reads.append(thread_id)
        return {
            "thread_id": thread_id,
            "message_count": 1,
            "subject": "Example Health ORBIT pilot announcement",
            "summary": "Verified current announcement.",
            "thread_context": "Verified bounded context for the current announcement.",
            "latest_received_at": "2026-09-14T16:00:00Z",
            "participants": ["Example Health"],
        }

    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        search_message_summaries,
    )
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        get_thread,
    )
    selected = _gmail_result_payload(
        message_id="",
        thread_id="thread-current",
        assessments=[
            {
                "candidate_id": "thread-current",
                "disposition": "selected",
                "rationale": "The read context matches the requested ORBIT announcement.",
            }
        ],
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {"query": "Example Health pilot", "max_results": 10},
                    call_id="query-1",
                )
            ],
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {"query": "Example Health ORBIT", "max_results": 10},
                    call_id="query-2",
                )
            ],
            [
                _tool_call(
                    "read_gmail_context",
                    {"resource_type": "thread", "resource_id": "thread-current"},
                    call_id="read-current",
                )
            ],
            [_structured_message(selected)],
        ]
    )

    with activate_model_request_budget(4) as ledger:
        result = run_gmail_triage_sdk(
            GmailTriageSDKInput(
                subject="",
                body="",
                request="Find the Example Health ORBIT pilot announcement.",
            ),
            run_config=build_local_run_config(FakeProvider(model)),
            live=True,
            provider_selection_required=True,
        )
        snapshot = ledger.snapshot()

    assert len(model.calls) == 4
    assert provider_queries == ["Example Health pilot", "Example Health ORBIT"]
    assert context_reads == ["thread-current"]
    assert result.final_output.thread_id == "thread-current"
    assert result.request_cache["decision_ownership"]["validator_outcome"]["status"] == (
        "accepted"
    )
    assert '"corrective_query_allowed": true' in str(model.calls[1]["input"])
    assert '"candidate_read_and_final_allowed": true' in str(model.calls[2]["input"])
    assert '"must_return_final_response_now": true' in str(model.calls[3]["input"])
    assert snapshot["consumed"] == 4
    assert snapshot["remaining"] == 0
    assert snapshot["exhausted"] is False


def test_gmail_duplicate_empty_query_recovers_without_repeating_provider_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = {
        value["threadId"]: value for value in _four_g2i_candidates()
    }["thread-current"]
    provider_queries: list[str] = []
    context_reads: list[str] = []
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")

    def search_message_summaries(**kwargs: Any) -> list[dict[str, Any]]:
        query = str(kwargs.get("query") or "")
        provider_queries.append(query)
        return [current] if "g2i.co" in query else []

    def get_thread(thread_id: str) -> dict[str, Any]:
        context_reads.append(thread_id)
        return {
            "thread_id": thread_id,
            "message_count": 1,
            "subject": current["subject"],
            "summary": current["snippet"],
            "thread_context": current["snippet"],
            "latest_received_at": current["received_at"],
            "participants": [current["sender_name"]],
        }

    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        search_message_summaries,
    )
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        get_thread,
    )
    selected = _gmail_result_payload(
        message_id="msg-current",
        thread_id="thread-current",
        assessments=[
            {
                "candidate_id": "thread-current",
                "disposition": "selected",
                "rationale": "The confirmed invitation is the current conversation.",
            }
        ],
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {"query": "recent interview", "max_results": 10},
                    call_id="query-1",
                )
            ],
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {"query": "recent interview", "max_results": 10},
                    call_id="query-repeat",
                )
            ],
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {
                        "query": "from:(g2i.co) interview confirmation",
                        "max_results": 10,
                    },
                    call_id="query-2",
                )
            ],
            [
                _tool_call(
                    "read_gmail_context",
                    {"resource_type": "thread", "resource_id": "thread-current"},
                    call_id="read-current",
                )
            ],
            [_structured_message(selected)],
        ]
    )

    result = run_gmail_triage_sdk(
        GmailTriageSDKInput(
            subject="",
            body="",
            request=(
                "Find the recent interview conversation where they confirmed "
                "receiving my follow-up and tell me if another reply is warranted."
            ),
        ),
        run_config=build_local_run_config(FakeProvider(model)),
        live=True,
        provider_selection_required=True,
    )

    telemetry = result.request_cache["decision_ownership"]
    assert provider_queries == [
        "recent interview",
        "from:(g2i.co) interview confirmation",
    ]
    assert context_reads == ["thread-current"]
    assert telemetry["query_call_count"] == 2
    assert telemetry["model_query_call_count"] == 3
    assert telemetry["repeated_query_call_count"] == 1
    assert telemetry["corrective_query_count"] == 1
    assert telemetry["validator_outcome"]["status"] == "accepted"
    assert all("query" not in attempt for attempt in telemetry["query_attempts"])
    assert result.request_cache["tool_corrections"] == 1


def test_gmail_contradictory_message_thread_pair_repairs_to_canonical_thread_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _four_g2i_candidates()
    by_thread = {value["threadId"]: value for value in values}
    query_calls: list[dict[str, Any]] = []
    context_reads: list[str] = []
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")

    def search_message_summaries(**kwargs: Any) -> list[dict[str, Any]]:
        query_calls.append(dict(kwargs))
        return list(values[:2])

    def get_thread(thread_id: str) -> dict[str, Any]:
        context_reads.append(thread_id)
        value = by_thread[thread_id]
        return {
            "thread_id": thread_id,
            "message_count": 1,
            "subject": value["subject"],
            "summary": value["snippet"],
            "thread_context": value["snippet"],
            "latest_received_at": value["received_at"],
            "participants": [value["sender_name"]],
            "triage_limitations": ["Synthetic provider fixture."],
        }

    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        search_message_summaries,
    )
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        get_thread,
    )
    initial = _gmail_result_payload(
        message_id="msg-cancelled",
        thread_id="thread-current",
        assessments=_candidate_assessments()[:2],
    )
    repaired = _gmail_result_payload(
        message_id="",
        thread_id="thread-current",
        assessments=_candidate_assessments()[:2],
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {"query": "G2i interview", "max_results": 10},
                    call_id="query-1",
                )
            ],
            [
                _tool_call(
                    "read_gmail_context",
                    {"resource_type": "thread", "resource_id": thread_id},
                    call_id=f"read-{index}",
                )
                for index, thread_id in enumerate(
                    ("thread-current", "thread-cancelled"),
                    start=1,
                )
            ],
            [_structured_message(initial)],
            [_structured_message(_gmail_selected_repair_payload(repaired))],
        ]
    )

    result = run_gmail_triage_sdk(
        GmailTriageSDKInput(
            subject="",
            body="",
            request="Find today's G2i interview thread and write Slack-only reply copy.",
        ),
        run_config=build_local_run_config(FakeProvider(model)),
        live=True,
        provider_selection_required=True,
    )

    assert result.final_output.thread_id == "thread-current"
    assert result.final_output.message_id == ""
    assert result.final_output.decision.selected_candidate_id == "thread-current"
    assert len(query_calls) == 1
    assert sorted(context_reads) == ["thread-cancelled", "thread-current"]
    validator = result.request_cache["decision_ownership"]["validator_outcome"]
    assert validator["status"] == "accepted"
    assert validator["reason_code"] == "agent_selection_bound_to_verified_candidate_set"
    assert validator["repair_attempted"] is True
    repair_input = str(model.calls[-1]["input"])
    assert "leave message_id blank" in repair_input
    assert model.calls[-1]["tool_names"] == []
    assert result.request_cache["repair_stage_request_cache"]["session_attached"] is False


def test_gmail_read_normalizes_returned_message_id_and_single_flights_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _four_g2i_candidates()
    provider_reads: list[str] = []
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        lambda **_kwargs: list(values),
    )

    def get_thread(thread_id: str) -> dict[str, Any]:
        provider_reads.append(thread_id)
        assert thread_id == "thread-current"
        return {
            "thread_id": thread_id,
            "message_count": 2,
            "subject": "Current interview conversation",
            "summary": "Interview link token=redactionfixture123 is available.",
            "thread_context": "Current invitation and follow-up.",
            "latest_received_at": "2026-08-03T14:00:00Z",
            "participants": ["G2i Recruiting"],
            "triage_limitations": ["Synthetic provider fixture."],
        }

    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        get_thread,
    )
    selected = _gmail_result_payload(
        message_id="msg-current",
        thread_id="thread-current",
        assessments=[_candidate_assessments()[0]],
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {"query": "current interview", "max_results": 10},
                    call_id="query-1",
                )
            ],
            [
                _tool_call(
                    "read_gmail_context",
                    {"resource_type": "thread", "resource_id": "thread-current"},
                    call_id="read-thread",
                ),
                _tool_call(
                    "read_gmail_context",
                    {"resource_type": "thread", "resource_id": "msg-current"},
                    call_id="read-message-as-thread",
                ),
            ],
            [_structured_message(selected)],
        ]
    )

    result = run_gmail_triage_sdk(
        GmailTriageSDKInput(
            subject="",
            body="",
            request="Find the current interview conversation and prepare Slack-only copy.",
        ),
        run_config=build_local_run_config(FakeProvider(model)),
        live=True,
        provider_selection_required=True,
    )

    assert provider_reads == ["thread-current"]
    serialized_items = str(result.raw_result.new_items)
    assert "redactionfixture123" not in serialized_items
    assert "redacted secret-like value" in serialized_items
    telemetry = result.request_cache["decision_ownership"]
    assert telemetry["decision_candidate_ids"] == ["thread-current"]
    assert telemetry["validator_outcome"]["status"] == "accepted"


def test_gmail_needs_more_context_repair_uses_mutually_exclusive_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gmail_provider(monkeypatch)
    contradictory = _gmail_result_payload(
        message_id="msg-current",
        thread_id="thread-current",
        assessments=_candidate_assessments(),
    )
    contradictory["decision"]["needs_more_context"] = True
    model = FakeModel(
        [
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {"query": "interview", "max_results": 10},
                    call_id="query-1",
                )
            ],
            [
                _tool_call(
                    "read_gmail_context",
                    {"resource_type": "thread", "resource_id": thread_id},
                    call_id=f"read-{index}",
                )
                for index, thread_id in enumerate(
                    (
                        "thread-current",
                        "thread-cancelled",
                        "thread-obsolete",
                        "thread-reminder",
                    ),
                    start=1,
                )
            ],
            [_structured_message(contradictory)],
            [_structured_message(_gmail_unresolved_repair_payload())],
        ]
    )

    result = run_gmail_triage_sdk(
        GmailTriageSDKInput(
            subject="",
            body="",
            request="Find the interview conversation and write Slack-only reply copy.",
        ),
        run_config=build_local_run_config(FakeProvider(model)),
        live=True,
        provider_selection_required=True,
    )

    assert result.final_output.decision.needs_more_context is True
    assert result.final_output.message_id == ""
    assert result.final_output.thread_id == ""
    assert result.final_output.draft_reply is None
    assert result.final_output.recommended_labels == []
    assert result.request_cache["decision_ownership"]["validator_outcome"]["status"] == ("accepted")
    repair_input = str(model.calls[-1]["input"])
    assert "mutually exclusive" in repair_input
    assert "conflicting_output_fields" in repair_input
    assert model.calls[-1]["tool_names"] == []


def test_gmail_selection_rejects_conflicting_message_and_thread_assessments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gmail_provider(monkeypatch)
    assessments = [
        *_candidate_assessments(),
        {
            "candidate_id": "msg-cancelled",
            "disposition": "plausible",
            "rationale": "Conflicts with the excluded disposition for this thread.",
        },
    ]
    model = FakeModel(
        [
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {"query": "interview", "max_results": 10},
                    call_id="query-1",
                )
            ],
            [
                _tool_call(
                    "read_gmail_context",
                    {"resource_type": "thread", "resource_id": thread_id},
                    call_id=f"read-{index}",
                )
                for index, thread_id in enumerate(
                    (
                        "thread-current",
                        "thread-cancelled",
                        "thread-obsolete",
                        "thread-reminder",
                    ),
                    start=1,
                )
            ],
            [
                _structured_message(
                    _gmail_result_payload(
                        message_id="msg-current",
                        thread_id="thread-current",
                        assessments=assessments,
                    )
                )
            ],
        ]
    )

    with pytest.raises(GmailAgentDecisionError) as exc_info:
        run_gmail_triage_sdk(
            GmailTriageSDKInput(
                subject="",
                body="",
                request="Find the current interview conversation.",
            ),
            run_config=build_local_run_config(FakeProvider(model)),
            live=True,
            provider_selection_required=True,
            repair_invalid_selection=False,
        )

    outcome = exc_info.value.telemetry["validator_outcome"]
    assert outcome["reason_code"] == "contradictory_candidate_assessments"
    assert outcome["selected_identity_in_candidate_set"] is True
    assert outcome["selected_identity_was_read"] is True


def test_invalid_gmail_selection_gets_one_tool_free_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gmail_provider(monkeypatch)
    invalid = _gmail_result_payload(
        message_id="fabricated-message",
        thread_id="fabricated-thread",
        assessments=[
            {
                "candidate_id": "fabricated-thread",
                "disposition": "selected",
                "rationale": "Invalid model selection for repair test.",
            }
        ],
    )
    repaired = _gmail_result_payload(
        message_id="msg-current",
        thread_id="thread-current",
        assessments=_candidate_assessments(),
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {"query": "G2i", "max_results": 10},
                    call_id="query-1",
                )
            ],
            [
                _tool_call(
                    "read_gmail_context",
                    {"resource_type": "thread", "resource_id": thread_id},
                    call_id=f"read-{index}",
                )
                for index, thread_id in enumerate(
                    (
                        "thread-current",
                        "thread-cancelled",
                        "thread-obsolete",
                        "thread-reminder",
                    ),
                    start=1,
                )
            ],
            [_structured_message(invalid)],
            [_structured_message(_gmail_selected_repair_payload(repaired))],
        ]
    )

    result = run_gmail_triage_sdk(
        GmailTriageSDKInput(subject="", body="", request="Find the current G2i email."),
        run_config=build_local_run_config(FakeProvider(model)),
        live=True,
        provider_selection_required=True,
    )

    assert result.final_output.thread_id == "thread-current"
    telemetry = result.request_cache["decision_ownership"]
    assert telemetry["validator_outcome"]["status"] == "accepted"
    assert telemetry["validator_outcome"]["repair_attempted"] is True
    assert [stage["tool_mode"] for stage in telemetry["model_stages"]] == [
        "model_called",
        "verified_context_tool_free",
    ]
    assert model.calls[-1]["tool_names"] == []
    assert len(model.calls) == 4
    assert [event["event_type"] for event in telemetry["events"]] == [
        "proposed",
        "validator_result",
        "repair_proposed",
        "validator_result",
        "terminal",
    ]


def test_gmail_repair_stamps_fixed_stage_without_changing_agent_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gmail_provider(monkeypatch)
    unread_selection = _gmail_result_payload(
        message_id="msg-obsolete",
        thread_id="thread-obsolete",
        assessments=[
            {
                "candidate_id": "thread-obsolete",
                "disposition": "selected",
                "rationale": "This candidate was not read and must be repaired.",
            }
        ],
    )
    repaired = _gmail_result_payload(
        message_id="msg-current",
        thread_id="thread-current",
        assessments=[_candidate_assessments()[0]],
        decision_stage="specialist_selection",
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    "query_gmail_message_summaries",
                    {"query": "G2i", "max_results": 10},
                    call_id="query-1",
                )
            ],
            [
                _tool_call(
                    "read_gmail_context",
                    {"resource_type": "thread", "resource_id": "thread-current"},
                    call_id="read-current",
                )
            ],
            [_structured_message(unread_selection)],
            [_structured_message(_gmail_selected_repair_payload(repaired))],
        ]
    )

    result = run_gmail_triage_sdk(
        GmailTriageSDKInput(subject="", body="", request="Find the current G2i email."),
        run_config=build_local_run_config(FakeProvider(model)),
        live=True,
        provider_selection_required=True,
    )

    assert result.final_output.thread_id == "thread-current"
    assert result.final_output.decision.selected_candidate_id == "thread-current"
    assert result.final_output.decision.decision_stage == "gmail_candidate_selection"
    telemetry = result.request_cache["decision_ownership"]
    assert telemetry["model_stages"][0]["reason_code"] == "selected_candidate_not_read"
    assert telemetry["validator_outcome"]["status"] == "accepted"
    assert telemetry["validator_outcome"]["repair_attempted"] is True
    assert telemetry["repair_metadata_normalizations"] == [
        {
            "field": "decision_stage",
            "reported_value": "specialist_selection",
            "canonical_value": "gmail_candidate_selection",
            "semantic_selection_changed": False,
        }
    ]
    assert model.calls[-1]["tool_names"] == []


def test_verified_gmail_continuation_reads_exact_thread_without_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gmail_provider(monkeypatch)
    model = FakeModel(
        [
            [
                _tool_call(
                    "read_gmail_context",
                    {
                        "resource_type": "thread",
                        "resource_id": "thread-current",
                    },
                    call_id="read-continuation",
                )
            ],
            [
                _structured_message(
                    _gmail_result_payload(
                        message_id="msg-current",
                        thread_id="thread-current",
                        assessments=[_candidate_assessments()[0]],
                        decision_stage="gmail_verified_continuation",
                    )
                )
            ],
        ]
    )

    result = run_gmail_triage_sdk(
        GmailTriageSDKInput(
            subject="",
            body="",
            message_id="msg-current",
            thread_id="thread-current",
            request=(
                "Create the same good Gmail draft on the verified thread, but do not send it."
            ),
        ),
        run_config=build_local_run_config(FakeProvider(model)),
        live=True,
        provider_selection_required=False,
        provider_context_read_required=True,
    )

    assert result.final_output.thread_id == "thread-current"
    assert result.request_cache["decision_ownership"]["validator_outcome"]["status"] == ("accepted")
    tool_execution = result.request_cache["tool_execution"]
    assert tool_execution["model_called_tool_names"] == ["read_gmail_context"]
    assert "query_gmail_message_summaries" not in tool_execution["model_called_tool_names"]


def test_invalid_verified_gmail_continuation_gets_one_tool_free_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gmail_provider(monkeypatch)
    provider_reads: list[str] = []
    original_get_thread = gmail_query_tools.gmail_tool.get_thread_with_source_url

    def counted_get_thread(thread_id: str) -> dict[str, Any]:
        provider_reads.append(thread_id)
        return original_get_thread(thread_id)

    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        counted_get_thread,
    )
    invalid = _gmail_result_payload(
        message_id="fabricated-message",
        thread_id="fabricated-thread",
        assessments=[
            {
                "candidate_id": "fabricated-thread",
                "disposition": "selected",
                "rationale": "Invalid continuation identity.",
            }
        ],
        decision_stage="gmail_verified_continuation",
    )
    repaired = _gmail_result_payload(
        message_id="msg-current",
        thread_id="thread-current",
        assessments=[_candidate_assessments()[0]],
        decision_stage="gmail_verified_continuation",
    )
    model = FakeModel(
        outputs=[
            [
                _tool_call(
                    "read_gmail_context",
                    {
                        "resource_type": "thread",
                        "resource_id": "thread-current",
                    },
                    call_id="read-continuation",
                )
            ],
            [_structured_message(invalid)],
            [_structured_message(repaired)],
        ]
    )
    request = "Use the same thread and prepare the reply, but do not send it."

    result = run_gmail_triage_sdk(
        GmailTriageSDKInput(
            subject="",
            body="",
            message_id="msg-current",
            thread_id="thread-current",
            request=request,
        ),
        run_config=build_local_run_config(FakeProvider(model)),
        live=True,
        provider_selection_required=False,
        provider_context_read_required=True,
    )

    assert result.final_output.thread_id == "thread-current"
    assert provider_reads == ["thread-current"]
    assert len(model.calls) == 3
    assert model.calls[-1]["tool_names"] == []
    assert request in str(model.calls[-1]["input"])
    assert "Your interview is confirmed for tomorrow" in str(model.calls[-1]["input"])
    telemetry = result.request_cache["decision_ownership"]
    assert telemetry["validator_outcome"]["status"] == "accepted"
    assert telemetry["validator_outcome"]["repair_attempted"] is True
    assert [stage["tool_mode"] for stage in telemetry["model_stages"]] == [
        "model_called",
        "verified_context_tool_free",
    ]
    called_tools = result.request_cache["tool_execution"]["model_called_tool_names"]
    assert called_tools == ["read_gmail_context"]


def test_verified_gmail_continuation_repair_exhaustion_does_not_requery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gmail_provider(monkeypatch)
    provider_reads: list[str] = []
    original_get_thread = gmail_query_tools.gmail_tool.get_thread_with_source_url

    def counted_get_thread(thread_id: str) -> dict[str, Any]:
        provider_reads.append(thread_id)
        return original_get_thread(thread_id)

    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        counted_get_thread,
    )
    invalid = _gmail_result_payload(
        message_id="fabricated-message",
        thread_id="fabricated-thread",
        assessments=[
            {
                "candidate_id": "fabricated-thread",
                "disposition": "selected",
                "rationale": "Still invalid after repair.",
            }
        ],
        decision_stage="gmail_verified_continuation",
    )
    model = FakeModel(
        outputs=[
            [
                _tool_call(
                    "read_gmail_context",
                    {
                        "resource_type": "thread",
                        "resource_id": "thread-current",
                    },
                    call_id="read-continuation",
                )
            ],
            [_structured_message(invalid)],
            [_structured_message(invalid)],
        ]
    )

    with pytest.raises(GmailAgentDecisionError) as exc_info:
        run_gmail_triage_sdk(
            GmailTriageSDKInput(
                subject="",
                body="",
                message_id="msg-current",
                thread_id="thread-current",
                request="Use the same thread.",
            ),
            run_config=build_local_run_config(FakeProvider(model)),
            live=True,
            provider_selection_required=False,
            provider_context_read_required=True,
        )

    assert provider_reads == ["thread-current"]
    assert len(model.calls) == 3
    assert model.calls[-1]["tool_names"] == []
    assert exc_info.value.telemetry["validator_outcome"]["repair_attempted"] is True


def test_rss_agent_calls_history_and_owns_relevance_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [
        {
            "feed_item_id": "rss-general",
            "title": "General technology newsletter",
            "url": "https://example.com/general",
            "source": "Example Feed",
            "summary": "General technology coverage.",
            "published_at": "2026-08-02",
            "tags": ["technology"],
        },
        {
            "feed_item_id": "rss-clinical-ai",
            "title": "Clinical AI validation partnership",
            "url": "https://example.com/clinical-ai",
            "source": "Example Health Feed",
            "summary": "A behavioral-health system announced a validation partnership.",
            "published_at": "2026-08-03",
            "tags": ["clinical AI", "behavioral health"],
        },
    ]
    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_rss_announcement_history_impl",
        lambda **_kwargs: {
            "status": "success",
            "kind": "rss",
            "query": "clinical AI",
            "item_count": 2,
            "items": candidates,
            "send_enabled": False,
        },
    )
    payload = {
        "mode": "llm",
        "summary": "Selected the clinical-AI validation signal for deeper review.",
        "query": "clinical AI partnership",
        "retrieved_item_ids": ["rss-clinical-ai"],
        "articles": [
            {
                **candidates[1],
                "relevance_status": "selected",
                "selection_reason": "Directly matches clinical AI validation.",
                "relevance_to_keystone": "Potential evidence-validation fit.",
            }
        ],
        "recommended_actions": ["Validate the linked source before action."],
        "decision": {
            "decision_owner": "specialist_agent",
            "decision_stage": "signal_relevance_selection",
            "selected_candidate_ids": ["rss-clinical-ai"],
            "candidate_assessments": [
                {
                    "candidate_id": "rss-general",
                    "disposition": "excluded",
                    "rationale": "Does not address the requested clinical domain.",
                },
                {
                    "candidate_id": "rss-clinical-ai",
                    "disposition": "selected",
                    "rationale": "Direct clinical AI validation and partnership relevance.",
                },
            ],
            "reasoning": "The second signal directly matches the operator objective.",
        },
    }
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_rss_announcement_history",
                    {
                        "query": "clinical AI partnership",
                        "selected_only": None,
                        "limit": 8,
                        "live": False,
                    },
                    call_id="rss-history",
                )
            ],
            [_structured_message(payload)],
        ]
    )

    result = run_signal_context_sdk(
        "rss",
        "Which recent RSS signal best fits KNI clinical AI validation work?",
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert result.final_output.retrieved_item_ids == ["rss-clinical-ai"]
    assert result.request_cache["decision_ownership"]["validator_outcome"]["status"] == ("accepted")
    assert result.request_cache["tool_execution"]["model_called_tool_names"] == [
        "retrieve_rss_announcement_history"
    ]
    decision_events = result.request_cache["decision_ownership"]["events"]
    assert all(event["decision_owner"] == "specialist_agent" for event in decision_events)
    assert all(event["decision_stage"] == "signal_relevance_selection" for event in decision_events)
    assert all(event["reasoning"] for event in decision_events)


@pytest.mark.parametrize(
    ("kind", "history_tool", "evidence_tool"),
    [
        (
            "rss",
            "retrieve_rss_announcement_history",
            "read_rss_announcement_evidence",
        ),
        (
            "preprints",
            "retrieve_preprint_announcement_history",
            "read_preprint_announcement_evidence",
        ),
    ],
)
def test_signal_agent_reads_selected_saved_evidence_before_decision(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    history_tool: str,
    evidence_tool: str,
) -> None:
    database_url = f"sqlite:///{tmp_path / f'{kind}-evidence.db'}"
    store = SQLiteStore(database_url)
    item = AnnouncementFeedItem(
        canonical_key=f"{kind}-late-qualification",
        title=f"{kind} Clinical AI validation update",
        url="https://example.org/clinical-ai",
        source=kind,
        feed=kind,
        summary="A preliminary validation update.",
        evidence=[
            *[
                AnnouncementFeedEvidence(
                    kind="search",
                    title=f"Background {index}",
                    url=f"https://example.org/background/{index}",
                    snippet="Background only.",
                )
                for index in range(3)
            ],
            AnnouncementFeedEvidence(
                kind="article",
                title="Saved result",
                url="https://example.org/result",
                snippet=(
                    "The primary outcome was NOT improved; external validation is absent."
                ),
                status="success",
            ),
        ],
    )
    store.save_announcement_feed_item(item)
    monkeypatch.setenv("DATABASE_URL", database_url)
    history_impl = (
        announcement_context_tools.retrieve_preprint_announcement_history_impl
        if kind == "preprints"
        else announcement_context_tools.retrieve_rss_announcement_history_impl
    )
    history = history_impl(query=item.title, database_url=database_url)
    candidate = history["items"][0]
    evidence_id = candidate["evidence_index"][3]["evidence_id"]
    payload = {
        "mode": "llm",
        "summary": "The saved result says the primary outcome was not improved.",
        "query": item.title,
        "retrieved_item_ids": [item.canonical_key],
        "articles": [
            {
                "feed_item_id": item.canonical_key,
                "title": item.title,
                "url": item.url,
                "source": item.source,
                "feed": item.feed,
                "summary": item.summary,
                "detailed_summary": (
                    "The selected saved evidence reports that the primary outcome was "
                    "not improved and external validation is absent."
                ),
                "evidence_status": "article_extracted",
                "evidence_notes": [
                    "Saved evidence snippet reviewed; full article not verified."
                ],
                "limitations": ["Full article coverage was not established."],
                "relevance_status": "selected",
                "selection_reason": "Exact saved evidence qualified the preview.",
            }
        ],
        "recommended_actions": ["Do not treat the result as validated."],
        "decision": {
            "decision_owner": "specialist_agent",
            "decision_stage": "signal_relevance_selection",
            "selected_candidate_ids": [item.canonical_key],
            "candidate_assessments": [
                {
                    "candidate_id": item.canonical_key,
                    "disposition": "selected",
                    "rationale": "The exact saved article evidence was inspected.",
                }
            ],
            "reasoning": "The selected source directly answers the bounded request.",
            "limitations": ["Saved snippet only; full article not verified."],
        },
    }
    model = FakeModel(
        [
            [
                _tool_call(
                    history_tool,
                    {
                        "query": item.title,
                        "history_scope": "discovery",
                        "limit": 8,
                        **({"live": False} if kind == "rss" else {}),
                    },
                    call_id="rss-history",
                )
            ],
            [
                _tool_call(
                    evidence_tool,
                    {
                        "feed_item_id": item.canonical_key,
                        "evidence_id": evidence_id,
                        "max_chars": 1000,
                    },
                    call_id="rss-evidence",
                )
            ],
            [_structured_message(payload)],
        ]
    )

    result = run_signal_context_sdk(
        kind,  # type: ignore[arg-type]
        f"Summarize the saved {kind} Clinical AI validation update and its limitations.",
        run_config=build_local_run_config(FakeProvider(model)),
    )

    discovery_input = json.dumps(model.calls[1]["input"], default=str)
    decision_input = json.dumps(model.calls[2]["input"], default=str)
    assert evidence_id in discovery_input
    assert "NOT improved" not in discovery_input
    assert "NOT improved" in decision_input
    assert "external validation is absent" in decision_input
    assert result.request_cache["decision_ownership"]["validator_outcome"]["status"] == (
        "accepted"
    )
    assert result.request_cache["signal_decision_evidence"][
        "history_tool_call_count"
    ] == 1
    assert result.request_cache["signal_decision_evidence"][
        "evidence_read_call_count"
    ] == 1


def test_signal_decision_rejects_selected_canonical_item_without_saved_evidence_read() -> None:
    candidate_id = "rss-evidence-required"
    result = RssContextResult(
        mode="llm",
        summary="Unverified selection.",
        retrieved_item_ids=[candidate_id],
        articles=[
            {
                "feed_item_id": candidate_id,
                "title": "Saved signal",
                "url": "https://example.org/saved-signal",
                "source": "rss",
            }
        ],
        decision={
            "decision_owner": "specialist_agent",
            "decision_stage": "signal_relevance_selection",
            "selected_candidate_ids": [candidate_id],
            "candidate_assessments": [
                {
                    "candidate_id": candidate_id,
                    "disposition": "selected",
                    "rationale": "Selected from the preview without reading evidence.",
                }
            ],
            "reasoning": "The preview appeared relevant.",
        },
    )
    evidence = {
        "candidate_ids": [candidate_id],
        "candidates": [
            {
                "candidate_id": candidate_id,
                "selected_evidence_read_required": True,
            }
        ],
        "tool_call_count": 1,
        "history_tool_call_count": 1,
        "evidence_read_call_count": 0,
        "tool_output_count": 1,
        "unresolved_tool_call_count": 0,
        "bounded_reformulation": {"used": False, "valid": True},
        "authoritative_constraints": [],
        "evidence_reads": [],
    }

    outcome = validate_signal_agent_decision(result, evidence, max_selected=8)

    assert outcome.status == "rejected"
    assert outcome.reason_code == "selected_signal_saved_evidence_not_read"


def test_preprints_agent_calls_history_and_owns_relevance_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [
        {
            "feed_item_id": "preprint-general",
            "title": "General machine learning benchmark",
            "url": "https://example.com/preprint-general",
            "source": "medRxiv",
            "summary": "A generic machine learning methods paper.",
            "published_at": "2026-08-01",
            "tags": ["machine learning"],
        },
        {
            "feed_item_id": "preprint-psychiatry",
            "title": "Prospective validation of relapse risk signals",
            "url": "https://example.com/preprint-psychiatry",
            "source": "medRxiv",
            "summary": "A prospective psychiatric relapse validation study.",
            "published_at": "2026-08-03",
            "tags": ["psychiatry", "validation"],
        },
    ]
    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_preprint_announcement_history_impl",
        lambda **_kwargs: {
            "status": "success",
            "kind": "preprints",
            "query": "psychiatric validation",
            "item_count": 2,
            "items": candidates,
            "send_enabled": False,
        },
    )
    payload = {
        "mode": "llm",
        "summary": "Selected the prospective psychiatric validation preprint.",
        "query": "psychiatric validation",
        "retrieved_item_ids": ["preprint-psychiatry"],
        "articles": [
            {
                **candidates[1],
                "relevance_status": "selected",
                "selection_reason": "Directly addresses psychiatric validation.",
                "relevance_to_keystone": "Relevant to evidence-validation workflows.",
            }
        ],
        "recommended_actions": ["Verify the full methods before operational use."],
        "decision": {
            "decision_owner": "specialist_agent",
            "decision_stage": "signal_relevance_selection",
            "selected_candidate_ids": ["preprint-psychiatry"],
            "candidate_assessments": [
                {
                    "candidate_id": "preprint-general",
                    "disposition": "excluded",
                    "rationale": "Does not address the requested clinical domain.",
                },
                {
                    "candidate_id": "preprint-psychiatry",
                    "disposition": "selected",
                    "rationale": "Direct psychiatric validation relevance.",
                },
            ],
            "reasoning": "The prospective validation paper best matches the request.",
            "limitations": ["Selection used bounded stored preprint context."],
        },
    }
    model = FakeModel(
        [
            [
                _tool_call(
                    "retrieve_preprint_announcement_history",
                    {
                        "query": "psychiatric validation",
                        "selected_only": None,
                        "limit": 8,
                    },
                    call_id="preprint-history",
                )
            ],
            [_structured_message(payload)],
        ]
    )

    result = run_signal_context_sdk(
        "preprints",
        "Which recent preprint is most relevant to psychiatric validation work?",
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert result.final_output.retrieved_item_ids == ["preprint-psychiatry"]
    assert result.request_cache["decision_ownership"]["validator_outcome"]["status"] == ("accepted")
    assert result.request_cache["tool_execution"]["model_called_tool_names"] == [
        "retrieve_preprint_announcement_history"
    ]
    events = result.request_cache["decision_ownership"]["events"]
    assert all(event["reasoning"] for event in events)
    assert all(event["limitations"] for event in events)


def test_decision_record_rejects_selected_identity_assessed_as_excluded() -> None:
    with pytest.raises(ValueError, match="disposition='selected'"):
        AgentDecisionRecord(
            selected_candidate_ids=["candidate-1"],
            candidate_assessments=[
                {
                    "candidate_id": "candidate-1",
                    "disposition": "excluded",
                    "rationale": "Contradictory assessment.",
                }
            ],
        )


def test_signal_needs_more_context_cannot_claim_retrieved_or_article_identity() -> None:
    result = RssContextResult(
        mode="llm",
        retrieved_item_ids=["rss-1"],
        articles=[
            {
                "feed_item_id": "rss-1",
                "title": "Claimed without sufficient context",
            }
        ],
        decision={
            "decision_owner": "specialist_agent",
            "decision_stage": "signal_relevance_selection",
            "needs_more_context": True,
            "reasoning": "The bounded history is insufficient.",
        },
    )
    evidence = {
        "candidate_ids": ["rss-1"],
        "tool_call_count": 1,
        "tool_output_count": 1,
    }

    outcome = validate_signal_agent_decision(result, evidence, max_selected=3)
    telemetry = signal_decision_telemetry(
        result,
        evidence,
        outcome,
        route="rss_context_agent",
    )

    assert outcome.status == "rejected"
    assert outcome.reason_code == "needs_more_context_with_claimed_signal_output"
    assert telemetry["validator_outcome"]["status"] == "rejected"


@pytest.mark.parametrize(
    "request_text",
    [
        "Find tomorrow's G2i meeting on my calendar and draft a Slack reply to the related email.",
        "Can you check my calendar for the G2i interview, find its email, and write a reply here?",
        (
            "Use the event at 10 tomorrow to locate the matching Gmail thread and say "
            "I'm looking forward to it."
        ),
        (
            "Which email goes with my next G2i calendar appointment? Give me reply "
            "copy, not a Gmail draft."
        ),
        (
            "Look up the meeting in Google Calendar, then prepare a response to the "
            "associated email in Slack."
        ),
        (
            "I have a G2i event tomorrow morning. Find the corresponding inbox "
            "conversation and help me reply."
        ),
        (
            "From the calendar invite, locate the right Gmail thread and write a "
            "short answer for this thread."
        ),
        (
            "Please connect my upcoming G2i appointment to its email and draft a "
            "response without sending."
        ),
        (
            "Check the scheduled G2i call, then find the matching message and give "
            "me local reply wording."
        ),
        (
            "Find the email associated with the next G2i event on my calendar and "
            "draft a reply here only."
        ),
        (
            "Use tomorrow's interview appointment as context to choose the correct "
            "Gmail conversation and respond in Slack."
        ),
        (
            "Calendar first: identify the upcoming G2i meeting, then find its email "
            "and write a no-send reply."
        ),
    ],
)
def test_calendar_to_gmail_paraphrases_preserve_primary_owner_and_context(
    request_text: str,
) -> None:
    plan = infer_manual_request_plan(request_text)

    assert plan.target_agent == "gmail_triage"
    assert plan.provider_system == "gmail"
    assert any(
        requirement.provider_system == "google_calendar"
        and requirement.resource_type == "calendar_event"
        and set(requirement.operations) <= {"read", "search", "verify"}
        for requirement in plan.provider_context_requirements
    )


def test_calendar_manager_selection_is_validated_before_gmail_handoff() -> None:
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_item",
                call_id="calendar-read",
                tool_name="read_google_calendar_window",
            ),
            SimpleNamespace(
                type="tool_call_output_item",
                call_id="calendar-read",
                output=json.dumps(
                    {
                        "status": "success",
                        "events": [
                            {
                                "event_id": "event-old",
                                "title": "G2i interview",
                                "display_start_date": "2026-08-03",
                                "display_start_time": "09:00",
                            },
                            {
                                "event_id": "event-current",
                                "title": "G2i interview",
                                "display_start_date": "2026-08-04",
                                "display_start_time": "10:00",
                                "organizer": "G2i Recruiting",
                                "attendees": ["Anup"],
                            },
                        ],
                        "verification": {"passed": True},
                    }
                ),
            ),
        ]
    )
    decision = AgentDecisionRecord(
        decision_owner="chief_of_staff",
        decision_stage="calendar_context_selection",
        selected_candidate_id="event-current",
        candidate_assessments=[
            DecisionCandidateAssessment(
                candidate_id="event-old",
                disposition="excluded",
                rationale="Earlier obsolete time.",
            ),
            DecisionCandidateAssessment(
                candidate_id="event-current",
                disposition="selected",
                rationale="Upcoming event matching the request.",
            ),
        ],
        reasoning="Selected the upcoming active appointment.",
    )
    stage = validate_calendar_provider_context_decision(raw_result, [decision])
    plan = infer_manual_request_plan(
        "Find my upcoming G2i calendar event and draft a reply to its associated Gmail email."
    )

    assert stage.validator_outcome.status == "accepted"
    assert stage.selected_object["event_id"] == "event-current"
    satisfied, missing = provider_context_requirements_satisfied(plan, [stage])
    assert satisfied is True
    assert missing == []
    handoff = provider_context_handoff_text([stage])
    assert "event-current" in handoff
    assert "Gmail" not in stage.tool_names
    assert stage.provider_receipts == [
        {
            "status": "success",
            "provider": "google_calendar",
            "candidate_count": 2,
            "verification": {"passed": True, "reason_code": ""},
            "receipt_id": "",
        }
    ]
    assert "events" not in stage.provider_receipts[0]


def test_calendar_context_requires_chief_to_select_gmail_handoff() -> None:
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_item",
                call_id="calendar-read",
                tool_name="read_google_calendar_window",
            ),
            SimpleNamespace(
                type="tool_call_output_item",
                call_id="calendar-read",
                output=json.dumps(
                    {
                        "status": "success",
                        "events": [{"event_id": "event-current", "title": "Interview"}],
                        "verification": {"passed": True},
                    }
                ),
            ),
        ]
    )
    calendar_decision = AgentDecisionRecord(
        decision_owner="chief_of_staff",
        decision_stage="calendar_context_selection",
        selected_candidate_id="event-current",
        candidate_assessments=[
            DecisionCandidateAssessment(
                candidate_id="event-current",
                disposition="selected",
                rationale="Matches the requested event.",
            )
        ],
        reasoning="Selected the verified event.",
    )
    missing_handoff_output = SimpleNamespace(
        agent_name="chief_of_staff",
        durable_handoff=None,
        decision=AgentDecisionRecord(
            decision_owner="chief_of_staff",
            decision_stage="chief_delegation_selection",
            selected_candidate_ids=["workflow:calendar-read"],
            candidate_assessments=[
                DecisionCandidateAssessment(
                    candidate_id="workflow:calendar-read",
                    disposition="selected",
                    rationale="Read Calendar context.",
                )
            ],
            reasoning="Calendar context is needed first.",
        ),
    )

    rejected = validate_calendar_provider_context_decision(
        raw_result,
        [calendar_decision],
        manager_output=missing_handoff_output,
        required_downstream_agent="gmail_triage",
    )

    assert rejected.validator_outcome.status == "repair_required"
    assert rejected.validator_outcome.reason_code == "required_manager_handoff_missing"
    assert rejected.selected_object == {}
    assert rejected.handoff_confirmed is False

    accepted_output = SimpleNamespace(
        agent_name="chief_of_staff",
        durable_handoff=SimpleNamespace(agent="gmail_triage"),
        decision=AgentDecisionRecord(
            decision_owner="chief_of_staff",
            decision_stage="chief_delegation_selection",
            selected_candidate_ids=["workflow:calendar-read", "gmail_triage"],
            candidate_assessments=[
                DecisionCandidateAssessment(
                    candidate_id="workflow:calendar-read",
                    disposition="selected",
                    rationale="Read Calendar context.",
                ),
                DecisionCandidateAssessment(
                    candidate_id="gmail_triage",
                    disposition="selected",
                    rationale="Use the verified event to select the Gmail thread.",
                ),
            ],
            reasoning="Calendar supplies context and Gmail owns the reply decision.",
        ),
    )

    accepted = validate_calendar_provider_context_decision(
        raw_result,
        [calendar_decision],
        manager_output=accepted_output,
        required_downstream_agent="gmail_triage",
    )

    assert accepted.validator_outcome.status == "accepted"
    assert accepted.downstream_agent == "gmail_triage"
    assert accepted.handoff_confirmed is True


def test_fake_chief_calendar_to_gmail_stage_preserves_context_and_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "Use tomorrow's G2i calendar interview to find the associated Gmail thread "
        "and write reply copy here without sending or saving anything."
    )
    plan = infer_manual_request_plan(request)
    assert plan.target_agent == "gmail_triage"
    assert plan.provider_context_requirements
    monkeypatch.setattr(
        google_calendar_tool,
        "read_google_calendar_window_impl",
        lambda *_args, **_kwargs: {
            "status": "success",
            "provider": "google_calendar",
            "events": [
                {
                    "event_id": "event-current",
                    "title": "G2i interview",
                    "display_start_date": "2026-08-04",
                    "display_start_time": "10:00",
                    "organizer": "G2i Recruiting",
                }
            ],
            "verification": {"passed": True},
            "receipt_id": "calendar-fixture-receipt",
        },
    )
    manager_payload = {
        "agent_name": "chief_of_staff",
        "mode": "llm",
        "intent": request,
        "summary": "Selected the matching Calendar event, then handed Gmail ownership forward.",
        "recommended_route": {
            "workflow_type": "calendar-read",
            "target_channel": "current-thread",
            "rationale": "Calendar supplies bounded read context before Gmail triage.",
            "requires_human_approval_before_post": True,
        },
        "durable_handoff": {
            "agent": "gmail_triage",
            "rationale": "Gmail owns related-thread selection and reply relevance.",
            "requires_approval": False,
        },
        "provider_context_decisions": [
            {
                "decision_owner": "chief_of_staff",
                "decision_stage": "calendar_context_selection",
                "selected_candidate_id": "event-current",
                "candidate_assessments": [
                    {
                        "candidate_id": "event-current",
                        "disposition": "selected",
                        "rationale": "Only verified event matching G2i and tomorrow.",
                    }
                ],
                "reasoning": "Selected the verified current G2i event.",
            }
        ],
        "decision": {
            "decision_owner": "chief_of_staff",
            "decision_stage": "chief_delegation_selection",
            "selected_candidate_ids": ["workflow:calendar-read", "gmail_triage"],
            "candidate_assessments": [
                {
                    "candidate_id": "workflow:calendar-read",
                    "disposition": "selected",
                    "rationale": "Calendar must supply the event context first.",
                },
                {
                    "candidate_id": "gmail_triage",
                    "disposition": "selected",
                    "rationale": "Gmail owns the associated-thread and reply decisions.",
                },
            ],
            "reasoning": "Coordinated a read-only Calendar to Gmail workflow.",
        },
    }
    model = FakeModel(
        outputs=[
            [
                _tool_call(
                    "read_google_calendar_window",
                    {
                        "time_min": "2026-08-04T00:00:00-04:00",
                        "time_max": "2026-08-05T00:00:00-04:00",
                        "query": "G2i",
                        "max_results": 4,
                        "display_timezone": "America/New_York",
                        "live": False,
                    },
                    call_id="calendar-read",
                )
            ],
            [_structured_message(manager_payload)],
        ]
    )

    manager_result = run_chief_of_staff_sdk(
        {
            "request": request,
            "manual_request_plan": plan.model_dump(mode="json"),
            "include_specialist_tools": False,
        },
        run_config=build_local_run_config(FakeProvider(model)),
        force_sdk_interpretation=True,
        manual_request_plan=plan,
        include_specialist_tools=False,
        attach_tools=True,
    )
    stage = validate_calendar_provider_context_decision(
        manager_result.raw_result,
        manager_result.final_output.provider_context_decisions,
        manager_output=manager_result.final_output,
        required_downstream_agent="gmail_triage",
    )

    assert stage.validator_outcome.status == "accepted"
    assert stage.selected_object["event_id"] == "event-current"
    assert stage.handoff_confirmed is True
    assert manager_result.request_cache["tool_execution"]["model_called_tool_names"] == [
        "read_google_calendar_window"
    ]
    assert manager_result.request_cache["decision_ownership"]["reasoning"] == (
        "Coordinated a read-only Calendar to Gmail workflow."
    )


def test_calendar_context_rejects_non_manager_selection_without_handoff() -> None:
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_item",
                call_id="calendar-read",
                tool_name="read_google_calendar_window",
            ),
            SimpleNamespace(
                type="tool_call_output_item",
                call_id="calendar-read",
                output=json.dumps(
                    {
                        "status": "success",
                        "events": [{"event_id": "event-current", "title": "Interview"}],
                        "verification": {"passed": True},
                    }
                ),
            ),
        ]
    )
    decision = AgentDecisionRecord(
        decision_owner="specialist_agent",
        decision_stage="calendar_context_selection",
        selected_candidate_id="event-current",
        candidate_assessments=[
            DecisionCandidateAssessment(
                candidate_id="event-current",
                disposition="selected",
                rationale="Matches the requested event.",
            )
        ],
        reasoning="Selected the only event.",
    )

    stage = validate_calendar_provider_context_decision(raw_result, [decision])

    assert stage.validator_outcome.status == "repair_required"
    assert stage.validator_outcome.reason_code == "invalid_calendar_context_decision_owner"
    assert stage.selected_object == {}


def test_calendar_context_requires_assessment_of_every_alternative() -> None:
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_item",
                call_id="calendar-read",
                tool_name="read_google_calendar_window",
            ),
            SimpleNamespace(
                type="tool_call_output_item",
                call_id="calendar-read",
                output=json.dumps(
                    {
                        "status": "success",
                        "events": [
                            {"event_id": "event-old", "title": "Interview"},
                            {"event_id": "event-current", "title": "Interview"},
                        ],
                        "verification": {"passed": True},
                    }
                ),
            ),
        ]
    )
    decision = AgentDecisionRecord(
        decision_owner="orchestrator",
        decision_stage="calendar_context_selection",
        selected_candidate_id="event-current",
        candidate_assessments=[
            DecisionCandidateAssessment(
                candidate_id="event-current",
                disposition="selected",
                rationale="Matches the requested time.",
            )
        ],
        reasoning="Selected the current event.",
    )

    stage = validate_calendar_provider_context_decision(raw_result, [decision])

    assert stage.validator_outcome.status == "repair_required"
    assert stage.validator_outcome.reason_code == "calendar_candidate_assessments_incomplete"
    assert stage.selected_object == {}


def test_fabricated_selection_is_rejected_without_python_substitution() -> None:
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_item",
                call_id="query-1",
                tool_name="query_gmail_message_summaries",
            ),
            SimpleNamespace(
                type="tool_call_output_item",
                call_id="query-1",
                output=json.dumps(
                    {
                        "status": "read",
                        "items": [
                            {
                                "message_id": value["id"],
                                "thread_id": value["threadId"],
                                "received_at": value["received_at"],
                                "sender_name": value["sender_name"],
                                "sender_email": value["sender_email"],
                                "subject": value["subject"],
                                "snippet": value["snippet"],
                            }
                            for value in _four_g2i_candidates()
                        ],
                    }
                ),
            ),
        ]
    )
    result = EmailTriageResult.model_validate(
        _gmail_result_payload(
            message_id="invented",
            thread_id="invented-thread",
            assessments=[
                {
                    "candidate_id": "invented-thread",
                    "disposition": "selected",
                    "rationale": "Fabricated identity.",
                }
            ],
        )
    )

    outcome = validate_gmail_agent_decision(result, gmail_decision_evidence(raw_result))

    assert outcome.status == "repair_required"
    assert outcome.reason_code == "fabricated_selected_identity"
    assert outcome.selected_candidate_id == "invented-thread"


def test_gmail_query_ceiling_counts_attempts_without_parseable_outputs() -> None:
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_item",
                call_id="query-1",
                tool_name="query_gmail_message_summaries",
            ),
            SimpleNamespace(
                type="tool_call_output_item",
                call_id="query-1",
                output=json.dumps({"status": "read", "items": []}),
            ),
            SimpleNamespace(
                type="tool_call_item",
                call_id="query-2",
                tool_name="query_gmail_message_summaries",
            ),
            SimpleNamespace(
                type="tool_call_output_item",
                call_id="query-2",
                output="not-json",
            ),
        ]
    )
    result = EmailTriageResult.model_validate(
        _gmail_result_payload(message_id="", thread_id="", assessments=[])
        | {
            "decision": {
                "decision_owner": "specialist_agent",
                "decision_stage": "gmail_candidate_selection",
                "needs_more_context": True,
            }
        }
    )

    evidence = gmail_decision_evidence(raw_result)
    outcome = validate_gmail_agent_decision(result, evidence)

    assert evidence.query_call_count == 2
    assert evidence.query_output_count == 1
    assert outcome.reason_code == "gmail_query_call_count_out_of_bounds"


def test_signal_history_ceiling_counts_attempts_without_parseable_outputs() -> None:
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_item",
                call_id="signal-1",
                tool_name="retrieve_rss_announcement_history",
            ),
            SimpleNamespace(
                type="tool_call_output_item",
                call_id="signal-1",
                output=json.dumps({"status": "success", "items": []}),
            ),
            SimpleNamespace(
                type="tool_call_item",
                call_id="signal-2",
                tool_name="retrieve_rss_announcement_history",
            ),
        ]
    )
    result = RssContextResult(
        summary="More context is needed.",
        decision=AgentDecisionRecord(
            decision_owner="specialist_agent",
            decision_stage="signal_relevance_selection",
            needs_more_context=True,
        ),
    )

    evidence = signal_decision_evidence(
        raw_result,
        tool_name="retrieve_rss_announcement_history",
    )
    outcome = validate_signal_agent_decision(result, evidence, max_selected=8)

    assert evidence["tool_call_count"] == 1
    assert evidence["model_tool_call_count"] == 2
    assert evidence["unresolved_tool_call_count"] == 1
    assert evidence["tool_output_count"] == 1
    assert outcome.reason_code == "signal_history_call_count_out_of_bounds"


def test_verified_gmail_continuation_rejects_query_or_extra_read() -> None:
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_item",
                call_id="query-1",
                tool_name="query_gmail_message_summaries",
            ),
            SimpleNamespace(
                type="tool_call_output_item",
                call_id="query-1",
                output=json.dumps({"status": "read", "items": []}),
            ),
            SimpleNamespace(
                type="tool_call_item",
                call_id="read-1",
                tool_name="read_gmail_context",
            ),
            SimpleNamespace(
                type="tool_call_output_item",
                call_id="read-1",
                output=json.dumps(
                    {"status": "read", "resource_type": "thread", "resource_id": "thread-current"}
                ),
            ),
        ]
    )
    result = EmailTriageResult.model_validate(
        _gmail_result_payload(
            message_id="msg-current",
            thread_id="thread-current",
            assessments=[_candidate_assessments()[0]],
            decision_stage="gmail_verified_continuation",
        )
    )

    outcome = validate_verified_gmail_continuation_decision(
        result,
        raw_result,
        expected_message_id="msg-current",
        expected_thread_id="thread-current",
    )

    assert outcome.status == "repair_required"
    assert outcome.reason_code == "verified_gmail_continuation_mismatch"
