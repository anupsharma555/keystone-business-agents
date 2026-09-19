"""Actual SDK retries distinguish operation replay from whole-tool disabling."""

from __future__ import annotations

import json
from contextlib import nullcontext

import pytest
from agents.exceptions import ModelBehaviorError, UserError
from agents.models.interface import Model, ModelProvider, ModelResponse
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from pydantic import BaseModel

from keystone_agents.receipts.journal import durable_provider_tool, reset_tool_receipt_journal
from keystone_agents.receipts.recovery import ProviderPartialSuccessError, ProviderRecoveryStore
from keystone_agents.run import UnsafeMutationRetryError, run_typed_sdk_agent
from keystone_agents.runtime.durable_execution import ExecutionStore, activate_execution
from keystone_agents.sdk import build_local_run_config, build_sdk_agent, function_tool


class _Answer(BaseModel):
    completed: list[str]


class _Model(Model):
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.visible_tools = []

    async def get_response(self, *args, **kwargs):
        tools = kwargs.get("tools", args[3] if len(args) > 3 else [])
        self.visible_tools.append([tool.name for tool in tools])
        return ModelResponse(output=self.outputs.pop(0), usage=Usage(requests=1),
                             response_id=f"synthetic-{len(self.visible_tools)}")

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class _Provider(ModelProvider):
    def __init__(self, model):
        self.model = model

    def get_model(self, model_name):
        return self.model


def _message(text):
    return [ResponseOutputMessage(id="synthetic-message", type="message", role="assistant",
                                  status="completed", content=[ResponseOutputText(
                                      type="output_text", text=text, annotations=[],
                                  )])]


def _call(target, suffix="", *, live=None):
    arguments = {"target": target}
    if live is not None:
        arguments["live"] = live
    return ResponseFunctionToolCall(id=f"synthetic-{target}-{suffix}", type="function_call",
                                    call_id=f"call-{target}-{suffix}", name="airtable_write_record",
                                    arguments=json.dumps(arguments))


def _receipt(target):
    return {"status": "success", "tool_name": "airtable_write_record", "provider": "airtable",
            "operation": "create_record", "record_id": f"rec_synthetic_{target}",
            "provider_write": True, "verification": {"passed": True}}


def _run(model, effects, *, unknown=False, recovery=None):
    @durable_provider_tool("airtable_write_record")
    def provider(target: str, *, live=True):
        if target not in {"A", "B"}:
            raise PermissionError("Target is outside the exact synthetic approval scope")
        if not live:
            return {"status": "dry-run"}
        effects.append(target)
        if unknown:
            raise ModelBehaviorError("Synthetic provider effect occurred without its receipt")
        return _receipt(target)

    @function_tool(failure_error_function=None)
    def airtable_write_record(target: str, live: bool = True) -> str:
        """Apply only the exact A and B operations authorized by the synthetic fixture."""
        return json.dumps(provider(target, live=live))

    agent = build_sdk_agent(name="mutation_retry_fixture", instructions="Complete A and B only.",
                            output_type=_Answer, tools=[airtable_write_record],
                            model="gpt-5.4-mini", enforce_tool_policy=False)
    return run_typed_sdk_agent(
        agent=agent, typed_input={"raw_request": "Apply exact A and B; no other targets."},
        output_type=_Answer, run_config=build_local_run_config(_Provider(model)),
        inherit_env_session=False, max_turns=4, recovery_store=recovery,
    )


@pytest.fixture(autouse=True)
def _bounded_local_repair(monkeypatch):
    reset_tool_receipt_journal()
    monkeypatch.setattr("keystone_agents.run._sdk_structured_output_max_retries", lambda **_: 1)
    monkeypatch.setenv("KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES", "0")
    yield
    reset_tool_receipt_journal()


def _execution(tmp_path):
    store = ExecutionStore(tmp_path / "execution.db")
    identity = store.begin({"request": "Synthetic scoped A and B"})["id"]
    store.update(identity, status="running", work_item_id="wi_synthetic")
    return store, identity


@pytest.mark.parametrize("durable", [False, True])
def test_schema_retry_keeps_same_tool_only_with_verified_durable_operations(tmp_path, durable):
    store, identity = _execution(tmp_path)
    effects = []
    model = _Model([
        [_call("A")], _message("Invalid JSON"), [_call("A", "replay"), _call("B")],
        _message('{"completed":["A","B"]}'),
    ])
    with activate_execution(store, identity) if durable else nullcontext():
        if durable:
            result = _run(model, effects)
            assert result.output.completed == ["A", "B"]
            assert result.request_cache["retry_safety"][0]["disabled_completed_tool_names"] == []
        else:
            with pytest.raises(ModelBehaviorError):
                _run(model, effects)
    if durable:
        assert effects == ["A", "B"]  # Exact A replay does not call the provider twice.
        assert model.visible_tools == [["airtable_write_record"]] * 4
        assert [operation["status"] for operation in store.operations(identity)] == [
            "verified", "verified",
        ]
    else:
        assert effects == ["A"]
        assert model.visible_tools[-1] == []


def test_durable_unknown_effect_still_blocks_structured_retry(tmp_path):
    store, identity = _execution(tmp_path)
    effects = []
    model = _Model([[_call("A")], [_call("B")]])
    with activate_execution(store, identity), pytest.raises(UnsafeMutationRetryError):
        _run(model, effects, unknown=True)
    assert effects == ["A"]
    assert len(model.visible_tools) == 1
    assert store.operations(identity)[0]["status"] == "intent"


def test_durable_retry_does_not_bypass_exact_provider_authorization(tmp_path):
    store, identity = _execution(tmp_path)
    effects = []
    model = _Model([[_call("A")], _message("Invalid JSON"), [_call("C")]])
    with activate_execution(store, identity), pytest.raises(UserError, match="approval scope"):
        _run(model, effects)
    assert model.visible_tools[-1] == ["airtable_write_record"]
    assert effects == ["A"]


def test_legacy_provider_recovery_remains_read_only_for_its_completed_tool(tmp_path):
    store, identity = _execution(tmp_path)
    store.before_operation(identity, "airtable_write_record", {"target": "A"})
    store.observe_operation(identity, "airtable_write_record", {"target": "A"}, _receipt("A"))
    recovery = ProviderRecoveryStore(tmp_path / "legacy.json", idempotency_key="synthetic")
    recovery.record_receipt(_receipt("A"))
    effects = []
    model = _Model([[_call("B")], [_call("B", "repair")]])
    with activate_execution(store, identity), pytest.raises(ProviderPartialSuccessError):
        _run(model, effects, recovery=recovery)
    assert effects == []
    assert all(tools == [] for tools in model.visible_tools)


def test_durable_preview_then_invalid_output_keeps_actual_scoped_write_callable(tmp_path):
    store, identity = _execution(tmp_path)
    effects = []
    model = _Model([
        [_call("A", "preview", live=False)], _message("Invalid JSON"),
        [_call("A", "actual", live=True)], _message('{"completed":["A"]}'),
    ])
    with activate_execution(store, identity):
        result = _run(model, effects)
    assert result.output.completed == ["A"]
    assert effects == ["A"]
    assert model.visible_tools == [["airtable_write_record"]] * 4
    assert [operation["status"] for operation in store.operations(identity)] == [
        "no_effect", "verified",
    ]
