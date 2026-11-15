"""Local source continuation preserves one actual model response for synthesis."""

from __future__ import annotations

import ast
import json
from types import SimpleNamespace

import pytest

from keystone_agents.agents.business_research_analyst import _with_runtime_source_layer_policy
from keystone_agents.models import ResearchSDKInput, TypedAgentRunResult
from keystone_agents.runtime.request_budget import activate_model_request_budget
from keystone_agents.tools import website_extraction_tool as web

URL = "https://example.org/selected-study"


@pytest.fixture
def partial_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("KEYSTONE_RUNTIME_STATE_DIR", str(tmp_path))
    result = web.WebsiteExtractionResult(
        url=URL, provider="trafilatura", status="success", title="Synthetic source",
        text_or_markdown="Study background and methods. " * 1600,
    )
    preview, access = web.project_web_source(result, selected_url=URL, max_chars=1200)
    return preview, access


def test_research_input_carries_remaining_capacity_without_changing_source_packet():
    original = ResearchSDKInput(
        target_name="Synthetic source", source_context=json.dumps({
            "source_catalog": [{"source_id": "source-1", "url": URL}],
            "operator_evidence": "The source remains partial.",
        }),
    )
    with activate_model_request_budget(8) as ledger:
        for _ in range(3):
            ledger.consume(stage="orchestrator:llm_start")
        prepared = _with_runtime_source_layer_policy(original)
    packet, _ = json.JSONDecoder().raw_decode(prepared.source_context)
    assert packet["source_catalog"] == [{"source_id": "source-1", "url": URL}]
    assert packet["operator_evidence"] == "The source remains partial."
    capacity_line = prepared.source_context.split("Model request capacity: ", 1)[1].splitlines()[0]
    capacity = json.loads(capacity_line)
    assert capacity["remaining_model_requests"] == 5
    assert capacity["reserved_final_response_requests"] == 1
    assert "disclose unread source coverage" in prepared.source_context
    assert "model_request_capacity" not in json.loads(original.source_context)


def test_source_window_tool_hides_before_final_request_and_restores_next_run(partial_snapshot):
    _, access = partial_snapshot
    reader = web.scoped_web_source_read_tool((access,))
    tool = reader.sdk_tool
    assert tool.params_json_schema["properties"]["max_chars"]["default"] == 8000
    assert tool.params_json_schema["properties"]["max_chars"]["maximum"] == 8000
    with activate_model_request_budget(2) as ledger:
        assert tool.is_enabled(None, None)
        ledger.consume(stage="research:window")
        # A call advertised before this model charge remains executable.
        assert tool.is_enabled(None, None)
        reader(
            source_id=access.source_id, selected_url=access.selected_url,
            expected_snapshot_sha256=access.snapshot_sha256,
            start_char=access.next_start_char, max_chars=8000,
        )
        assert not tool.is_enabled(None, None)
    with activate_model_request_budget(3):
        assert tool.is_enabled(None, None)


def test_window_result_reports_live_capacity_and_keeps_partial_offsets(partial_snapshot):
    _, access = partial_snapshot
    with activate_model_request_budget(5) as ledger:
        for _ in range(4):
            ledger.consume(stage="research:llm_start")
        result = web.read_web_source_window_impl(
            source_id=access.source_id, selected_url=access.selected_url,
            expected_snapshot_sha256=access.snapshot_sha256,
            start_char=access.next_start_char, max_chars=8000,
        )
        assert ledger.remaining == 1
    assert result["status"] == "success"
    assert result["model_request_capacity"]["remaining_model_requests"] == 1
    assert result["model_request_capacity"]["required_future_requests"] == 2
    assert result["model_request_capacity"]["capacity_sufficient"] is False
    assert "disclose unread coverage" in result["read_window_guidance"]
    assert result["web_source_access"]["start_char"] == 1200
    assert result["web_source_access"]["end_char"] == 9200
    assert result["web_source_access"]["snapshot_sha256"] == access.snapshot_sha256
    assert result["reached_end"] is False
    assert result["web_source_access"]["content_complete"] is False


def test_counterfactual_same_ledger_child_reservation_does_not_poison_later_visibility(
    partial_snapshot,
):
    _, access = partial_snapshot
    tool = web.scoped_web_source_read_tool((access,)).sdk_tool
    with activate_model_request_budget(8) as ledger:
        lease = ledger.reserve_child(stage="counterfactual_preflight")
        assert ledger.remaining == 0
        assert not tool.is_enabled(None, None)
        lease.settle(3)
        assert ledger.remaining == 5
        assert tool.is_enabled(None, None)
    receipt = tool.keystone_source_window_visibility()
    assert [item["remaining"] for item in receipt["observations"]] == [0, 5]
    assert [item["enabled"] for item in receipt["observations"]] == [False, True]
    assert "not_provider_schema" in receipt["observation_kind"]


def test_visibility_callback_receipts_are_bounded_content_free_and_request_local(partial_snapshot):
    _, access = partial_snapshot
    tool = web.scoped_web_source_read_tool((access,)).sdk_tool
    with activate_model_request_budget(5):
        for _ in range(20):
            assert tool.is_enabled(None, None)
    receipt = tool.keystone_source_window_visibility()
    assert len(receipt["observations"]) == 12
    assert receipt["evaluation_count"] == 20
    assert receipt["truncated"]
    serialized = json.dumps(receipt)
    assert URL not in serialized
    assert access.source_id not in serialized
    assert access.snapshot_sha256 not in serialized
    with activate_model_request_budget(1):
        assert not tool.is_enabled(None, None)
    assert tool.keystone_source_window_visibility()["evaluation_count"] == 1
    assert receipt["evaluation_count"] == 20


@pytest.mark.parametrize("failed", [False, True])
def test_research_wrapper_retains_visibility_on_success_and_failure(
    partial_snapshot, monkeypatch, failed,
):
    from keystone_agents.agents import business_research_analyst as research
    from keystone_agents.run import sdk_run_failure_metadata

    _, access = partial_snapshot
    tool = web.scoped_web_source_read_tool((access,)).sdk_tool
    agent = SimpleNamespace(tools=[tool])
    failure = RuntimeError("sensitive failure detail")
    failure.keystone_sdk_run_failure = {"request_cache": {}}

    def runner(**kwargs):
        assert kwargs["agent"] is agent
        assert tool.is_enabled(None, None)
        if failed:
            raise failure
        return TypedAgentRunResult(
            agent_name="business_research_analyst", output=None, raw_result=None,
        )

    monkeypatch.setattr(research, "run_typed_sdk_agent", runner)
    with activate_model_request_budget(5):
        if failed:
            with pytest.raises(RuntimeError) as caught:
                research._run_research_sdk_with_visibility(agent=agent)
            cache = sdk_run_failure_metadata(caught.value)["request_cache"]
        else:
            cache = research._run_research_sdk_with_visibility(agent=agent).request_cache
    receipt = cache["source_window_visibility"][0]
    assert receipt["tool_names"] == ["read_web_source_window"]
    assert receipt["observations"][0]["enabled"] is True
    assert "sensitive" not in json.dumps(cache)


@pytest.mark.parametrize("budget,parallel_reads,reserved_probe", [
    (1, 0, False), (2, 1, False), (2, 2, False), (8, 1, True),
])
def test_native_sdk_final_reserve_has_no_source_tool_and_returns_partial_answer(
    partial_snapshot, budget, parallel_reads, reserved_probe,
):
    from agents.models.interface import Model, ModelProvider, ModelResponse
    from agents.usage import Usage
    from openai.types.responses import (
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
    )
    from pydantic import BaseModel

    from keystone_agents.sdk import build_local_run_config, build_sdk_agent, run_typed_sdk_sync

    _, access = partial_snapshot
    tool = web.scoped_web_source_read_tool((access,))

    class Answer(BaseModel):
        summary: str
        partial: bool

    class ReadModel(Model):
        def __init__(self):
            self.tools_seen = []

        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            self.tools_seen.append([item.name for item in tools])
            if tools:
                output = [ResponseFunctionToolCall(
                    type="function_call", name="read_web_source_window",
                    call_id=f"local-window-{i}",
                    arguments=json.dumps({
                        "source_id": access.source_id, "selected_url": access.selected_url,
                        "expected_snapshot_sha256": access.snapshot_sha256,
                        "start_char": 1200 + i * 8000, "max_chars": 8000,
                    }),
                ) for i in range(parallel_reads)]
            else:
                returned = [item for item in input if isinstance(item, dict)
                            and item.get("type") == "function_call_output"]
                if returned:
                    assert len(returned) == parallel_reads
                    try:
                        result = json.loads(returned[-1]["output"])
                    except json.JSONDecodeError:
                        result = ast.literal_eval(returned[-1]["output"])
                    assert result["model_request_capacity"]["remaining_model_requests"] == 1
                    assert result["web_source_access"]["content_complete"] is False
                output = [ResponseOutputMessage(
                    id="partial-answer", type="message", role="assistant", status="completed",
                    content=[ResponseOutputText(
                        type="output_text", annotations=[],
                        text=Answer(
                            summary="Only observed excerpts were read; later text remains unread.",
                            partial=True,
                        ).model_dump_json(),
                    )],
                )]
            return ModelResponse(output=output, usage=Usage(requests=1), response_id="synthetic")

        def stream_response(self, *args, **kwargs):
            raise AssertionError("Streaming is not part of this offline control.")

    class Provider(ModelProvider):
        def __init__(self, model):
            self.model = model

        def get_model(self, name):
            return self.model

    model = ReadModel()
    agent = build_sdk_agent(
        name="business_research_analyst", instructions="Read bounded evidence then answer.",
        tools=[tool], output_type=Answer,
    )
    with activate_model_request_budget(budget) as ledger:
        if reserved_probe:
            lease = ledger.reserve_child(stage="counterfactual_preflight")
            assert not tool.sdk_tool.is_enabled(None, None)
            lease.settle(6)
            assert ledger.remaining == 2
        _, answer = run_typed_sdk_sync(
            agent, "Summarize available evidence and state unread coverage.", Answer,
            run_config=build_local_run_config(Provider(model)), max_turns=2,
        )
        assert ledger.snapshot()["consumed"] == budget
    assert model.tools_seen == ([[]] if budget == 1 else [["read_web_source_window"], []])
    assert answer.partial
    assert "unread" in answer.summary


@pytest.mark.parametrize("unit", [
    "Line with newline.\n", 'Quoted "text" and slash\\. ', "Unicode 🧪 café. ",
])
def test_window_uses_largest_prefix_within_unchanged_serialized_cap(monkeypatch, tmp_path, unit):
    monkeypatch.setenv("KEYSTONE_RUNTIME_STATE_DIR", str(tmp_path))
    text = unit * 1500 + "Final source boundary."
    result = web.WebsiteExtractionResult(
        url=URL, provider="trafilatura", status="success", text_or_markdown=text,
    )
    preview, access = web.project_web_source(result, selected_url=URL, max_chars=1200)
    recovered = preview
    while access.next_start_char is not None:
        window = web.read_web_source_window_impl(
            source_id=access.source_id, selected_url=access.selected_url,
            expected_snapshot_sha256=access.snapshot_sha256,
            start_char=access.next_start_char, max_chars=8000,
        )
        payload = window["text"]
        assert len(payload) <= 8000
        assert len(json.dumps(payload)) <= 8002
        previous_end = access.end_char
        access = web.WebSourceAccess.model_validate(window["web_source_access"])
        assert access.start_char == previous_end
        assert access.end_char > access.start_char
        if len(payload) < 8000 and not window["reached_end"]:
            assert len(json.dumps(text[access.start_char:access.end_char + 1])) > 8002
        recovered += payload
    assert recovered == text
