"""Exact-source admission excludes unrelated and stale local web snapshots."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from keystone_agents.agents.business_research_analyst import (
    build_business_research_analyst_research_brief_agent,
)
from keystone_agents.sdk import ToolGuardrailViolation
from keystone_agents.tools import website_extraction_tool as web

SELECTED = "https://example.org/selected-report"
OTHER = "https://example.org/other-report"


def _result(text, url=SELECTED):
    return web.WebsiteExtractionResult(
        url=url,
        provider="trafilatura",
        status="success",
        title="Synthetic report",
        text_or_markdown=text,
        claims=["Synthetic report describes preliminary research."],
    )


def _arguments(access):
    return {
        "source_id": access.source_id,
        "selected_url": access.selected_url,
        "expected_snapshot_sha256": access.snapshot_sha256,
        "start_char": 0,
    }


def _plan(target=SELECTED):
    return {
        "source": "test",
        "target_agent": "business_research_analyst",
        "requested_agent": "business_research_analyst",
        "intent": "company_research",
        "primary_target": target,
        "target_type": "url",
        "task_objective": "source_research",
        "provider_operations": ["read"],
        "expected_artifact_type": "source_summary",
        "ask_shape": {"permission_state": "read_only", "strict_filter_mode": "exact"},
        "requires_live_search": False,
    }


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("KEYSTONE_RUNTIME_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION", "true")


@pytest.mark.parametrize("replay_extraction", [False, True])
def test_exact_source_sdk_denies_known_old_handles_before_returning_text(
    monkeypatch,
    replay_extraction,
):
    from agents import Runner
    from agents.models.interface import Model, ModelProvider, ModelResponse
    from agents.usage import Usage
    from openai.types.responses import (
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
    )

    from keystone_agents.receipts.journal import instrument_agent_tools
    from keystone_agents.runtime import durable_execution
    from keystone_agents.sdk import build_local_run_config

    forbidden_markers = ["UNRELATED_PRIVATE_TEST_MARKER", "STALE_TEST_MARKER"]
    _, other = web.project_web_source(_result(forbidden_markers[0], OTHER), selected_url=OTHER)
    _, stale = web.project_web_source(_result(forbidden_markers[1]), selected_url=SELECTED)
    fresh_marker = "The current outcome was NOT confirmed; independent review remains required."
    fresh_text = "Research background. " * 250 + fresh_marker
    extracted_urls = []

    def transport(url, **kwargs):
        extracted_urls.append(url)
        return _result(fresh_text, url)

    original_builder = web.build_selected_url_source_bundle
    cached_bundle = original_builder(
        company_name="Synthetic report",
        selected_urls=[SELECTED],
        live_extraction=True,
        extractor=lambda *a, **k: _result(fresh_text),
    ).model_dump(mode="json")
    monkeypatch.setattr(
        web,
        "build_selected_url_source_bundle",
        lambda **kw: original_builder(
            **kw,
            extractor=transport,
        ),
    )
    reads = []
    original_read = web.read_web_source_window_impl

    def observed_read(**kwargs):
        reads.append(kwargs)
        return original_read(**kwargs)

    monkeypatch.setattr(web, "read_web_source_window_impl", observed_read)
    attempted = [
        ("read_web_source_window", _arguments(other)),
        ("read_web_source_window", _arguments(stale)),
        (
            "extract_selected_urls_to_source_bundle",
            {
                "company_name": "Synthetic report",
                "selected_urls": [OTHER],
                "live_extraction": True,
            },
        ),
        (
            "extract_selected_urls_to_source_bundle",
            {
                "company_name": "Synthetic report",
                "selected_urls": [SELECTED],
                "live_extraction": True,
            },
        ),
    ]

    class ModelStub(Model):
        def __init__(self):
            self.inputs = []

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
            self.inputs.append(input)
            assert {tool.name for tool in tools} == {
                "extract_selected_urls_to_source_bundle",
                "read_web_source_window",
            }
            assert all(marker not in json.dumps(input) for marker in forbidden_markers)
            turn = len(self.inputs) - 1
            if turn < len(attempted):
                name, args = attempted[turn]
            elif turn == len(attempted):
                outputs = [
                    row
                    for row in input
                    if isinstance(row, dict) and row.get("type") == "function_call_output"
                ]
                raw = outputs[-1]["output"]
                try:
                    returned = json.loads(raw)
                except json.JSONDecodeError:
                    returned = ast.literal_eval(raw)
                access = web.WebSourceAccess.model_validate(
                    returned["source_bundle"]["sources"][0]["web_source_access"]
                )
                assert access.snapshot_sha256 != stale.snapshot_sha256
                name, args = "read_web_source_window", _arguments(access)
                args["start_char"] = access.next_start_char
            else:
                assert fresh_marker in json.dumps(input)
                return ModelResponse(
                    output=[
                        ResponseOutputMessage(
                            id="scope-result",
                            type="message",
                            role="assistant",
                            status="completed",
                            content=[
                                ResponseOutputText(
                                    type="output_text",
                                    annotations=[],
                                    text=json.dumps(
                                        {
                                            "target_name": "Synthetic report",
                                            "summary": "Source scope transport verified.",
                                        }
                                    ),
                                )
                            ],
                        )
                    ],
                    usage=Usage(requests=1),
                    response_id="scope-result",
                )
            return ModelResponse(
                output=[
                    ResponseFunctionToolCall(
                        type="function_call",
                        name=name,
                        call_id=f"scope-{turn}",
                        arguments=json.dumps(args),
                        status="completed",
                    )
                ],
                usage=Usage(requests=1),
                response_id=f"scope-{turn}",
            )

        def stream_response(self, *args, **kwargs):
            raise NotImplementedError

    model = ModelStub()

    class Provider(ModelProvider):
        def get_model(self, model_name):
            return model

    # The history contains valid old identities but no old content. The plan
    # remains authoritative despite the unrelated URL in that prior context.
    request = "Read only the selected report. Prior handles: " + json.dumps(
        [
            other.model_dump(mode="json"),
            stale.model_dump(mode="json"),
        ]
    )
    agent = build_business_research_analyst_research_brief_agent(
        request_text=request,
        manual_request_plan=_plan(),
    )
    replay_lookups = []

    class CachedStore:
        def operations(self, execution_id):
            return []

        def stage_result(self, execution_id, stage, inputs):
            replay_lookups.append((stage, inputs))
            if inputs["arguments"].get("selected_url") == OTHER:
                return {"tool_output": {"text": forbidden_markers[0]}}
            if (
                replay_extraction
                and stage == "tool_read:extract_selected_urls_to_source_bundle"
                and inputs["arguments"].get("selected_urls") == [SELECTED]
            ):
                return {"tool_output": cached_bundle}
            return None

        def save_stage(self, *args):
            pass

    monkeypatch.setattr(
        durable_execution,
        "current_execution",
        lambda: SimpleNamespace(
            execution_id="synthetic-execution",
            store=CachedStore(),
        ),
    )
    instrument_agent_tools(agent)
    result = Runner.run_sync(
        agent, request, run_config=build_local_run_config(Provider()), max_turns=8
    )
    assert result.final_output.target_name == "Synthetic report"
    assert extracted_urls == ([] if replay_extraction else [SELECTED])
    assert len(reads) == 1 and reads[0]["selected_url"] == SELECTED
    assert all(inputs["arguments"].get("selected_url") != OTHER for _, inputs in replay_lookups)
    deciding_input = json.dumps(model.inputs[-1])
    assert deciding_input.count("current request scope") >= 3
    assert fresh_marker in deciding_input
    if destination := os.environ.get("KBA_WEB_SCOPE_PROOF_DIR"):
        (Path(destination) / f"sdk-inputs-replay-{replay_extraction}.json").write_text(
            json.dumps(
                {
                    "actual_sdk_inputs": model.inputs,
                    "extractor_cache_replay": replay_extraction,
                    "forbidden_source_text_absent": True,
                    "fresh_qualification_present": True,
                    "proof_boundary": "Scripted SDK model, production tools, no network.",
                },
                indent=2,
            )
        )


def test_returned_handles_are_not_shared_between_exact_source_runs(monkeypatch):
    original = web.build_selected_url_source_bundle
    monkeypatch.setattr(
        web,
        "build_selected_url_source_bundle",
        lambda **kw: original(
            **kw,
            extractor=lambda *a, **k: _result("Research evidence " * 1000),
        ),
    )
    extractor, reader = web.selected_url_extraction_tools([SELECTED])
    output = extractor(
        company_name="Synthetic report", selected_urls=[SELECTED], live_extraction=True
    )
    access = web.WebSourceAccess.model_validate(
        output["source_bundle"]["sources"][0]["web_source_access"]
    )
    assert reader(**_arguments(access))["status"] == "success"
    _, next_reader = web.selected_url_extraction_tools([SELECTED])
    with pytest.raises(ToolGuardrailViolation, match="current request scope"):
        next_reader(**_arguments(access))


@pytest.mark.parametrize("target", ["", "not-a-url", "http://127.0.0.1/private"])
def test_missing_or_invalid_authoritative_target_never_widens(target):
    extractor, reader = web.selected_url_extraction_tools([target])
    _, existing = web.project_web_source(_result("Prior research evidence."), selected_url=SELECTED)
    with pytest.raises(ToolGuardrailViolation, match="current request scope"):
        extractor(company_name="Synthetic report", selected_urls=[SELECTED], live_extraction=True)
    with pytest.raises(ToolGuardrailViolation, match="current request scope"):
        reader(**_arguments(existing))


def test_default_research_and_supplied_only_bindings_are_unchanged():
    default_agent = build_business_research_analyst_research_brief_agent()
    generic_reader = next(
        tool for tool in default_agent.tools if tool.name == "read_web_source_window"
    )
    assert generic_reader.on_invoke_tool is web.read_web_source_window.sdk_tool.on_invoke_tool
    _, allowed = web.project_web_source(
        _result("Current research evidence."), selected_url=SELECTED
    )
    supplied_agent = build_business_research_analyst_research_brief_agent(
        attach_tools=False,
        web_source_accesses=(allowed,),
        manual_request_plan=_plan(),
    )
    assert [tool.name for tool in supplied_agent.tools] == ["read_web_source_window"]
    assert supplied_agent.tools[0].on_invoke_tool is not generic_reader.on_invoke_tool


@pytest.mark.parametrize(
    "current_urls,allowed", [([SELECTED], True), ([OTHER], False), ([], False)]
)
def test_nested_scope_uses_current_child_arguments_not_inherited_parent_input(
    current_urls, allowed
):
    extractor, _ = web.selected_url_extraction_tools([SELECTED])
    guard = next(
        g
        for g in extractor.sdk_tool.tool_input_guardrails
        if g.name == "keystone_selected_web_source_scope"
    )
    data = SimpleNamespace(
        context=SimpleNamespace(
            tool_name="extract_selected_urls_to_source_bundle",
            tool_input={"selected_urls": [SELECTED], "parent_context": "inherited"},
            tool_arguments=json.dumps({"selected_urls": current_urls}),
        )
    )
    result = guard.guardrail_function(data)
    assert result.output_info["selected_source_scope_verified"] is allowed
