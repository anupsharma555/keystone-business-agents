"""Selected producer -> persisted WorkItem -> graph SDK evidence recovery, offline."""

from __future__ import annotations

import json

import pytest
from test_context_agent_fake_model_matrix import (
    FakeModel,
    FakeProvider,
    _structured_message,
    _tool_call,
)
from test_supplied_research_sdk import _item, _output

from keystone_agents import workflow_runner
from keystone_agents.langgraph_workflow import langgraph_available, run_work_item_langgraph
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.work_item import WorkflowRunRequest, WorkItemRoute
from keystone_agents.sdk import build_local_run_config
from keystone_agents.storage import SQLiteStore
from keystone_agents.tools import website_extraction_tool as web


@pytest.mark.skipif(not langgraph_available(), reason="optional LangGraph is not installed")
def test_selected_web_source_access_survives_projection_storage_and_graph(tmp_path, monkeypatch):
    url = "https://example.org/selected-research"
    marker = "NOT approved until independent prospective validation."
    text = (
        "The violet cohort contained 37 observations; the meridian control was absent. "
        + "Background context. " * 420
        + marker
    )
    monkeypatch.setenv("KEYSTONE_RUNTIME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION", "true")
    extraction = web.WebsiteExtractionResult(
        url=url,
        provider="trafilatura",
        status="success",
        text_or_markdown=text,
        title="Saffron evidence",
        claims=["Preliminary evidence only."],
    )
    bundle = web.build_selected_url_source_bundle(
        company_name="Saffron 73",
        selected_urls=[url],
        live_extraction=True,
        extractor=lambda *a, **kw: extraction,
    )
    source = bundle.source_bundle.sources[0]
    converted = workflow_runner._work_item_source_ref_from_company_source(source)
    assert len(converted.evidence_excerpt) == 1200
    assert converted.web_source_access.next_start_char == 1200
    assert marker not in converted.evidence_excerpt
    output = _output()
    original_id = output["sources"][0]["source_id"]
    output = json.loads(
        json.dumps(output)
        .replace(original_id, source.source_id)
        .replace("https://example.test/saffron-study", url)
    )
    output["summary"] = marker
    output["limitations"] = [marker]
    args = {
        "source_id": source.source_id,
        "selected_url": url,
        "expected_snapshot_sha256": source.web_source_access.snapshot_sha256,
        "start_char": converted.web_source_access.next_start_char,
        "max_chars": 8000,
    }
    model = FakeModel(
        [
            [_tool_call("read_web_source_window", args, call_id="first-window")],
            [_structured_message(output)],
            [_tool_call("read_web_source_window", args, call_id="resume-window")],
            [_structured_message(output)],
        ]
    )
    actual = workflow_runner.run_business_research_analyst_research_brief_sdk
    monkeypatch.setattr(
        workflow_runner,
        "run_business_research_analyst_research_brief_sdk",
        lambda typed, **kw: actual(
            typed, **{**kw, "run_config": build_local_run_config(FakeProvider(model))}
        ),
    )
    monkeypatch.setattr(workflow_runner, "build_selected_url_source_bundle", lambda **kw: bundle)
    monkeypatch.setattr("socket.socket.connect", lambda *a, **kw: pytest.fail("Network forbidden"))
    database_url = f"sqlite:///{tmp_path / 'workitems.sqlite'}"
    store = SQLiteStore(database_url)
    item = _item().model_copy(update={"sources": []})
    item.target.url = url
    store.save_work_item(item)
    request_text = f"Read only {url} and explain the conditions for use."
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="business_research_analyst",
        target_agent="business_research_analyst",
        workflow=["business_research_analyst"],
        intent="research_brief",
        primary_target=url,
        target_type="url",
        provider_operations=["read"],
        expected_artifact_type="source_summary",
        requires_live_search=False,
        task_objective="source_research",
        ask_shape={
            "source_type_preference": ["selected_public_url"],
            "prior_context_dependency": "selected_context",
            "permission_state": "read_only",
        },
    ).model_dump(mode="json")
    result = workflow_runner.advance_work_item(
        WorkflowRunRequest(
            request_text=request_text,
            work_item_id=item.id,
            database_url=database_url,
            manual_request_plan=plan,
            live_sdk=True,
            live_search=False,
            sdk_session_enabled=False,
            requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            model_request_limit=4,
        )
    )
    assert result.advanced, result.human_summary
    saved = SQLiteStore(database_url).get_work_item(item.id)
    assert (
        saved.sources[0].web_source_access.snapshot_sha256
        == source.web_source_access.snapshot_sha256
    )
    resumed = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="Reassess the saved source and preserve its conditions.",
            manual_request_plan={
                **plan,
                "target_type": "topic",
                "primary_target": "Saffron 73",
                "expected_artifact_type": "research_brief",
                "provider_operations": [],
                "ask_shape": {
                    "prior_context_dependency": "selected_context",
                    "permission_state": "read_only",
                },
            },
            work_item_id=item.id,
            database_url=database_url,
            live_sdk=True,
            live_search=False,
            sdk_session_enabled=False,
            requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            model_request_limit=4,
        ),
        require_langgraph=True,
    )
    assert resumed.result.advanced, resumed.result.human_summary
    assert len(model.calls) == 4
    for index in (0, 2):
        assert (
            marker not in json.dumps(model.calls[index]["input"]) or index == 2
        )  # prior answer may be retained
        assert "read_web_source_window" in model.calls[index]["tool_names"]
    for index in (1, 3):
        observed = json.dumps(model.calls[index]["input"])
        assert marker in observed and url in observed
    assert marker in result.human_summary and marker in resumed.result.human_summary
