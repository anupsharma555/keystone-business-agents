"""Replay completion requires exact call-boundary attestation, not callback assertions."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from keystone_agents.receipts.journal import (
    instrument_agent_tools,
    reset_tool_receipt_journal,
    tool_invocation_journal,
)
from keystone_agents.run import _verified_replayed_read_tool_names


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "changed_arguments",
        "changed_output",
        "missing_arguments",
        "changed_name",
        "attached_only",
        "attempted_only",
        "unsuccessful",
        "blocked",
        "failure_flag",
        "exception",
        "mutation",
        "not_replayed",
    ],
)
def test_only_exact_successful_replayed_reads_satisfy_prior_execution(fault):
    arguments = {"resource_id": "synthetic-record-17"}
    payload = {"status": "read", "resource_id": "synthetic-record-17", "evidence": "bounded"}
    if fault == "unsuccessful":
        payload["status"] = "not_found"
    elif fault == "blocked":
        payload["status"] = "blocked"
    elif fault == "failure_flag":
        payload["success"] = False
    name = "apply_gmail_labels" if fault == "mutation" else "read_synthetic_context"
    provider_calls = []

    async def callback(_context, raw_arguments):
        provider_calls.append(json.loads(raw_arguments))
        if fault == "exception":
            raise RuntimeError("Synthetic provider failure.")
        return json.dumps(payload)

    tool = SimpleNamespace(name=name, on_invoke_tool=callback, is_enabled=True)
    reset_tool_receipt_journal()
    instrument_agent_tools(SimpleNamespace(tools=[tool]))
    if fault != "attached_only":
        try:
            asyncio.run(tool.on_invoke_tool(None, json.dumps(arguments)))
        except RuntimeError:
            assert fault == "exception"
    journal = tool_invocation_journal()
    entry = {"tool_name": name, "arguments": deepcopy(arguments), "output": deepcopy(payload)}
    if fault == "changed_arguments":
        entry["arguments"]["resource_id"] = "another-record"
    elif fault == "changed_output":
        entry["output"]["resource_id"] = "another-record"
    elif fault == "missing_arguments":
        entry.pop("arguments")
    elif fault == "changed_name":
        entry["tool_name"] = "read_other_context"
    elif fault == "attempted_only":
        journal = [item for item in journal if item["status"] == "started"]
    replayed = () if fault == "not_replayed" else (entry, entry)
    assert _verified_replayed_read_tool_names(replayed, journal) == (
        (name,) if fault == "none" else ()
    )
    assert len(provider_calls) == (0 if fault == "attached_only" else 1)
    serialized_journal = json.dumps(journal)
    assert "synthetic-record-17" not in serialized_journal
    assert "bounded" not in serialized_journal


def test_throttle_after_structured_retry_preserves_the_same_verified_evidence(monkeypatch):
    from keystone_agents.model_provider import ModelConfig
    from keystone_agents.run import run_typed_sdk_agent
    from keystone_agents.runtime.tool_execution import ToolEvidenceGroup, ToolExecutionContract
    from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult

    class ModelBehaviorError(Exception):
        pass

    class RateLimitError(Exception):
        status_code = 429

    arguments = {"resource_id": "synthetic-record-17"}
    payload = {"status": "read", "resource_id": "synthetic-record-17", "evidence": "bounded"}
    provider_calls = []

    async def read(_context, raw_arguments):
        provider_calls.append(json.loads(raw_arguments))
        return json.dumps(payload)

    tool = SimpleNamespace(name="read_synthetic_context", on_invoke_tool=read, is_enabled=True)
    agent = SimpleNamespace(name="chief_of_staff", model="gpt-5.4-mini", tools=[tool])
    prompts = []

    def sdk_attempt(agent, prompt, *args, **kwargs):
        prompts.append(str(prompt))
        if len(prompts) == 1:
            asyncio.run(agent.tools[0].on_invoke_tool(None, json.dumps(arguments)))
            raise ModelBehaviorError("Invalid structured output.")
        assert "Verified model tool evidence" in str(prompt)
        assert "synthetic-record-17" in str(prompt)
        assert "bounded" in str(prompt)
        assert agent.tools[0].is_enabled is False
        if len(prompts) == 2:
            raise RateLimitError("429 rate limit")
        return SimpleNamespace(
            new_items=[],
            usage={"requests": 1, "input_tokens": 30, "output_tokens": 10},
        ), ChiefOfStaffResult(
            mode="llm",
            summary="Recovered from the original bounded evidence.",
        )

    monkeypatch.setattr("keystone_agents.run.run_typed_sdk_sync", sdk_attempt)
    monkeypatch.setattr("keystone_agents.run._sdk_structured_output_max_retries", lambda **_: 1)
    monkeypatch.setattr("keystone_agents.run._sdk_rate_limit_max_retries", lambda **_: 1)
    monkeypatch.setattr("keystone_agents.run._sdk_rate_limit_retry_delay_seconds", lambda *_: 0)
    result = run_typed_sdk_agent(
        agent=agent,
        typed_input={"request": "Review supplied evidence."},
        output_type=ChiefOfStaffResult,
        live=True,
        config=ModelConfig(provider="openai", model="gpt-5.4-mini", api_key="synthetic-test-key"),
        tool_execution_contract=ToolExecutionContract.required(
            ToolEvidenceGroup("bounded_read", (tool.name,)),
            stage="synthetic_review",
        ),
        structured_retry_evidence_provider=lambda: (
            {
                "tool_name": tool.name,
                "arguments": arguments,
                "output": payload,
            },
        ),
    )
    assert len(prompts) == 3 and len(provider_calls) == 1
    assert result.request_cache["structured_output_retries"] == 1
    assert result.request_cache["rate_limit_retries"] == 1
    assert not result.request_cache.get("tool_corrections")
    assert result.request_cache["tool_execution_postcondition"]["satisfied"] is True


def test_native_decision_repair_after_structured_retry_keeps_original_attested_read(monkeypatch):
    from agents import Agent, function_tool
    from agents.models.interface import Model, ModelProvider, ModelResponse
    from agents.usage import Usage
    from openai.types.responses import (
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
    )
    from pydantic import BaseModel

    from keystone_agents.run import run_typed_sdk_agent
    from keystone_agents.runtime.decision_validation import (
        AgentDecisionContract,
        SpecialistDecisionEvidence,
    )
    from keystone_agents.runtime.tool_execution import ToolEvidenceGroup, ToolExecutionContract
    from keystone_agents.schemas.decision_ownership import AgentDecisionRecord
    from keystone_agents.sdk import build_local_run_config

    class Selection(BaseModel):
        selected_record_id: str
        decision: AgentDecisionRecord

    record_id = "synthetic-record-17"
    evidence = {
        "status": "read",
        "record_id": record_id,
        "summary": "The current invitation concerns a bounded evaluation-design review.",
    }
    provider_calls = []

    @function_tool
    def read_synthetic_context(record_id: str) -> str:
        """Read one exact synthetic candidate record."""
        provider_calls.append(record_id)
        return json.dumps(evidence)

    class ScriptedModel(Model):
        calls = 0

        async def get_response(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                output = [
                    ResponseFunctionToolCall(
                        type="function_call",
                        name="read_synthetic_context",
                        call_id="attested-read",
                        arguments=json.dumps({"record_id": record_id}),
                    )
                ]
            else:
                if self.calls >= 3:
                    serialized = json.dumps(kwargs["input"])
                    assert record_id in serialized and evidence["summary"] in serialized
                    assert kwargs["tools"] == []
                selected = "invented-record" if self.calls == 3 else record_id
                selection = Selection(
                    selected_record_id=selected,
                    decision=AgentDecisionRecord(
                        decision_owner="specialist_agent",
                        decision_stage="record_selection",
                        selected_candidate_id=selected,
                        reasoning="Choose from the observed record.",
                        candidate_assessments=[
                            {
                                "candidate_id": record_id,
                                "disposition": "excluded" if self.calls == 3 else "selected",
                                "rationale": "The available record was assessed.",
                            }
                        ],
                    ),
                )
                output = [
                    ResponseOutputMessage(
                        id=f"selection-{self.calls}",
                        type="message",
                        role="assistant",
                        status="completed",
                        content=[
                            ResponseOutputText(
                                type="output_text",
                                text="{" if self.calls == 2 else selection.model_dump_json(),
                                annotations=[],
                            )
                        ],
                    )
                ]
            return ModelResponse(
                output=output,
                usage=Usage(requests=1, input_tokens=30, output_tokens=10),
                response_id=f"attested-response-{self.calls}",
            )

        def stream_response(self, *args, **kwargs):
            raise NotImplementedError

    class Provider(ModelProvider):
        def get_model(self, model_name):
            return model

    model = ScriptedModel()
    monkeypatch.setattr("keystone_agents.run._sdk_structured_output_max_retries", lambda **_: 1)
    result = run_typed_sdk_agent(
        agent=Agent(
            name="Synthetic selection",
            instructions="Select only observed evidence.",
            output_type=Selection,
            tools=[read_synthetic_context],
        ),
        typed_input={"request": "Read the current candidate and select the appropriate record."},
        output_type=Selection,
        run_config=build_local_run_config(Provider()),
        max_turns=3,
        decision_contract=AgentDecisionContract(
            route="synthetic_selection",
            decision_stage="record_selection",
            evidence_resolver=lambda output: SpecialistDecisionEvidence.build(
                (record_id,),
                required_selected_ids=(output.selected_record_id,),
            ),
        ),
        tool_execution_contract=ToolExecutionContract.required(
            ToolEvidenceGroup("selected_record", ("read_synthetic_context",)),
            stage="synthetic_read_selection",
        ),
        structured_retry_evidence_provider=lambda: (
            {
                "tool_name": "read_synthetic_context",
                "arguments": {"record_id": record_id},
                "output": evidence,
            },
        ),
    )
    assert model.calls == 4 and provider_calls == [record_id]
    assert result.output.selected_record_id == record_id
    assert result.usage["requests"] == 4
    assert result.request_cache["decision_repair_evidence"]["status"] == "ready"
    assert result.request_cache["decision_repair_evidence"]["replayed_output_count"] == 1
    assert result.request_cache["tool_execution_postcondition"]["satisfied"] is True
    assert result.request_cache["tool_execution"]["model_tool_call_count"] == 1
