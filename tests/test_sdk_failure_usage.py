"""Real SDK failure accounting, using only deterministic in-process models."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Barrier
from typing import Any

import pytest
from agents import _debug
from agents.exceptions import ModelBehaviorError
from agents.models.interface import Model, ModelProvider, ModelResponse
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from pydantic import BaseModel

from keystone_agents.model_provider import ModelConfig
from keystone_agents.run import run_typed_sdk_agent, sdk_run_failure_metadata
from keystone_agents.runtime.request_budget import (
    ModelRequestBudgetExhausted,
    activate_model_request_budget,
)
from keystone_agents.sdk import (
    build_local_run_config,
    build_sdk_agent,
    function_tool,
    run_typed_sdk_sync,
    sdk_numeric_usage_observations,
)


class _Answer(BaseModel):
    value: int


def _message(text: str) -> list[Any]:
    return [ResponseOutputMessage(
        id="synthetic-message", type="message", role="assistant", status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )]


class _Model(Model):
    def __init__(self, outputs, *, input_tokens=10, output_tokens=20, barrier=None):
        self.outputs = list(outputs)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.barrier = barrier
        self.calls = 0

    async def get_response(self, *args, **kwargs):
        self.calls += 1
        if self.barrier is not None:
            self.barrier.wait(timeout=10)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return ModelResponse(
            output=output,
            usage=Usage(requests=1, input_tokens=self.input_tokens,
                        output_tokens=self.output_tokens,
                        total_tokens=self.input_tokens + self.output_tokens),
            response_id=f"synthetic-response-{self.calls}",
        )

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class _Provider(ModelProvider):
    def __init__(self, model):
        self.model = model

    def get_model(self, model_name):
        return self.model


def _agent(*, tools=(), output_type=_Answer):
    return build_sdk_agent(
        name="usage_probe", instructions="Return the required structured value.",
        output_type=output_type, tools=list(tools), model="gpt-5.4-mini",
        enforce_tool_policy=False,
    )


def _run(model, *, tools=()):
    return run_typed_sdk_agent(
        agent=_agent(tools=tools), typed_input={"raw_request": "Synthetic usage probe."},
        output_type=_Answer, run_config=build_local_run_config(_Provider(model)),
        config=ModelConfig(provider="openai", model="gpt-5.4-mini"),
        inherit_env_session=False, tracing_disabled=True, max_turns=4,
    )


@pytest.fixture(autouse=True)
def _offline_privacy_defaults(monkeypatch):
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    monkeypatch.setattr("keystone_agents.run._sdk_structured_output_max_retries", lambda **_: 0)
    monkeypatch.setenv("KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES", "0")


def test_invalid_output_preserves_numeric_usage_without_sdk_model_data():
    private_text = "synthetic-private-output-do-not-retain"
    with pytest.raises(ModelBehaviorError) as caught:
        _run(_Model([_message(private_text)]))

    assert caught.value.run_data is None
    snapshot = sdk_numeric_usage_observations(caught.value)
    assert snapshot == {
        "requests_started": 1,
        "responses": [{"request_ordinal": 1, "requests": 1, "input_tokens": 10,
                       "output_tokens": 20, "total_tokens": 30,
                       "cached_input_tokens": 0, "cache_write_input_tokens": None,
                       "reasoning_output_tokens": 0}],
    }
    failure = sdk_run_failure_metadata(caught.value)
    assert private_text not in json.dumps(snapshot)
    assert private_text not in json.dumps(failure)
    assert failure["usage"]["requests"] == 1
    assert failure["usage"]["total_tokens"] == 30
    assert failure["usage"]["provider_request_count_confirmed"] is True
    assert failure["cost"]["estimated_usd"] > 0


def test_invalid_then_repaired_result_counts_each_response_once(monkeypatch):
    monkeypatch.setattr("keystone_agents.run._sdk_structured_output_max_retries", lambda **_: 1)
    model = _Model([_message("invalid"), _message('{"value": 7}')])

    result = _run(model)

    assert result.output.value == 7
    assert model.calls == result.usage["requests"] == 2
    assert result.usage["input_tokens"] == 20
    assert result.usage["output_tokens"] == 40
    assert result.usage["complete"] is True
    assert len(result.request_cache["model_attempt_usage"]) == 2


def test_exhausted_repair_keeps_both_failed_responses(monkeypatch):
    monkeypatch.setattr("keystone_agents.run._sdk_structured_output_max_retries", lambda **_: 1)
    with pytest.raises(ModelBehaviorError) as caught:
        _run(_Model([_message("invalid"), _message("still invalid")]))

    failure = sdk_run_failure_metadata(caught.value)
    assert failure["attempt_count"] == 2
    assert failure["usage"]["requests"] == 2
    assert failure["usage"]["total_tokens"] == 60
    assert failure["usage"]["complete"] is True
    assert failure["cost"]["estimated_usd"] > 0
    assert sdk_numeric_usage_observations(caught.value)["requests_started"] == 1


def test_budget_denied_repair_keeps_prior_response_usage_complete(monkeypatch):
    monkeypatch.setattr("keystone_agents.run._sdk_structured_output_max_retries", lambda **_: 1)
    model = _Model([_message("invalid")])
    with activate_model_request_budget(1) as budget:
        with pytest.raises(ModelRequestBudgetExhausted) as caught:
            _run(model)
        assert budget.consumed == model.calls == 1

    assert sdk_numeric_usage_observations(caught.value) == {
        "requests_started": 0, "responses": [],
    }
    failure = sdk_run_failure_metadata(caught.value)
    assert failure["attempt_count"] == 2
    assert failure["usage"]["requests"] == 1
    assert failure["usage"]["input_tokens"] == 10
    assert failure["usage"]["output_tokens"] == 20
    assert failure["usage"]["total_tokens"] == 30
    assert failure["usage"]["complete"] is True
    assert failure["usage"]["provider_request_count_confirmed"] is True
    assert failure["cost"]["estimated_usd"] > 0
    assert failure["cost"].get("complete") is not False
    blocked = failure["request_cache"]["model_attempt_usage"][1]["usage"]
    assert blocked["available"] is True
    assert blocked["complete"] is True
    assert blocked["requests"] == blocked["total_tokens"] == 0
    assert "unknown" not in failure["usage"]["note"]


@pytest.mark.parametrize("first_response", [False, True])
def test_missing_response_stays_unknown_or_explicit_lower_bound(first_response):
    @function_tool
    def read_fixture() -> str:
        """Read synthetic local fixture data."""
        return "synthetic context"

    outputs = []
    if first_response:
        outputs.append([ResponseFunctionToolCall(
            id="tool-1", type="function_call", call_id="call-1",
            name="read_fixture", arguments="{}",
        )])
    outputs.append(RuntimeError("synthetic connection interrupted"))
    with pytest.raises(RuntimeError) as caught:
        _run(_Model(outputs), tools=[read_fixture])

    failure = sdk_run_failure_metadata(caught.value)
    assert failure["usage"]["complete"] is False
    assert failure["usage"]["provider_request_count_confirmed"] is False
    if first_response:
        assert failure["usage"]["requests"] == 1
        assert failure["usage"]["input_tokens"] == 10
        assert failure["cost"]["estimated_usd"] > 0
        assert failure["cost"]["complete"] is False
    else:
        assert failure["usage"]["requests"] is None
        assert failure["usage"]["available"] is False
        assert failure["cost"]["available"] is False


def test_failure_after_wrapper_type_coercion_keeps_numeric_response():
    model = _Model([_message("not a typed object")])
    with pytest.raises(ValueError) as caught:
        run_typed_sdk_sync(
            _agent(output_type=None), "synthetic prompt", _Answer,
            run_config=build_local_run_config(_Provider(model)),
        )
    assert sdk_numeric_usage_observations(caught.value)["responses"][0]["total_tokens"] == 30


def test_sdk_rejection_before_end_hook_does_not_infer_usage_from_error_type():
    calls = []

    @function_tool
    def read_fixture(value: int) -> str:
        """Read one synthetic value."""
        calls.append(value)
        return str(value)

    model = _Model([[
        ResponseFunctionToolCall(id=f"tool-{value}", type="function_call", call_id="reused-call",
                                 name="read_fixture", arguments=json.dumps({"value": value}))
        for value in (1, 2)
    ]])
    with pytest.raises(ModelBehaviorError) as caught:
        _run(model, tools=[read_fixture])

    assert model.calls == 1
    assert calls == []
    assert sdk_numeric_usage_observations(caught.value) == {
        "requests_started": 1, "responses": [],
    }
    failure = sdk_run_failure_metadata(caught.value)
    assert failure["usage"]["available"] is False
    assert failure["usage"]["requests"] is None
    assert failure["usage"]["provider_request_count_confirmed"] is False


def test_nested_runner_usage_is_separate_from_parent_invocation():
    child_failure = {}

    def run_child():
        with pytest.raises(ModelBehaviorError) as caught:
            _run(_Model([_message("child invalid")], input_tokens=70, output_tokens=80))
        child_failure.update(sdk_run_failure_metadata(caught.value))

    @function_tool
    def inspect_child() -> str:
        """Run an isolated synthetic child probe."""
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(copy_context().run, run_child).result(timeout=10)
        return "child failed with bounded diagnostics"

    parent = _Model([
        [ResponseFunctionToolCall(id="parent-tool", type="function_call", call_id="parent-call",
                                  name="inspect_child", arguments="{}")],
        _message("parent invalid"),
    ])
    with activate_model_request_budget(3) as budget:
        with pytest.raises(ModelBehaviorError) as caught:
            _run(parent, tools=[inspect_child])
        assert budget.consumed == 3

    parent_failure = sdk_run_failure_metadata(caught.value)
    assert parent_failure["usage"]["requests"] == 2
    assert parent_failure["usage"]["input_tokens"] == 20
    assert child_failure["usage"]["requests"] == 1
    assert child_failure["usage"]["input_tokens"] == 70


def test_concurrent_runner_failures_do_not_share_numeric_observations():
    barrier = Barrier(2)

    def fail(tokens):
        with pytest.raises(ModelBehaviorError) as caught:
            _run(_Model([_message("invalid")], input_tokens=tokens, barrier=barrier))
        return sdk_run_failure_metadata(caught.value)["usage"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        usages = list(pool.map(fail, [10, 90]))

    assert [usage["input_tokens"] for usage in usages] == [10, 90]
    assert [usage["requests"] for usage in usages] == [1, 1]
