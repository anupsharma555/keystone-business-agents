from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest

import keystone_agents.agents.opportunity_scout as opportunity_scout_module
from keystone_agents.agent_decision_contracts import (
    opportunity_scout_decision_contract,
    outreach_composer_decision_contract,
)
from keystone_agents.agents.opportunity_scout import run_opportunity_scout_sdk
from keystone_agents.agents.outreach_composer import build_outreach_composer_agent
from keystone_agents.models import OpportunityScoutSDKInput, OutreachComposerSDKInput
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.runtime.tool_execution import (
    ToolEvidenceGroup,
    ToolExecutionContract,
)
from keystone_agents.schemas.manual_request_plan import AskShapePolicy, ManualRequestPlan
from keystone_agents.schemas.outreach import OutreachDraft
from keystone_agents.sdk import build_local_run_config

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
    """Deterministic offline model that records exactly what it could see."""

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
                "system_instructions": system_instructions,
            }
        )
        return ModelResponse(
            output=self.outputs.pop(0),
            usage=Usage(requests=1),
            response_id=f"opportunity-outreach-fake-{len(self.calls)}",
        )

    def stream_response(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        raise NotImplementedError


class FakeProvider(ModelProvider):
    def __init__(self, model: FakeModel) -> None:
        self.model = model

    def get_model(self, _model_name: str | None) -> Model:
        return self.model


def _tool_call(name: str, arguments: dict[str, Any]) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        type="function_call",
        name=name,
        call_id=f"fake-{name}",
        arguments=json.dumps(arguments),
        status="completed",
    )


def _structured_message(payload: dict[str, Any]) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="fake-structured-output",
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


def _model_input_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def _decision(
    *,
    stage: str,
    candidate_ids: list[str],
    selected_ids: list[str],
    reasoning: str,
) -> dict[str, Any]:
    return {
        "decision_owner": "specialist_agent",
        "decision_stage": stage,
        "selected_candidate_ids": selected_ids,
        "candidate_assessments": [
            {
                "candidate_id": candidate_id,
                "disposition": (
                    "selected" if candidate_id in selected_ids else "excluded"
                ),
                "rationale": (
                    "Used in the returned decision."
                    if candidate_id in selected_ids
                    else "Not used in the returned decision."
                ),
            }
            for candidate_id in candidate_ids
        ],
        "reasoning": reasoning,
        "limitations": ["Offline synthetic evidence only."],
        "needs_more_context": False,
    }


def _opportunity_payload(*, selected_id: str) -> dict[str, Any]:
    candidate_id = "opportunity:harborlight-rfp"
    assessed_ids = [candidate_id]
    if selected_id != candidate_id:
        assessed_ids.append(selected_id)
    return {
        "topic": "regional behavioral-health measurement RFP",
        "dry_run": True,
        "records": [
            {
                "company_name": "Harborlight Measurement Partners",
                "canonical_entity_key": candidate_id,
                "opportunity_type": "grant or collaboration opportunity",
                "priority_score": 50,
                "why_now_signal": (
                    "A supplied active RFP requests cross-site behavioral-health "
                    "outcomes reporting support."
                ),
                "recommended_next_step": (
                    "Verify eligibility and the primary RFP before any outreach."
                ),
                "sources": [
                    {
                        "source_id": candidate_id,
                        "provider_candidate_id": candidate_id,
                        "title": "Synthetic Harborlight RFP notice",
                        "url": "https://example.test/harborlight-rfp",
                        "source_type": "government",
                        "supported_signal": (
                            "Active cross-site behavioral-health reporting RFP."
                        ),
                    }
                ],
                "source_signals": ["active RFP", "behavioral health"],
                "keystone_fit_reason": (
                    "The supplied signal overlaps Keystone's measurement and evidence work."
                ),
                "outside_consulting_likelihood": 60,
                "handoff_to_business_research_analyst": False,
                "outreach_draft": None,
                "approval_required_before_outreach": True,
            }
        ],
        "audit_notes": ["Used only the supplied synthetic packet and scoring helper."],
        "outreach_generated": False,
        "decision": _decision(
            stage="opportunity_candidate_selection",
            candidate_ids=assessed_ids,
            selected_ids=[selected_id],
            reasoning="Selected the source-backed RFP after reviewing the supplied signals.",
        ),
    }


def _outreach_payload(*, selected_ids: list[str]) -> dict[str, Any]:
    allowed_ids = ["keystone_profile", "source:harborlight-approved"]
    assessed_ids = [*allowed_ids]
    for selected_id in selected_ids:
        if selected_id not in assessed_ids:
            assessed_ids.append(selected_id)
    return {
        "company_name": "Harborlight Measurement Partners",
        "recipient": "Harborlight team",
        "outreach_goal": "compare notes on cross-site behavioral-health reporting",
        "email_subject": "Cross-site behavioral-health reporting",
        "email_body": (
            "Hello,\n\nHarborlight supports regional clinics with cross-site "
            "behavioral-health reporting. Keystone provides clinical AI evaluation "
            "and evidence strategy support. Would a brief exploratory conversation "
            "be useful?"
        ),
        "linkedin_note": (
            "Harborlight's cross-site reporting work caught my attention. "
            "Open to compare notes on behavioral-health measurement?"
        ),
        "personalization_rationale": (
            "Used only the approved Harborlight signal and Keystone profile."
        ),
        "facts_used": [
            {
                "claim_text": (
                    "Harborlight supports regional clinics with cross-site "
                    "behavioral-health reporting."
                ),
                "source_id": "source:harborlight-approved",
                "confidence": 0.9,
                "claim_type": "opportunity_signal",
            },
            {
                "claim_text": (
                    "Keystone provides clinical AI evaluation and evidence strategy support."
                ),
                "source_id": "keystone_profile",
                "confidence": 0.9,
                "claim_type": "company_identity",
            },
        ],
        "source_ids_used": allowed_ids,
        "drafting_mode": "llm_constrained",
        "approved_context_used": True,
        "unsupported_claims_flagged": [],
        "unsupported_claim_explanations": [],
        "approval_required": True,
        "approval_state": "pending",
        "approval_scope": "external_use",
        "send_enabled": False,
        "sent": False,
        "can_send_email": False,
        "decision": _decision(
            stage="outreach_evidence_selection",
            candidate_ids=assessed_ids,
            selected_ids=selected_ids,
            reasoning="Selected only approved facts after checking the proposed copy.",
        ),
    }


def test_opportunity_scout_sees_evidence_calls_score_and_repairs_its_own_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    candidate_id = "opportunity:harborlight-rfp"
    source_url = "https://example.test/harborlight-rfp"
    typed_input = OpportunityScoutSDKInput(
        topic="regional behavioral-health measurement RFP",
        max_results=1,
        context=(
            "Approved synthetic candidate packet. "
            f"Candidate identity: {candidate_id}. Source URL: {source_url}. "
            "Signals: active RFP; behavioral health. Read-only; do not browse, "
            "save, or generate outreach."
        ),
    )
    plan = ManualRequestPlan(
        source="canonical:test",
        target_agent="opportunity_scout",
        intent="opportunity_search",
        task_objective="opportunity_discovery",
        expected_artifact_type="opportunity_record",
        constraints=["comparison-format"],
        requires_live_search=False,
        ask_shape=AskShapePolicy(permission_state="read_only"),
    )
    invalid_id = "opportunity:invented"
    model = FakeModel(
        outputs=[
            [
                _tool_call(
                    "score_opportunity",
                    {
                        "company_name": "Harborlight Measurement Partners",
                        "opportunity_type": "grant",
                        "signals": ["active RFP", "behavioral health"],
                    },
                )
            ],
            [_structured_message(_opportunity_payload(selected_id=invalid_id))],
            [_structured_message(_opportunity_payload(selected_id=candidate_id))],
        ]
    )
    contract = replace(
        opportunity_scout_decision_contract(),
        pre_model_candidate_ids=(candidate_id,),
        mandatory_pre_model_context_ids=(candidate_id,),
        pre_model_context_source="supplied_opportunity_context",
    )
    monkeypatch.setattr(
        opportunity_scout_module,
        "opportunity_scout_decision_contract",
        lambda: contract,
    )

    result = run_opportunity_scout_sdk(
        typed_input=typed_input,
        run_config=build_local_run_config(FakeProvider(model)),
        live=True,
        max_turns=4,
        manual_request_plan=plan,
    )

    assert model.calls[0]["tool_names"] == ["score_opportunity"]
    assert "When `score_opportunity` is attached, call it before returning a selected record" in (
        " ".join(model.calls[0]["system_instructions"].split())
    )
    first_input = _model_input_text(model.calls[0]["input"])
    assert candidate_id in first_input
    assert source_url in first_input
    assert "active RFP" in first_input
    assert "behavioral health" in first_input
    assert "next-action clarity" in _model_input_text(model.calls[1]["input"])
    assert "opportunity:invented" in _model_input_text(model.calls[2]["input"])
    assert model.calls[2]["tool_names"] == []

    pre_model = result.request_cache["pre_model_decision_context"]
    assert pre_model["context_complete"] is True
    assert pre_model["model_visible_candidate_ids"] == [candidate_id]
    decision = result.request_cache["decision_ownership"]
    assert decision["attempt_count"] == 2
    assert decision["repair_attempted"] is True
    assert decision["attempts"][0]["validator_outcome"]["reason_code"] == (
        "selected_identity_not_in_candidate_set"
    )
    assert decision["validator_outcome"]["status"] == "accepted"
    assert result.request_cache["decision_repairs"] == 1
    assert "score_opportunity" in result.request_cache["tool_execution"][
        "model_called_tool_names"
    ]
    assert result.final_output.records[0].canonical_entity_key == candidate_id
    assert result.final_output.records[0].sources[0].url == source_url
    assert result.final_output.decision.selected_candidate_ids == [candidate_id]


def test_outreach_composer_sees_approved_context_checks_claims_and_repairs_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    allowed_ids = ["keystone_profile", "source:harborlight-approved"]
    typed_input = OutreachComposerSDKInput(
        company_name="Harborlight Measurement Partners",
        outreach_goal="compare notes on cross-site behavioral-health reporting",
        approved_context=(
            "Approved evidence only. keystone_profile: Keystone provides clinical AI "
            "evaluation and evidence strategy support. source:harborlight-approved: "
            "Harborlight supports regional clinics with cross-site behavioral-health "
            "reporting. No approved outcomes or prior-client claims."
        ),
    )
    plan = ManualRequestPlan(
        source="canonical:test",
        target_agent="outreach_composer",
        intent="outreach_draft",
        task_objective="outreach_draft",
        expected_artifact_type="outreach_draft",
        requires_live_search=False,
        requires_approved_context=False,
        ask_shape=AskShapePolicy(
            permission_state="draft_only",
            prior_context_dependency="selected_context",
            source_type_preference=["approved_synthetic"],
        ),
    )
    invalid_id = "source:invented-outcome"
    model = FakeModel(
        outputs=[
            [
                _tool_call(
                    "check_unsupported_claims",
                    {
                        "text": (
                            "Keystone has helped companies reduce enrollment delays. "
                            "Would you be open to compare notes?"
                        ),
                        "allowed_claims": [
                            "Keystone provides clinical AI evaluation and evidence "
                            "strategy support."
                        ],
                    },
                )
            ],
            [_structured_message(_outreach_payload(selected_ids=[invalid_id]))],
            [_structured_message(_outreach_payload(selected_ids=allowed_ids))],
        ]
    )
    agent = build_outreach_composer_agent(
        request_text=typed_input.to_prompt(),
        manual_request_plan=plan,
    )

    result = run_typed_sdk_agent(
        agent=agent,
        typed_input=typed_input,
        output_type=OutreachDraft,
        run_config=build_local_run_config(FakeProvider(model)),
        max_turns=4,
        tool_execution_contract=ToolExecutionContract.required(
            ToolEvidenceGroup("outreach_claim_check", ("check_unsupported_claims",)),
            stage="outreach_approved_context_draft",
        ),
        decision_contract=outreach_composer_decision_contract(allowed_ids),
    )

    assert model.calls[0]["tool_names"] == [
        "check_unsupported_claims",
        "build_approved_outreach_drafting_context",
        "compose_outreach_draft_llm_constrained",
    ]
    first_input = _model_input_text(model.calls[0]["input"])
    assert all(source_id in first_input for source_id in allowed_ids)
    assert "No approved outcomes or prior-client claims" in first_input
    checked_input = _model_input_text(model.calls[1]["input"])
    assert "has_unsupported_claims" in checked_input
    assert "unsupported outreach claim" in checked_input
    assert invalid_id in _model_input_text(model.calls[2]["input"])
    assert model.calls[2]["tool_names"] == []

    pre_model = result.request_cache["pre_model_decision_context"]
    assert pre_model["context_complete"] is True
    assert pre_model["model_visible_candidate_ids"] == allowed_ids
    decision = result.request_cache["decision_ownership"]
    assert decision["attempt_count"] == 2
    assert decision["repair_attempted"] is True
    assert decision["attempts"][0]["validator_outcome"]["reason_code"] == (
        "selected_identity_not_in_candidate_set"
    )
    assert decision["validator_outcome"]["status"] == "accepted"
    assert result.request_cache["decision_repairs"] == 1
    assert "check_unsupported_claims" in result.request_cache["tool_execution"][
        "model_called_tool_names"
    ]
    assert result.final_output.source_ids_used == allowed_ids
    assert [fact.source_id for fact in result.final_output.facts_used] == [
        "source:harborlight-approved",
        "keystone_profile",
    ]
    assert result.final_output.decision.selected_candidate_ids == allowed_ids
    assert result.final_output.unsupported_claims_flagged == []
    assert result.final_output.send_enabled is False
