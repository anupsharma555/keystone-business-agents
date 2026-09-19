"""Real SDK controls for safe, model-owned structured-output correction."""

from __future__ import annotations

import json

import pytest
from agents import _debug
from agents.models.interface import Model, ModelProvider, ModelResponse
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from pydantic import BaseModel, model_validator

from keystone_agents.model_provider import ModelConfig
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.runtime.output_validation import sanitized_output_diagnostics
from keystone_agents.runtime.request_budget import activate_model_request_budget
from keystone_agents.schemas.decision_ownership import AgentDecisionRecord
from keystone_agents.sdk import build_local_run_config, build_sdk_agent


class ScriptedModel(Model):
    def __init__(self, first, second, assert_retry):
        self.outputs = [first, second]
        self.inputs = []
        self.schemas = []
        self.assert_retry = assert_retry

    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        **kwargs,
    ):
        text = input if isinstance(input, str) else "\n".join(
            item["content"] for item in input if isinstance(item.get("content"), str)
        )
        self.inputs.append(text)
        self.schemas.append(output_schema.json_schema())
        assert tools == []
        if len(self.inputs) == 2:
            assert self.inputs[1].startswith(self.inputs[0])
            assert self.schemas[1] == self.schemas[0]
            self.assert_retry(self.inputs[1])
        output = self.outputs.pop(0)
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id=f"synthetic-{len(self.inputs)}",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(
                            type="output_text", text=json.dumps(output), annotations=[]
                        )
                    ],
                )
            ],
            usage=Usage(requests=1, input_tokens=10, output_tokens=10, total_tokens=20),
            response_id=f"synthetic-response-{len(self.inputs)}",
        )

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class Provider(ModelProvider):
    def __init__(self, model):
        self.model = model

    def get_model(self, model_name):
        return self.model


def run(output_type, model, request):
    with activate_model_request_budget(2) as budget:
        result = run_typed_sdk_agent(
            agent=build_sdk_agent(
                "guided_retry_probe",
                "Follow the original task and schema.",
                output_type,
                tools=[],
                model="gpt-5.4-mini",
            ),
            typed_input=request,
            output_type=output_type,
            live=False,
            run_config=build_local_run_config(Provider(model)),
            config=ModelConfig(provider="openai", model="gpt-5.4-mini"),
            inherit_env_session=False,
            max_turns=1,
        )
        assert budget.consumed == len(model.inputs) == 2
    assert result.request_cache["structured_output_retries"] == 1
    return result


@pytest.fixture(autouse=True)
def existing_single_retry(monkeypatch):
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    # Exercise the existing one-retry live policy with in-process SDK models.
    monkeypatch.setattr("keystone_agents.run._sdk_structured_output_max_retries", lambda **_: 1)


def decision(*ids):
    return AgentDecisionRecord(
        decision_owner="orchestrator",
        decision_stage="synthetic_selection",
        selected_candidate_id=ids[0],
        selected_candidate_ids=list(ids),
        candidate_assessments=[
            {
                "candidate_id": identity,
                "disposition": "selected",
                "rationale": "Synthetic evidence.",
            }
            for identity in ids
        ],
        reasoning="Model-selected synthetic alternatives.",
    ).model_dump(mode="json")


def test_confirmed_set_mismatch_receives_static_rule_without_replaying_failed_values():
    private = "synthetic_private_failed_model_identity"
    invalid = decision("candidate_a", "candidate_b")
    invalid["selected_candidate_id"] = private
    invalid["selected_candidate_ids"] = ["candidate_a"]
    invalid["reasoning"] = private
    valid = decision("candidate_b", "candidate_c")
    request = {
        "raw_request": "Choose two evidence-backed alternatives; no provider writes.",
        "evidence": [
            {"candidate_id": name, "claim": "Verified synthetic source."}
            for name in ("candidate_a", "candidate_b", "candidate_c")
        ],
        "permissions": {"provider_writes": False},
    }

    def assert_retry(prompt):
        assert "decision_selected_set_mismatch" in prompt
        assert "set union of non-empty selected_candidate_id and selected_candidate_ids" in prompt
        assert "candidate_assessments whose disposition is 'selected'" in prompt
        assert "Choose the identities, dispositions, and alternatives yourself" in prompt
        assert "This correction does not authorize additional provider actions." in prompt
        assert private not in prompt
        assert json.dumps(request, ensure_ascii=True, sort_keys=True) in prompt

    result = run(AgentDecisionRecord, ScriptedModel(invalid, valid, assert_retry), request)
    assert result.output.selected_candidate_ids == ["candidate_b", "candidate_c"]
    diagnostics = result.request_cache["validation_diagnostics"]
    assert diagnostics["failures"][0]["errors"][0]["rule_code"] == "decision_selected_set_mismatch"
    assert (
        sanitized_output_diagnostics(diagnostics)["failures"][0]["errors"][0]["rule_code"]
        == "decision_selected_set_mismatch"
    )
    assert private not in json.dumps(result.request_cache)


@pytest.mark.parametrize("failure", ["same_literal_from_other_validator", "unknown_field_error"])
def test_unreviewed_errors_receive_only_safe_path_and_type(failure):
    class Output(BaseModel):
        value: int

        @model_validator(mode="after")
        def other_validator(self):
            if self.value < 0:
                raise ValueError("Selected identity fields must match selected assessments.")
            return self

    private = "synthetic_private_failed_value"
    invalid = {"value": -1 if failure == "same_literal_from_other_validator" else private}

    def assert_retry(prompt):
        assert "Structured output correction under the existing schema" in prompt
        assert "decision_selected_set_mismatch" not in prompt
        assert "Selected identity fields must match selected assessments." not in prompt
        assert "set union" not in prompt
        assert private not in prompt
        expected = (
            "root: value_error"
            if failure == "same_literal_from_other_validator"
            else "value: int_type"
        )
        assert expected in prompt

    result = run(
        Output, ScriptedModel(invalid, {"value": 1}, assert_retry), "Original bounded task"
    )
    assert result.output.value == 1
    assert (
        "rule_code"
        not in result.request_cache["validation_diagnostics"]["failures"][0]["errors"][0]
    )
