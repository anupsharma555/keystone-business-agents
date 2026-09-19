from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from agents import _debug
from agents.agent_output import AgentOutputSchema
from agents.exceptions import ModelBehaviorError
from agents.models.interface import Model, ModelProvider, ModelResponse
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_core import PydanticCustomError

from keystone_agents.model_provider import ModelConfig
from keystone_agents.run import run_typed_sdk_agent, sdk_run_failure_metadata
from keystone_agents.runtime.output_validation import (
    OutputValidationDiagnostics,
    agent_with_output_diagnostics,
)
from keystone_agents.schemas.experiment_v2 import ExperimentReview
from keystone_agents.sdk import build_local_run_config, build_sdk_agent
from promptfoo.eval_database import list_eval_trace_events


class ModelStub(Model):
    def __init__(self, payloads, barrier=None):
        self.payloads = list(payloads)
        self.calls = 0
        self.barrier = barrier

    async def get_response(self, *args, **kwargs):
        self.calls += 1
        if self.barrier:
            self.barrier.wait(timeout=10)
        payload = self.payloads.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="synthetic-output",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(
                            type="output_text", text=json.dumps(payload), annotations=[]
                        )
                    ],
                )
            ],
            usage=Usage(requests=1, input_tokens=10, output_tokens=20, total_tokens=30),
            response_id=f"synthetic-{self.calls}",
        )

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class Provider(ModelProvider):
    def __init__(self, model):
        self.model = model

    def get_model(self, model_name):
        return self.model


def agent(output_type):
    return build_sdk_agent(
        "validation_probe",
        "Return the declared output.",
        output_type,
        tools=[],
        model="gpt-5.4-mini",
    )


def run(selected_agent, output_type, model):
    return run_typed_sdk_agent(
        agent=selected_agent,
        typed_input="Synthetic request",
        output_type=output_type,
        run_config=build_local_run_config(Provider(model)),
        live=False,
        config=ModelConfig(provider="openai", model="gpt-5.4-mini"),
        inherit_env_session=False,
    )


@pytest.fixture(autouse=True)
def private_sdk_defaults(monkeypatch):
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    monkeypatch.setenv("KEYSTONE_SDK_STRUCTURED_OUTPUT_MAX_RETRIES", "0")
    monkeypatch.setenv("KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES", "0")


def test_root_validator_diagnostics_reach_failure_metadata_and_retained_trace(
    tmp_path, monkeypatch
):
    database = tmp_path / "traces.sqlite"
    monkeypatch.setenv("KEYSTONE_TRACE_SUMMARY_DB", str(database))
    selected_agent = agent(ExperimentReview)
    with pytest.raises(ModelBehaviorError) as caught:
        run(
            selected_agent,
            ExperimentReview,
            ModelStub([{"findings": [], "recommendation": "revise"}]),
        )
    assert selected_agent.output_type is ExperimentReview
    assert caught.value.run_data is None
    failure = sdk_run_failure_metadata(caught.value)
    detail = failure["request_cache"]["validation_diagnostics"]["failures"][0]["errors"][0]
    assert detail["type"] == "value_error"
    assert detail["location"] == []
    assert detail["validator"]["file"] == "src/keystone_agents/schemas/experiment_v2.py"
    assert detail["validator"]["function"] == "consistent_review"
    assert detail["validator"]["line"] > 0
    assert "source-bound criticism" not in json.dumps(failure)
    rows = list_eval_trace_events(database_path=database)
    persisted = [row["metadata"] for row in rows if row["event_type"] == "sdk_run_summary"]
    assert persisted[-1]["validation_diagnostics"]["failures"][0]["errors"][0] == detail


@pytest.mark.parametrize("private_key", ["synthetic_private_customer_identifier", "mapping"])
def test_dynamic_dictionary_keys_and_values_never_enter_diagnostics(
    private_key, tmp_path, monkeypatch
):
    database = tmp_path / "private-diagnostics.sqlite"
    monkeypatch.setenv("KEYSTONE_TRACE_SUMMARY_DB", str(database))

    class Output(BaseModel):
        mapping: dict[str, list[int]]

    declared = AgentOutputSchema(Output, strict_json_schema=False)
    selected_agent = agent(declared)
    private_value = "synthetic_private_value_do_not_retain"
    with pytest.raises(ModelBehaviorError) as caught:
        run(selected_agent, Output, ModelStub([{"mapping": {private_key: [private_value]}}]))
    failure = sdk_run_failure_metadata(caught.value)
    detail = failure["request_cache"]["validation_diagnostics"]["failures"][0]["errors"][0]
    assert detail["location"] == ["mapping", "<dynamic_key>", 0]
    assert detail["type"] == "int_parsing"
    serialized = json.dumps(failure)
    if private_key != "mapping":
        assert private_key not in serialized
        assert private_key.encode() not in database.read_bytes()
    assert private_value not in serialized
    assert private_value.encode() not in database.read_bytes()
    assert selected_agent.output_type is declared


def test_custom_error_codes_messages_and_context_are_not_retained():
    class Output(BaseModel):
        marker: str

        @model_validator(mode="after")
        def reject(self):
            raise PydanticCustomError(self.marker, "Private {marker}", {"marker": self.marker})

    private = "synthetic_private_error_type"
    with pytest.raises(ModelBehaviorError) as caught:
        run(agent(Output), Output, ModelStub([{"marker": private}]))
    metadata = sdk_run_failure_metadata(caught.value)
    detail = metadata["request_cache"]["validation_diagnostics"]["failures"][0]["errors"][0]
    assert detail == {"type": "custom_validation_error", "location": []}
    assert private not in json.dumps(metadata)


def test_adapter_preserves_schema_acceptance_and_validates_once():
    calls = []

    class Output(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: int

        @model_validator(mode="after")
        def positive(self):
            calls.append(1)
            if self.value < 0:
                raise ValueError("synthetic private validator detail")
            return self

    selected_agent = agent(Output)
    collector = OutputValidationDiagnostics(Output)
    wrapped = agent_with_output_diagnostics(selected_agent, collector).output_type
    native = AgentOutputSchema(Output)
    assert wrapped.json_schema() == native.json_schema()
    assert wrapped.is_strict_json_schema() == native.is_strict_json_schema()
    assert wrapped.validate_json('{"value":1}') == native.validate_json('{"value":1}')
    calls.clear()
    with pytest.raises(ModelBehaviorError) as caught:
        wrapped.validate_json('{"value":-1}')
    assert calls == [1]
    assert len(collector.failures) == 1
    assert selected_agent.output_type is Output
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    frame = caught.value.__traceback__
    while frame:
        assert not frame.tb_frame.f_code.co_filename.endswith("runtime/output_validation.py")
        frame = frame.tb_next


def test_retry_records_safe_failure_and_agent_reuse_starts_clean(monkeypatch):
    class Output(BaseModel):
        value: int

    monkeypatch.setattr("keystone_agents.run._sdk_structured_output_max_retries", lambda **_: 1)
    selected_agent = agent(Output)
    model = ModelStub([{"value": "synthetic invalid"}, {"value": 2}])
    result = run(selected_agent, Output, model)
    assert model.calls == 2 and result.output.value == 2
    failures = result.request_cache["validation_diagnostics"]["failures"]
    assert len(failures) == 1 and failures[0]["attempt"] == 1
    assert failures[0]["errors"][0]["location"] == ["value"]
    clean = run(selected_agent, Output, ModelStub([{"value": 3}]))
    assert "validation_diagnostics" not in clean.request_cache
    assert selected_agent.output_type is Output


def test_concurrent_runs_of_one_agent_do_not_share_diagnostics():
    class Output(BaseModel):
        left: int
        right: int

    selected_agent = agent(Output)
    barrier = Barrier(2)

    def invoke(field):
        payload = {"left": 1, "right": 2, field: "synthetic private value"}
        try:
            run(selected_agent, Output, ModelStub([payload], barrier=barrier))
        except ModelBehaviorError as error:
            return sdk_run_failure_metadata(error)["request_cache"]["validation_diagnostics"]
        pytest.fail("Invalid output unexpectedly passed")

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = list(pool.map(invoke, ["left", "right"]))
    assert left["failures"][0]["errors"][0]["location"] == ["left"]
    assert right["failures"][0]["errors"][0]["location"] == ["right"]
    assert len(left["failures"]) == len(right["failures"]) == 1
    assert selected_agent.output_type is Output


def test_failure_before_type_adapter_does_not_infer_schema_or_truncation_cause():
    class Output(BaseModel):
        value: int

    private = "synthetic private failure before parser"
    with pytest.raises(ModelBehaviorError) as caught:
        run(agent(Output), Output, ModelStub([ModelBehaviorError(private)]))
    metadata = sdk_run_failure_metadata(caught.value)
    diagnostic = metadata["request_cache"]["validation_diagnostics"]["failures"][0]
    assert diagnostic["status"] == "no_schema_diagnostic_observed"
    assert diagnostic["errors"] == []
    assert private not in json.dumps(metadata)
