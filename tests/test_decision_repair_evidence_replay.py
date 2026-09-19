from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from hashlib import sha256
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from keystone_agents.receipts.normalization import identity_fingerprints
from keystone_agents.run import run_typed_sdk_agent, sdk_run_failure_metadata
from keystone_agents.runtime.decision_validation import (
    AgentDecisionContract,
    AgentDecisionValidationError,
    SpecialistDecisionEvidence,
)
from keystone_agents.runtime.tool_execution import (
    ToolEvidenceGroup,
    ToolExecutionContract,
)
from keystone_agents.schemas.decision_ownership import AgentDecisionRecord
from keystone_agents.sdk import build_local_run_config, build_sdk_agent, function_tool

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


class DecisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_ids: list[str] = Field(max_length=10)
    selected_candidate_id: str
    summary: str
    decision: AgentDecisionRecord


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
                "system_instructions": system_instructions,
            }
        )
        return ModelResponse(
            output=self.outputs.pop(0),
            usage=Usage(requests=1),
            response_id=f"decision-replay-fake-{len(self.calls)}",
        )

    def stream_response(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        raise NotImplementedError


class FakeProvider(ModelProvider):
    def __init__(self, model: FakeModel) -> None:
        self.model = model

    def get_model(self, _model_name: str | None) -> Model:
        return self.model


def _tool_call(
    *,
    call_id: str = "provider-read-1",
    query: str = "tomorrow interview",
) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        type="function_call",
        name="read_provider_candidates",
        call_id=call_id,
        arguments=json.dumps({"query": query}),
        status="completed",
    )


def _score_call(candidate_id: str = "candidate-current") -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        type="function_call",
        name="score_candidate",
        call_id="score-candidate-1",
        arguments=json.dumps({"candidate_id": candidate_id}),
        status="completed",
    )


def _provider_detail_call() -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        type="function_call",
        name="read_provider_candidate_detail",
        call_id="provider-detail-1",
        arguments=json.dumps({"candidate_id": "candidate-cancelled"}),
        status="completed",
    )


def _mutation_call() -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        type="function_call",
        name="create_test_candidate",
        call_id="create-test-candidate-1",
        arguments=json.dumps({"candidate_id": "candidate-current"}),
        status="completed",
    )


def _validation_call() -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        type="function_call",
        name="validate_test_candidate",
        call_id="validate-test-candidate-1",
        arguments=json.dumps({"candidate_id": "candidate-current"}),
        status="completed",
    )


def _structured_message(payload: dict[str, Any]) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        type="message",
        id="decision-result",
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
    if isinstance(value, list):
        return "\n".join(
            str(item.get("content") or "")
            for item in value
            if isinstance(item, dict)
        )
    return str(value)


def _decision_payload(selected_id: str) -> dict[str, Any]:
    candidate_ids = ["candidate-current", "candidate-cancelled"]
    assessed_ids = [*candidate_ids]
    if selected_id not in assessed_ids:
        assessed_ids.append(selected_id)
    return {
        "candidate_ids": candidate_ids,
        "selected_candidate_id": selected_id,
        "summary": "Selected the active candidate from bounded provider evidence.",
        "decision": {
            "decision_owner": "specialist_agent",
            "decision_stage": "provider_candidate_selection",
            "selected_candidate_id": selected_id,
            "candidate_assessments": [
                {
                    "candidate_id": candidate_id,
                    "disposition": (
                        "selected" if candidate_id == selected_id else "excluded"
                    ),
                    "rationale": (
                        "Matches the active interview evidence."
                        if candidate_id == selected_id
                        else "Does not match the active interview evidence."
                    ),
                }
                for candidate_id in assessed_ids
            ],
            "reasoning": "Compared lifecycle and timing evidence before selecting.",
            "limitations": ["Synthetic bounded provider evidence."],
            "needs_more_context": False,
        },
    }


def _decision_contract() -> AgentDecisionContract:
    def evidence(output: DecisionResult) -> SpecialistDecisionEvidence:
        return SpecialistDecisionEvidence.build(
            output.candidate_ids,
            required_selected_ids=[output.selected_candidate_id],
            selection_required=True,
            provider_identities_by_candidate={
                candidate_id: [candidate_id] for candidate_id in output.candidate_ids
            },
        )

    return AgentDecisionContract(
        route="synthetic_provider_specialist",
        decision_stage="provider_candidate_selection",
        evidence_resolver=evidence,
    )


def _provider_tool(
    *,
    counter: dict[str, int],
    output_factory: Callable[[], dict[str, Any]],
) -> Any:
    @function_tool
    def read_provider_candidates(query: str) -> str:
        """Read a bounded synthetic provider candidate set."""

        counter["calls"] += 1
        return json.dumps(output_factory(), ensure_ascii=True, sort_keys=True)

    return read_provider_candidates


def _run(
    *,
    model: FakeModel,
    tool: Any | None,
    typed_input: str,
    contract: AgentDecisionContract | None = None,
    preacquired_tool_receipts: tuple[dict[str, Any], ...] = (),
    require_tool_execution: bool = True,
) -> Any:
    agent = build_sdk_agent(
        name="synthetic_provider_specialist",
        instructions="Use bounded evidence and return the typed decision.",
        output_type=DecisionResult,
        tools=[tool] if tool is not None else [],
        enforce_tool_policy=False,
    )
    return run_typed_sdk_agent(
        agent=agent,
        typed_input=typed_input,
        output_type=DecisionResult,
        run_config=build_local_run_config(FakeProvider(model)),
        max_turns=4,
        preacquired_tool_receipts=preacquired_tool_receipts,
        tool_execution_contract=(
            ToolExecutionContract.required(
                ToolEvidenceGroup("provider_candidates", ("read_provider_candidates",)),
                stage="provider_candidate_selection",
            )
            if tool is not None and require_tool_execution
            else None
        ),
        decision_contract=contract or _decision_contract(),
    )


def test_rejected_decision_replays_evidence_without_repeating_provider_read() -> None:
    counter = {"calls": 0}

    def provider_output() -> dict[str, Any]:
        return {
            "status": "success",
            "operation": "read",
            "provider": "synthetic_provider",
            "provider_read": True,
            "item_count": 2,
            "identity_fingerprints": identity_fingerprints(
                ["candidate-current", "candidate-cancelled"]
            ),
            "candidates": [
                {
                    "candidate_id": "candidate-current",
                    "subject": "Current interview invitation",
                    "scheduled_at": "2026-08-04T10:00:00-04:00",
                    "lifecycle": "active",
                    "summary": "Confirmed for tomorrow at 10 AM Eastern.",
                },
                {
                    "candidate_id": "candidate-cancelled",
                    "subject": "Earlier interview invitation",
                    "scheduled_at": "2026-08-03T09:00:00-04:00",
                    "lifecycle": "cancelled",
                    "summary": "This earlier time was cancelled.",
                },
            ],
            "raw_mime": "must-not-be-replayed",
            "access_token": "must-not-be-replayed",
        }

    model = FakeModel(
        outputs=[
            [_tool_call()],
            [_structured_message(_decision_payload("candidate-invented"))],
            [_structured_message(_decision_payload("candidate-current"))],
        ]
    )

    result = _run(
        model=model,
        tool=_provider_tool(counter=counter, output_factory=provider_output),
        typed_input="Find the active interview candidate and explain the exclusion.",
    )

    assert counter["calls"] == 1
    assert len(model.calls) == 3
    initial_input = _model_input_text(model.calls[0]["input"])
    assert "decision_owner: specialist_agent" in initial_input
    assert "decision_stage: provider_candidate_selection" in initial_input
    assert model.calls[0]["tool_names"] == ["read_provider_candidates"]
    assert model.calls[2]["tool_names"] == []
    repair_input = _model_input_text(model.calls[2]["input"])
    assert "Sanitized decision evidence replay" in repair_input
    assert "Current interview invitation" in repair_input
    assert "Earlier interview invitation" in repair_input
    assert "active" in repair_input
    assert "cancelled" in repair_input
    assert "must-not-be-replayed" not in repair_input

    replay = result.request_cache["decision_repair_evidence"]
    assert replay["status"] == "ready"
    assert replay["mode"] == "sanitized_tool_evidence_replay"
    assert replay["provider_calls_during_repair"] == 0
    assert replay["source_tool_call_count"] == 1
    assert replay["replayed_tool_names"] == ["read_provider_candidates"]
    assert replay["disabled_read_tool_names"] == ["read_provider_candidates"]
    assert len(replay["evidence_fingerprints"]) == 1
    assert len(replay["aggregate_evidence_fingerprint"]) == 64
    assert "Current interview invitation" not in json.dumps(replay)
    ownership = result.request_cache["decision_ownership"]
    assert ownership["validator_outcome"]["status"] == "accepted"
    assert ownership["attempts"][1]["tool_mode"] == "verified_context_tool_free"
    assert result.request_cache["tool_execution"]["model_tool_call_count"] == 1
    assert result.final_output.selected_candidate_id == "candidate-current"


def test_oversized_search_evidence_is_compacted_without_losing_candidates() -> None:
    counter = {"calls": 0}

    def provider_output() -> dict[str, Any]:
        noisy_candidates = [
            {
                "candidate_id": f"candidate-current-noise-{index}",
                "title": f"Unrelated provider result {index}",
                "snippet": "Repeated provider detail. " * 100,
                "url": f"https://example.test/noise/{index}",
            }
            for index in range(100)
        ]
        return {
            **{f"access_token_{index}": "must-not-survive" for index in range(35)},
            "status": "success",
            "operation": "search",
            "provider_read": True,
            "provider_diagnostics": ["Provider diagnostic detail. " * 100] * 25,
            "expanded_query_diagnostics": ["Expanded query detail. " * 100] * 25,
            "candidates": [
                *noisy_candidates,
                {
                    "candidate_id": "candidate-current",
                    "title": "Current interview invitation",
                    "snippet": "Confirmed for tomorrow at 10 AM Eastern.",
                    "url": "https://example.test/current",
                },
                {
                    "candidate_id": "candidate-cancelled",
                    "title": "Earlier interview invitation",
                    "snippet": "This earlier time was cancelled.",
                    "url": "https://example.test/cancelled",
                },
            ],
            "identity_fingerprints": identity_fingerprints(
                ["candidate-current", "candidate-cancelled"]
            ),
        }

    model = FakeModel(
        outputs=[
            [_tool_call()],
            [_structured_message(_decision_payload("candidate-invented"))],
            [_structured_message(_decision_payload("candidate-current"))],
        ]
    )

    result = _run(
        model=model,
        tool=_provider_tool(counter=counter, output_factory=provider_output),
        typed_input="Choose the current candidate from a noisy provider result set.",
    )

    assert counter["calls"] == 1
    assert len(model.calls) == 3
    repair_input = _model_input_text(model.calls[2]["input"])
    assert len(repair_input) < 50_000
    assert "candidate-current" in repair_input
    assert "Current interview invitation" in repair_input
    assert "candidate-cancelled" in repair_input
    assert "Earlier interview invitation" in repair_input
    assert "must-not-survive" not in repair_input
    replay_telemetry = result.request_cache["decision_repair_evidence"]
    assert replay_telemetry["status"] == "ready"
    replay_payload = json.loads(repair_input.rsplit("Replayed tool evidence: ", 1)[1])
    replay_fingerprints = [
        sha256(
            json.dumps(
                entry["output"],
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        for entry in replay_payload
    ]
    assert replay_telemetry["evidence_fingerprints"] == replay_fingerprints
    assert result.final_output.selected_candidate_id == "candidate-current"


def test_multi_search_repair_keeps_only_candidate_bearing_outputs() -> None:
    counter = {"calls": 0}

    @function_tool
    def read_provider_candidates(query: str) -> str:
        """Return one of several independently bounded synthetic searches."""

        counter["calls"] += 1
        index = int(query.rsplit("-", 1)[-1])
        candidate = None
        if index == 0:
            candidate = {
                "candidate_id": "candidate-current",
                "title": "Current interview invitation",
                "snippet": "Confirmed for tomorrow at 10 AM Eastern.",
            }
        elif index == 1:
            candidate = {
                "candidate_id": "candidate-cancelled",
                "title": "Earlier interview invitation",
                "snippet": "This earlier time was cancelled.",
            }
        payload = {
            "status": "success",
            "operation": "search",
            "provider_read": True,
            "identity_fingerprints": identity_fingerprints(
                ["candidate-current", "candidate-cancelled"]
                if candidate is not None
                else []
            ),
            "candidates": [candidate] if candidate is not None else [],
        }
        for diagnostic_index in range(18):
            payload[f"provider_diagnostic_{diagnostic_index}"] = [
                f"Search {index} diagnostic {diagnostic_index}. " * 100
            ] * 25
        return json.dumps(payload, ensure_ascii=True, sort_keys=True)

    model = FakeModel(
        outputs=[
            [
                _tool_call(call_id=f"provider-read-{index}", query=f"search-{index}")
                for index in range(12)
            ],
            [_structured_message(_decision_payload("candidate-invented"))],
            [_structured_message(_decision_payload("candidate-current"))],
        ]
    )

    result = _run(
        model=model,
        tool=read_provider_candidates,
        typed_input="Choose between the two supported candidates after broad search.",
    )

    assert counter["calls"] == 12
    assert len(model.calls) == 3
    repair_input = _model_input_text(model.calls[2]["input"])
    assert len(repair_input) < 50_000
    assert "candidate-current" in repair_input
    assert "Current interview invitation" in repair_input
    assert "candidate-cancelled" in repair_input
    assert "Earlier interview invitation" in repair_input
    assert "Search 11 diagnostic" not in repair_input
    replay = result.request_cache["decision_repair_evidence"]
    assert replay["status"] == "ready"
    assert replay["provider_calls_during_repair"] == 0
    assert replay["source_tool_call_count"] == 12
    assert replay["replayed_output_count"] == 2
    assert replay["replayed_tool_names"] == ["read_provider_candidates"]
    assert replay["compaction_applied"] is True
    assert replay["source_serialized_chars"] > replay["replay_serialized_chars"]
    assert replay["candidate_record_count"] == 2
    assert result.final_output.selected_candidate_id == "candidate-current"


def test_opportunity_repair_preserves_decision_fields_and_official_url() -> None:
    counter = {"calls": 0}

    def provider_output() -> dict[str, Any]:
        return {
            **{
                f"provider_diagnostic_{index}": "Noisy search trace. " * 2_000
                for index in range(35)
            },
            "status": "success",
            "operation": "search",
            "provider_read": True,
            "candidates": [
                {
                    "candidate_id": "candidate-current",
                    "title": "Behavioral Health Innovation Pilot",
                    "deadline": "2026-09-30",
                    "eligibility": "Small U.S. research consultancies may apply.",
                    "fit_evidence": "Calls for behavioral-health AI evaluation support.",
                    "url": "https://example.test/opportunities/current",
                    "diagnostics": "Repeated provider detail. " * 4_000,
                },
                {
                    "candidate_id": "candidate-cancelled",
                    "title": "Closed Behavioral Health Call",
                    "deadline": "2026-01-15",
                    "eligibility": "Universities only.",
                    "fit_evidence": "The call is closed and not consultancy eligible.",
                    "url": "https://example.test/opportunities/closed",
                    "diagnostics": "Repeated provider detail. " * 4_000,
                },
            ],
            "identity_fingerprints": identity_fingerprints(
                ["candidate-current", "candidate-cancelled"]
            ),
        }

    model = FakeModel(
        outputs=[
            [_tool_call()],
            [_structured_message(_decision_payload("candidate-invented"))],
            [_structured_message(_decision_payload("candidate-current"))],
        ]
    )

    result = _run(
        model=model,
        tool=_provider_tool(counter=counter, output_factory=provider_output),
        typed_input="Choose the best supported open opportunity without rereading providers.",
    )

    assert counter["calls"] == 1
    repair_input = _model_input_text(model.calls[2]["input"])
    assert len(repair_input) < 50_000
    assert "2026-09-30" in repair_input
    assert "Small U.S. research consultancies may apply." in repair_input
    assert "behavioral-health AI evaluation support" in repair_input
    assert "https://example.test/opportunities/current" in repair_input
    replay = result.request_cache["decision_repair_evidence"]
    assert replay["compaction_applied"] is True
    assert replay["provider_calls_during_repair"] == 0
    assert result.final_output.selected_candidate_id == "candidate-current"


def test_tool_correction_then_decision_repair_retains_provider_evidence() -> None:
    counters = {"reads": 0, "scores": 0}

    @function_tool
    def read_provider_candidates(query: str) -> str:
        """Read a bounded synthetic provider candidate set."""

        counters["reads"] += 1
        return json.dumps(
            {
                "status": "success",
                "operation": "search",
                "provider_read": True,
                "candidates": [
                    {
                        "candidate_id": "candidate-current",
                        "title": "Current interview invitation",
                        "summary": "Confirmed for tomorrow at 10 AM Eastern.",
                    },
                    {
                        "candidate_id": "candidate-cancelled",
                        "title": "Earlier interview invitation",
                        "summary": "This earlier time was cancelled.",
                    },
                ],
                "identity_fingerprints": identity_fingerprints(
                    ["candidate-current", "candidate-cancelled"]
                ),
            },
            ensure_ascii=True,
            sort_keys=True,
        )

    @function_tool
    def score_candidate(candidate_id: str) -> str:
        """Score one already retrieved candidate without provider access."""

        counters["scores"] += 1
        return json.dumps(
            {"candidate_id": candidate_id, "score": 91, "status": "success"},
            sort_keys=True,
        )

    invalid_owner = _decision_payload("candidate-current")
    invalid_owner["decision"]["decision_owner"] = "orchestrator"
    model = FakeModel(
        outputs=[
            [_tool_call()],
            [_structured_message(invalid_owner)],
            [_score_call()],
            [_structured_message(invalid_owner)],
            [_structured_message(_decision_payload("candidate-current"))],
        ]
    )
    agent = build_sdk_agent(
        name="synthetic_provider_specialist",
        instructions="Use bounded evidence and return the typed decision.",
        output_type=DecisionResult,
        tools=[read_provider_candidates, score_candidate],
        enforce_tool_policy=False,
    )

    result = run_typed_sdk_agent(
        agent=agent,
        typed_input="Read, score, and select the current provider candidate.",
        output_type=DecisionResult,
        run_config=build_local_run_config(FakeProvider(model)),
        max_turns=4,
        tool_execution_contract=ToolExecutionContract.required(
            ToolEvidenceGroup(
                "provider_candidates",
                ("read_provider_candidates",),
            ),
            ToolEvidenceGroup("deterministic_score", ("score_candidate",)),
            stage="provider_candidate_selection",
        ),
        decision_contract=_decision_contract(),
    )

    assert counters == {"reads": 1, "scores": 1}
    assert len(model.calls) == 5
    decision_repair_input = _model_input_text(model.calls[4]["input"])
    assert "Sanitized decision evidence replay" in decision_repair_input
    assert "Current interview invitation" in decision_repair_input
    assert "Earlier interview invitation" in decision_repair_input
    assert result.request_cache["tool_execution_correction"]["evidence_replay"][
        "provider_calls_during_repair"
    ] == 0
    assert result.request_cache["decision_repair_evidence"][
        "provider_calls_during_repair"
    ] == 0
    assert result.final_output.selected_candidate_id == "candidate-current"


def test_successive_provider_reads_accumulate_before_decision_repair() -> None:
    counters = {"searches": 0, "details": 0}

    @function_tool
    def read_provider_candidates(query: str) -> str:
        """Read a bounded synthetic provider candidate set."""

        counters["searches"] += 1
        return json.dumps(
            {
                "status": "success",
                "operation": "search",
                "provider_read": True,
                "candidates": [
                    {
                        "candidate_id": "candidate-current",
                        "title": "Current interview invitation",
                        "summary": "Confirmed for tomorrow at 10 AM Eastern.",
                    }
                ],
                "identity_fingerprints": identity_fingerprints(["candidate-current"]),
            },
            sort_keys=True,
        )

    @function_tool
    def read_provider_candidate_detail(candidate_id: str) -> str:
        """Read bounded lifecycle detail for one different provider candidate."""

        counters["details"] += 1
        return json.dumps(
            {
                "status": "success",
                "operation": "read",
                "provider_read": True,
                "candidate": {
                    "candidate_id": candidate_id,
                    "title": "Earlier interview invitation",
                    "summary": "This earlier time was cancelled.",
                },
                "identity_fingerprints": identity_fingerprints([candidate_id]),
            },
            sort_keys=True,
        )

    invalid_owner = _decision_payload("candidate-current")
    invalid_owner["decision"]["decision_owner"] = "orchestrator"
    model = FakeModel(
        outputs=[
            [_tool_call()],
            [_structured_message(invalid_owner)],
            [_provider_detail_call()],
            [_structured_message(invalid_owner)],
            [_structured_message(_decision_payload("candidate-current"))],
        ]
    )
    agent = build_sdk_agent(
        name="synthetic_provider_specialist",
        instructions="Use all bounded evidence and return the typed decision.",
        output_type=DecisionResult,
        tools=[read_provider_candidates, read_provider_candidate_detail],
        enforce_tool_policy=False,
    )

    result = run_typed_sdk_agent(
        agent=agent,
        typed_input="Compare both provider candidates and select the active one.",
        output_type=DecisionResult,
        run_config=build_local_run_config(FakeProvider(model)),
        max_turns=4,
        tool_execution_contract=ToolExecutionContract.required(
            ToolEvidenceGroup("candidate_search", ("read_provider_candidates",)),
            ToolEvidenceGroup(
                "candidate_detail",
                ("read_provider_candidate_detail",),
            ),
            stage="provider_candidate_selection",
        ),
        decision_contract=_decision_contract(),
    )

    assert counters == {"searches": 1, "details": 1}
    assert len(model.calls) == 5
    assert model.calls[2]["tool_names"] == ["read_provider_candidate_detail"]
    assert model.calls[4]["tool_names"] == []
    repair_input = _model_input_text(model.calls[4]["input"])
    assert "Current interview invitation" in repair_input
    assert "Earlier interview invitation" in repair_input
    replay = result.request_cache["decision_repair_evidence"]
    assert replay["source_tool_call_count"] == 2
    assert replay["provider_calls_during_repair"] == 0
    assert replay["replayed_tool_names"] == [
        "read_provider_candidates",
        "read_provider_candidate_detail",
    ]


def test_completed_mutation_is_not_repeated_during_correction_or_decision_repair() -> None:
    counters = {"creates": 0, "validations": 0}

    @function_tool
    def create_test_candidate(candidate_id: str) -> str:
        """Create one bounded synthetic test candidate."""

        counters["creates"] += 1
        return json.dumps(
            {
                "status": "success",
                "operation": "create",
                "candidate_id": candidate_id,
                "identity_fingerprints": identity_fingerprints([candidate_id]),
            },
            sort_keys=True,
        )

    @function_tool
    def validate_test_candidate(candidate_id: str) -> str:
        """Validate the already-created synthetic candidate without mutation."""

        counters["validations"] += 1
        return json.dumps(
            {
                "status": "success",
                "operation": "validate",
                "candidate_id": candidate_id,
                "identity_fingerprints": identity_fingerprints([candidate_id]),
            },
            sort_keys=True,
        )

    invalid_owner = _decision_payload("candidate-current")
    invalid_owner["decision"]["decision_owner"] = "orchestrator"
    model = FakeModel(
        outputs=[
            [_mutation_call()],
            [_structured_message(invalid_owner)],
            [_validation_call()],
            [_structured_message(invalid_owner)],
            [_structured_message(_decision_payload("candidate-current"))],
        ]
    )
    agent = build_sdk_agent(
        name="synthetic_mutation_specialist",
        instructions="Create once, validate, and return the typed decision.",
        output_type=DecisionResult,
        tools=[create_test_candidate, validate_test_candidate],
        enforce_tool_policy=False,
    )

    result = run_typed_sdk_agent(
        agent=agent,
        typed_input="Create and validate exactly one synthetic candidate.",
        output_type=DecisionResult,
        run_config=build_local_run_config(FakeProvider(model)),
        max_turns=4,
        tool_execution_contract=ToolExecutionContract.required(
            ToolEvidenceGroup("mutation", ("create_test_candidate",)),
            ToolEvidenceGroup("validation", ("validate_test_candidate",)),
            stage="synthetic_mutation",
        ),
        preacquired_tool_receipts=(
            {
                "tool_name": "synthetic_candidate_identity",
                "status": "success",
                "identity_fingerprints": identity_fingerprints(["candidate-current"]),
            },
        ),
        decision_contract=_decision_contract(),
    )

    assert counters == {"creates": 1, "validations": 1}
    assert "create_test_candidate" not in model.calls[2]["tool_names"]
    assert "create_test_candidate" not in model.calls[4]["tool_names"]
    assert result.request_cache["decision_ownership"]["validator_outcome"][
        "status"
    ] == "accepted"


def test_decision_repair_requires_description_for_each_candidate() -> None:
    counter = {"calls": 0}

    def partly_descriptive_output() -> dict[str, Any]:
        return {
            "status": "success",
            "operation": "read",
            "provider_read": True,
            "candidates": [
                {
                    "candidate_id": "candidate-current",
                    "subject": "Current interview invitation",
                    "summary": "Confirmed for tomorrow at 10 AM Eastern.",
                },
                {"candidate_id": "candidate-cancelled"},
            ],
            "identity_fingerprints": identity_fingerprints(
                ["candidate-current", "candidate-cancelled"]
            ),
        }

    model = FakeModel(
        outputs=[
            [_tool_call()],
            [_structured_message(_decision_payload("candidate-invented"))],
        ]
    )

    with pytest.raises(AgentDecisionValidationError) as exc_info:
        _run(
            model=model,
            tool=_provider_tool(counter=counter, output_factory=partly_descriptive_output),
            typed_input="Compare both candidates.",
        )

    assert counter["calls"] == 1
    assert len(model.calls) == 2
    replay = sdk_run_failure_metadata(exc_info.value)["request_cache"][
        "decision_repair_evidence"
    ]
    assert replay["reason_code"] == "decision_repair_candidate_evidence_not_descriptive"
    assert replay["provider_calls_during_repair"] == 0


def test_irreducible_replay_size_fails_without_a_repair_or_reread() -> None:
    counter = {"calls": 0}

    def irreducible_output() -> dict[str, Any]:
        return {
            "status": "success",
            "operation": "search",
            "provider_read": True,
            "candidates": [
                {
                    "candidate_id": "candidate-current",
                    "title": f"Current candidate evidence copy {index}",
                    "snippet": "Distinct descriptive evidence. " * 20,
                    "url": f"https://example.test/current/{index}",
                }
                for index in range(500)
            ],
            "identity_fingerprints": identity_fingerprints(["candidate-current"]),
        }

    model = FakeModel(
        outputs=[
            [_tool_call()],
            [_structured_message(_decision_payload("candidate-invented"))],
        ]
    )

    with pytest.raises(AgentDecisionValidationError) as exc_info:
        _run(
            model=model,
            tool=_provider_tool(counter=counter, output_factory=irreducible_output),
            typed_input="Choose from an irreducibly repeated result set.",
        )

    assert counter["calls"] == 1
    assert len(model.calls) == 2
    replay = sdk_run_failure_metadata(exc_info.value)["request_cache"][
        "decision_repair_evidence"
    ]
    assert replay["reason_code"] == "decision_repair_evidence_size_limit_exceeded"
    assert replay["provider_calls_during_repair"] == 0


def test_decision_repair_fails_closed_when_read_replay_is_id_only() -> None:
    counter = {"calls": 0}

    def id_only_output() -> dict[str, Any]:
        return {
            "status": "success",
            "operation": "read",
            "provider_read": True,
            "candidate_ids": ["candidate-current", "candidate-cancelled"],
            "identity_fingerprints": identity_fingerprints(
                ["candidate-current", "candidate-cancelled"]
            ),
        }

    model = FakeModel(
        outputs=[
            [_tool_call()],
            [_structured_message(_decision_payload("candidate-invented"))],
        ]
    )

    with pytest.raises(AgentDecisionValidationError) as exc_info:
        _run(
            model=model,
            tool=_provider_tool(counter=counter, output_factory=id_only_output),
            typed_input="Choose the active candidate.",
        )

    assert counter["calls"] == 1
    assert len(model.calls) == 2
    metadata = sdk_run_failure_metadata(exc_info.value)
    replay = metadata["request_cache"]["decision_repair_evidence"]
    assert replay["status"] == "unsafe"
    assert replay["reason_code"] == "decision_repair_evidence_not_descriptive"
    assert replay["provider_calls_during_repair"] == 0
    assert metadata["request_cache"]["decision_ownership"]["validator_outcome"][
        "reason_code"
    ] == "decision_repair_evidence_not_descriptive"


def test_preacquired_context_keeps_existing_tool_free_repair_behavior() -> None:
    typed_input = (
        "Preacquired verified context: candidate-current is the active invitation; "
        "candidate-cancelled is an earlier cancelled invitation."
    )
    contract = AgentDecisionContract(
        route="synthetic_provider_specialist",
        decision_stage="provider_candidate_selection",
        evidence_resolver=_decision_contract().evidence_resolver,
        pre_model_candidate_ids=("candidate-current", "candidate-cancelled"),
        mandatory_pre_model_context_ids=("candidate-current", "candidate-cancelled"),
        pre_model_context_source="preacquired_verified_context",
    )
    model = FakeModel(
        outputs=[
            [_structured_message(_decision_payload("candidate-invented"))],
            [_structured_message(_decision_payload("candidate-current"))],
        ]
    )
    counter = {"calls": 0}

    result = _run(
        model=model,
        tool=_provider_tool(
            counter=counter,
            output_factory=lambda: {
                "status": "success",
                "operation": "read",
                "provider_read": True,
            },
        ),
        typed_input=typed_input,
        contract=contract,
        preacquired_tool_receipts=(
            {
                "tool_name": "workflow_calendar_context",
                "status": "success",
                "provider_read": True,
                "identity_fingerprints": identity_fingerprints(
                    ["candidate-current", "candidate-cancelled"]
                ),
            },
        ),
        require_tool_execution=False,
    )

    assert len(model.calls) == 2
    assert model.calls[0]["tool_names"] == ["read_provider_candidates"]
    assert model.calls[1]["tool_names"] == []
    assert counter["calls"] == 0
    assert "bounded repair attempt" in str(model.calls[1]["input"])
    assert "decision_repair_evidence" not in result.request_cache
    assert result.request_cache["decision_ownership"]["validator_outcome"][
        "status"
    ] == "accepted"
