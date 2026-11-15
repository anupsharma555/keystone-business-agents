"""Offline lazy-context wiring; scripted model choices are not live behavior proof."""

from __future__ import annotations

import importlib
import json
import socket
import subprocess
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from agents.models.interface import Model, ModelProvider, ModelResponse
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from keystone_agents import langgraph_workflow, workflow_runner
from keystone_agents.agents import orchestrator
from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.execution_request import build_execution_request
from keystone_agents.orchestrator.preflight_context import compact_orchestrator_preflight_payload
from keystone_agents.schemas.orchestrator import OrchestratorPlanningResult
from keystone_agents.schemas.work_item import WorkflowRunRequest, WorkItemRoute
from keystone_agents.sdk import build_local_run_config
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools import website_extraction_tool as extraction_tool

ARTICLE_URL = "https://example.org/articles/local-model-review"
ROOT_REQUEST = "Use the article shared yesterday as the source for this thread."
CHANNEL = "C111TEST"
REQUEST_TIME = datetime(2026, 11, 14, 12, tzinfo=ZoneInfo("America/New_York"))
REQUEST_TS = f"{REQUEST_TIME.timestamp():.6f}"
OLDEST_TS = f"{(REQUEST_TIME - timedelta(days=7)).timestamp():.6f}"
POST_TS = f"{(REQUEST_TIME - timedelta(days=1)).timestamp():.6f}"
DAY_START = f"{REQUEST_TIME.replace(day=13, hour=0).timestamp():.6f}"
DAY_END = f"{REQUEST_TIME.replace(hour=0).timestamp():.6f}"


@pytest.fixture(autouse=True)
def strict_offline_boundaries(monkeypatch, tmp_path):
    """Record every prohibited boundary even if production code catches its error."""
    import httpx
    import requests
    from agents import OpenAIProvider

    from keystone_agents import sdk

    denied = []

    def deny(*args, **kwargs):
        denied.append("Unexpected provider, HTTP, or socket attempt")
        raise AssertionError(denied[-1])

    async def deny_async(*args, **kwargs):
        deny()

    monkeypatch.setattr(OpenAIProvider, "get_model", deny)
    monkeypatch.setattr(sdk, "build_live_run_config", deny)
    monkeypatch.setattr(workflow_runner, "run_chief_of_staff_sdk", deny)
    monkeypatch.setattr(httpx.Client, "send", deny)
    monkeypatch.setattr(httpx.AsyncClient, "send", deny_async)
    monkeypatch.setattr(requests.Session, "send", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(subprocess, "Popen", deny)
    monkeypatch.setenv("KEYSTONE_RUNTIME_STATE_DIR", str(tmp_path / "runtime-state"))
    monkeypatch.setenv("KEYSTONE_SDK_SESSION_ENABLED", "false")
    yield
    assert not denied, "An unexpected external boundary was attempted, even if caught."


def _message(output):
    return ResponseOutputMessage(
        id="synthetic-lazy-output", type="message", role="assistant", status="completed",
        content=[ResponseOutputText(
            type="output_text", text=output.model_dump_json(), annotations=[],
        )],
    )


def _response(output):
    return ModelResponse(
        output=output,
        usage=Usage(requests=1, input_tokens=30, output_tokens=20),
        response_id="synthetic-lazy-response",
    )


class Provider(ModelProvider):
    def __init__(self, model):
        self.model = model

    def get_model(self, model_name):
        return self.model


def _planning_result(*, selected_url="", self_contained=False):
    route = "orchestrator" if self_contained else "business_research_analyst"
    return OrchestratorPlanningResult(
        route=route,
        workflow=[] if self_contained else [route],
        selected_source_url=selected_url,
        rationale="The selected source identity was returned by the scoped context tools.",
        decision={
            "decision_owner": "orchestrator",
            "decision_stage": "orchestrator_route_selection",
            "selected_candidate_id": route,
            "candidate_assessments": [{
                "candidate_id": route, "disposition": "selected",
                "rationale": "This agent owns the requested response.",
            }],
            "reasoning": "The actual model-visible evidence supports this response owner.",
        },
    )


class ContextModel(Model):
    """The actual SDK executes both function calls and returns their outputs here."""
    def __init__(self, request, *, self_contained=False, skip_read=False,
                 repair_limit=False, from_ts=DAY_START, to_ts=DAY_END,
                 query="", ordinal=3, backend_calls=None):
        self.request = request
        self.self_contained = self_contained
        self.skip_read = skip_read
        self.repair_limit = repair_limit
        self.from_ts, self.to_ts, self.query = from_ts, to_ts, query
        self.ordinal = ordinal
        self.backend_calls = backend_calls
        self.inputs = []
        self.tool_outputs = []

    async def get_response(self, system_instructions, input, model_settings, tools,
                           output_schema, handoffs, tracing, **kwargs):
        serialized = json.dumps(input)
        self.inputs.append(serialized)
        assert not handoffs
        assert {tool.name for tool in tools} == {
            "query_slack_channel_context", "read_slack_channel_context",
        }, serialized[-3000:]
        assert self.request in serialized
        query_tool = next(tool for tool in tools if tool.name == "query_slack_channel_context")
        if self.repair_limit:
            limit_schema = query_tool.params_json_schema["properties"]["limit"]
            assert limit_schema["minimum"] == 1 and limit_schema["maximum"] == 10
        if len(self.inputs) == 1:
            # The source is absent before acquisition. A fixture that inserts it
            # into the initial prompt cannot demonstrate lazy source selection.
            assert ARTICLE_URL not in serialized
            assert CHANNEL in serialized
            assert REQUEST_TS in serialized
            assert OLDEST_TS in serialized
            if self.self_contained:
                return _response([_message(_planning_result(self_contained=True))])
            return _response([ResponseFunctionToolCall(
                type="function_call", name="query_slack_channel_context",
                call_id="lazy-query", status="completed", arguments=json.dumps({
                    "query": self.query, "from_ts": self.from_ts, "to_ts": self.to_ts,
                    "thread_ts": "", "limit": 20 if self.repair_limit else 8, "cursor": "",
                }),
            )])
        outputs = [item for item in input if isinstance(item, dict)
                   and item.get("type") == "function_call_output"]
        latest = json.loads(outputs[-1]["output"])
        self.tool_outputs.append(latest)
        if self.repair_limit and len(self.inputs) == 2:
            # limit=20 represents the observed class of argument-bound failures;
            # the historical failing receipt did not identify the exact field.
            assert latest["ok"] is False
            assert latest["coverage"]["errors"] == ["tool_arguments_rejected"]
            correction = latest["correction"]
            assert correction["retryable"] is True
            assert correction["acquisition_budget_consumed"] is False
            assert {error["field"] for error in correction["field_errors"]} == {"limit"}
            assert correction["field_errors"][0]["maximum"] == 10
            assert self.backend_calls == [], "Invalid calls must fail before raw Slack IO."
            return _response([ResponseFunctionToolCall(
                type="function_call", name="query_slack_channel_context",
                call_id="lazy-query-corrected", status="completed", arguments=json.dumps({
                    "query": self.query, "from_ts": self.from_ts, "to_ts": self.to_ts,
                    "thread_ts": "", "limit": 8, "cursor": "",
                }),
            )])
        assert latest["ok"] is True
        assert latest["messages"][0]["channel_id"] == CHANNEL
        assert latest["messages"][0]["role"] == "bot"
        assert latest["messages"][0]["authority"] == "historical_context_only"
        assert ARTICLE_URL in json.dumps(latest)
        assert {link["url"]: link["ordinal"]
                for link in latest["messages"][0]["source_links"]}[ARTICLE_URL] == self.ordinal
        if len(self.inputs) == 2 + int(self.repair_limit):
            assert outputs[-1]["call_id"] == (
                "lazy-query-corrected" if self.repair_limit else "lazy-query"
            )
            assert latest["messages"][0]["content_kind"] == "excerpt"
            if self.skip_read:
                return _response([_message(_planning_result(selected_url=ARTICLE_URL))])
            candidate_id = latest["messages"][0]["candidate_id"]
            return _response([ResponseFunctionToolCall(
                type="function_call", name="read_slack_channel_context",
                call_id="lazy-read", status="completed",
                arguments=json.dumps({"candidate_id": candidate_id}),
            )])
        assert len(self.inputs) == 3 + int(self.repair_limit), "Unexpected duplicated model turn"
        assert outputs[-1]["call_id"] == "lazy-read"
        assert latest["messages"][0]["content_kind"] == "message"
        return _response([_message(_planning_result(selected_url=ARTICLE_URL))])

    def stream_response(self, *args, **kwargs):
        raise AssertionError("Only non-streaming scripted model calls are expected.")


def _context_file(tmp_path):
    context = {
        "schema": "keystone.slack.history_context.v1",
        "channel_id": CHANNEL, "channel_name": "towards-data-science",
        "request_ts": REQUEST_TS, "thread_ts": REQUEST_TS,
        "thread_root_request": ROOT_REQUEST,
        "thread_messages": [], "read_context": "",
        "channel_context_capability": {
            "schema_version": "keystone.slack.channel_context.v1",
            "channel_id": CHANNEL, "channel_name": "towards-data-science",
            "request_ts": REQUEST_TS, "oldest_ts": OLDEST_TS,
        },
    }
    path = tmp_path / "lazy-context.json"
    path.write_text(json.dumps(context), encoding="utf-8")
    return path


@pytest.fixture
def ks_backend(monkeypatch):
    """Real adjacent KS adapter, with only its raw Slack API boundary replaced."""
    ks_root = Path(__file__).resolve().parents[2] / "keystone-slack"
    if not (ks_root / "kni_integrations/channel_context.py").is_file():
        pytest.skip("The adjacent KS lazy-context adapter is required for this cross-repo control.")
    monkeypatch.syspath_prepend(str(ks_root))
    adapter = importlib.import_module("kni_integrations.channel_context")
    from keystone_agents.tools import slack_channel_context_tools

    class BackendCalls(list):
        post_ts = POST_TS
        ordinal = 3

    calls = BackendCalls()

    def api_call(token, method, params):
        calls.append({"method": method, "params": dict(params)})
        assert params["channel"] == CHANNEL
        assert method in {"conversations.history", "conversations.replies"}
        return {
            "ok": True,
            "messages": [{
                "ts": calls.post_ts, "text": "Research digest", "bot_id": "B-SYNTHETIC",
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": (
                    "1. <https://example.org/articles/first|First article>\n"
                    f"{calls.ordinal}. <{ARTICLE_URL}|Local model review>"
                )}}],
            }],
            "has_more": False, "response_metadata": {"next_cursor": ""},
        }

    def backend(payload):
        assert payload["descriptor"]["channel_id"] == CHANNEL
        assert payload["descriptor"]["request_ts"] == REQUEST_TS
        return adapter.handle_request(payload, api_call=api_call, env=lambda name, default=None: {
            "SLACK_BOT_TOKEN": "synthetic-token",
            "KNI_BUSINESS_AGENTS_LAZY_CONTEXT_CHANNEL_IDS": CHANNEL,
        }.get(name, default))

    monkeypatch.setattr(slack_channel_context_tools, "_invoke_channel_context_backend", backend)
    return calls


@pytest.mark.parametrize("mention", ["@KNI", "@KNI CoS"])
def test_self_contained_preflight_does_not_query_channel_history(tmp_path, ks_backend, mention):
    raw_request = f"{mention} Explain the difference between a mean and a median."
    path = _context_file(tmp_path)
    state = cli._orchestrator_workflow_state_from_cli_context(
        context_file_path=str(path), request_text=raw_request,
    )
    model = ContextModel(raw_request, self_contained=True)
    preflight = orchestrator.run_orchestrator_preflight(
        raw_request, workflow_state=state,
        run_config=build_local_run_config(Provider(model)),
    )
    assert preflight.execution_allowed, preflight.block_reason
    assert len(model.inputs) == 1
    assert not ks_backend
    assert not model.tool_outputs


def test_query_preview_cannot_authorize_selected_source(tmp_path, ks_backend):
    raw_request = "@KNI summarize article #3 from yesterday."
    path = _context_file(tmp_path)
    state = cli._orchestrator_workflow_state_from_cli_context(
        context_file_path=str(path), request_text=raw_request,
    )
    model = ContextModel(raw_request, skip_read=True)
    preflight = orchestrator.run_orchestrator_preflight(
        raw_request, workflow_state=state,
        run_config=build_local_run_config(Provider(model)),
    )
    assert not preflight.execution_allowed
    assert preflight.block_kind == "selected_source_identity_unverified"
    assert len(ks_backend) == 1
    assert len(model.inputs) == 2


@pytest.mark.parametrize("arguments", [
    {"query": "article", "from_ts": str(int(float(OLDEST_TS)) - 1)},
    {"query": "article", "to_ts": str(int(float(REQUEST_TS)) + 1)},
    {"query": "article", "thread_ts": str(int(float(OLDEST_TS)) - 1)},
    {"from_ts": OLDEST_TS, "to_ts": REQUEST_TS},
])
def test_scope_validation_precedes_channel_acquisition(tmp_path, ks_backend, arguments):
    """Helper boundary control; this case does not claim model-selected behavior."""
    from keystone_agents.tools.slack_channel_context_tools import (
        channel_context_capability_from_state,
    )

    descriptor = json.loads(_context_file(tmp_path).read_text())["channel_context_capability"]
    capability = channel_context_capability_from_state({"channel_context_capability": descriptor})
    assert capability is not None
    result = capability.query(**arguments)
    assert result["ok"] is False
    assert result["coverage"]["errors"]
    assert not ks_backend
    assert capability.read(f"slack:C222OTHER:{POST_TS}")["ok"] is False
    assert not ks_backend


def test_partial_metadata_survives_excerpt_caps(tmp_path):
    """Coverage, revision, and authority stay independently inspectable."""
    from keystone_agents.tools.slack_channel_context_tools import SlackChannelContextCapability

    descriptor = json.loads(_context_file(tmp_path).read_text())["channel_context_capability"]
    raw = {
        "ok": True, "operation": "query", "retrieved_at": "2026-11-14T17:00:01+00:00",
        "messages": [{
            "candidate_id": f"slack:{CHANNEL}:{POST_TS}", "channel_id": CHANNEL,
            "message_ts": POST_TS, "thread_ts": POST_TS, "text": "long evidence " * 1000,
            "source_links": [{"url": ARTICLE_URL, "ordinal": 3}],
            "role": "bot", "authority": "historical_context_only", "source_kind": "slack_message",
            "content_kind": "excerpt", "text_truncated": True, "revision_ts": POST_TS,
        }],
        "coverage": {
            "truncated": True, "next_cursor": "synthetic-bounded-cursor", "pages": 5,
            "scanned_count": 500, "returned_count": 1,
            "errors": [], "limitations": ["Provider page cap reached."],
        },
    }
    capability = SlackChannelContextCapability(descriptor, backend=lambda payload: raw)
    result = capability.query(query="evidence")
    assert result["ok"]
    assert len(result["messages"][0]["excerpt"]) <= 1500
    assert result["messages"][0]["text_truncated"]
    assert result["messages"][0]["revision_ts"] == POST_TS
    assert result["messages"][0]["authority"] == "historical_context_only"
    assert result["coverage"] == raw["coverage"]
    assert result["retrieved_at"] == raw["retrieved_at"]


@pytest.mark.parametrize("violation", ["other-channel", "not-allowlisted", "future", "old"])
def test_ks_scope_denial_occurs_before_raw_api(tmp_path, ks_backend, violation):
    """The acquisition owner independently rejects scope widening before IO."""
    adapter = importlib.import_module("kni_integrations.channel_context")
    descriptor = json.loads(_context_file(tmp_path).read_text())["channel_context_capability"]
    arguments = {"query": "article", "from_ts": DAY_START, "to_ts": DAY_END}
    if violation == "other-channel":
        arguments["channel_id"] = "C222OTHER"
    elif violation == "not-allowlisted":
        descriptor["channel_id"] = "C222OTHER"
    elif violation == "future":
        arguments["to_ts"] = str(int(float(REQUEST_TS)) + 1)
    else:
        arguments["from_ts"] = str(int(float(OLDEST_TS)) - 1)
    calls = []

    def api_call(*args):
        calls.append(args)
        raise AssertionError("Scope denial must precede the provider boundary.")

    result = adapter.handle_request(
        {"operation": "query", "descriptor": descriptor, "arguments": arguments},
        api_call=api_call,
        env=lambda name, default=None: {
            "KNI_BUSINESS_AGENTS_LAZY_CONTEXT_CHANNEL_IDS": CHANNEL,
            "SLACK_BOT_TOKEN": "synthetic-token",
        }.get(name, default),
    )
    assert result["ok"] is False
    assert result["coverage"]["errors"]
    assert not calls and not ks_backend


@pytest.mark.parametrize("resume", [True, False], ids=["same-workitem-resume", "new-snapshot"])
def test_retained_reads_follow_accepted_snapshot_lifecycle(tmp_path, resume):
    """Preparation may reuse an accepted snapshot only in its original scope."""
    database_url = f"sqlite:///{tmp_path / 'snapshot-lifecycle.db'}"
    store = SQLiteStore(database_url)
    work_item = workflow_runner.create_or_load_work_item(
        store=store, request_text="Summarize the selected research article.",
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
    )
    retained = [{
        "ok": True, "operation": "read",
        "snapshot": {"channel_id": CHANNEL, "request_ts": REQUEST_TS, "oldest_ts": OLDEST_TS},
        "messages": [{"candidate_id": f"slack:{CHANNEL}:{POST_TS}", "channel_id": CHANNEL,
                      "message_ts": POST_TS, "thread_ts": POST_TS,
                      "excerpt": "Accepted earlier context.",
                      "source_links": [{"url": ARTICLE_URL, "ordinal": 3}]}],
        "coverage": {"truncated": False, "errors": []},
    }]
    work_item.target.metadata["channel_context_evidence"] = retained
    work_item.target.metadata["channel_context_usage"] = {"query_calls": 1, "read_calls": 1}
    store.save_work_item(work_item)
    path, preflight = "", None
    if not resume:
        path = str(_context_file(tmp_path))
        payload = json.loads(Path(path).read_text())
        payload["channel_id"] = "C222OTHER"
        payload["channel_context_capability"]["channel_id"] = "C222OTHER"
        Path(path).write_text(json.dumps(payload), encoding="utf-8")
        preflight = {"workflow_state": {
            "channel_context_capability": payload["channel_context_capability"],
            "channel_context_evidence": [],
        }}
    request = WorkflowRunRequest(
        request_text="continue" if resume else "Explain the median in the current discussion.",
        work_item_id=work_item.id, requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        save=True, database_url=database_url, context_file_path=path,
        orchestrator_preflight=preflight, sdk_session_enabled=False,
    )
    prepared = workflow_runner.prepare_work_item_step(request)
    context = workflow_runner._specialist_orchestrator_context_payload(request, prepared.work_item)
    if resume:
        assert context["channel_context_evidence"] == retained
    else:
        assert "channel_context_evidence" not in context
        assert "channel_context_evidence" not in prepared.work_item.target.metadata


@pytest.mark.parametrize("mention", ["@KNI", "@KNI CoS"])
@pytest.mark.parametrize("use_graph", [False, True], ids=["direct-workitem", "langgraph"])
@pytest.mark.parametrize("context_case", ["yesterday", "two-days-repair", "seventh-day-term"])
def test_lazy_source_selection_reaches_real_workitem_backend(
    tmp_path, monkeypatch, ks_backend, mention, use_graph, context_case,
):
    """Acquisition -> same-model read -> actual engine -> source-backed synthesis."""
    from keystone_agents.schemas.research import ResearchBrief

    model_arguments = {"backend_calls": ks_backend}
    expected_start, expected_end = DAY_START, DAY_END
    post_ts, repair_limit = POST_TS, context_case == "two-days-repair"
    if repair_limit:
        posted = REQUEST_TIME - timedelta(days=2)
        start = posted.replace(hour=0)
        end = start + timedelta(days=1)
        post_ts = f"{posted.timestamp():.6f}"
        expected_start, expected_end = str(start.timestamp()), str(end.timestamp())
        model_arguments.update(from_ts=start.isoformat(), to_ts=end.isoformat(),
                               ordinal=2, repair_limit=True)
        raw_request = f"{mention} provide a quick summary of article #2 posted two days ago."
    elif context_case == "seventh-day-term":
        post_ts = f"{(REQUEST_TIME - timedelta(days=7) + timedelta(seconds=1)).timestamp():.6f}"
        expected_start, expected_end = OLDEST_TS, REQUEST_TS
        model_arguments.update(from_ts=OLDEST_TS, to_ts=REQUEST_TS, query="research", ordinal=2)
        raw_request = f"{mention} summarize article #2 from the research digest seven days ago."
    else:
        raw_request = f"{mention} provide a quick summary of article #3 posted yesterday."
    ks_backend.post_ts = post_ts
    ks_backend.ordinal = model_arguments.get("ordinal", 3)
    path = _context_file(tmp_path)
    state = cli._orchestrator_workflow_state_from_cli_context(
        context_file_path=str(path), request_text=raw_request,
    )
    execution = build_execution_request(raw_request, slack_context_input=True)
    model = ContextModel(raw_request, **model_arguments)
    preflight = orchestrator.run_orchestrator_preflight(
        raw_request, requested_agent=execution.requested_agent,
        workflow_state=state, run_config=build_local_run_config(Provider(model)),
    )
    assert preflight.execution_allowed, preflight.block_reason
    assert len(model.inputs) == 3 + int(repair_limit)
    assert len(model.inputs) <= 5
    assert len(model.tool_outputs) == 2 + int(repair_limit)
    assert len(ks_backend) == 2
    assert Decimal(ks_backend[0]["params"]["oldest"]) == Decimal(expected_start)
    assert Decimal(ks_backend[0]["params"]["latest"]) == Decimal(expected_end)
    assert preflight.manual_request_plan.primary_target == ARTICLE_URL
    assert preflight.manual_request_plan.expected_artifact_type == "source_summary"
    assert not preflight.manual_request_plan.requires_live_search
    extraction_calls, research_inputs, research_errors = [], [], []

    class ResearchModel(Model):
        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            serialized = json.dumps(input)
            research_inputs.append(serialized)
            assert ARTICLE_URL in serialized
            assert ROOT_REQUEST in serialized
            assert raw_request in serialized
            assert CHANNEL in serialized
            assert REQUEST_TS in serialized
            assert post_ts in serialized
            assert "channel_context_evidence" in serialized
            assert "A local drafting model is evaluated by a separate local reviewer." in serialized
            sources = research_packet["source_catalog"]
            ids = [source["source_id"] for source in sources]
            output = ResearchBrief(
                target_name=research_typed.target_name, target_type=research_typed.target_type,
                summary="A local drafting model has a separate reviewer.",
                facts=[{"text": "The supplied article describes separate local review.",
                        "source_ids": ids}], source_ids_used=ids, sources=sources,
                limitations=["Synthetic SDK wiring evidence; live behavior is untested."],
                decision={
                    "decision_owner": "specialist_agent",
                    "decision_stage": "research_source_selection",
                    "selected_candidate_ids": ids, "needs_more_context": False,
                    "reasoning": "The exact article's extracted content was supplied.",
                    "candidate_assessments": [{"candidate_id": identity,
                        "disposition": "selected", "rationale": "The article was read."}
                        for identity in ids],
                },
            )
            return _response([_message(output)])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("Only one non-streaming research turn is expected.")

    actual_research = workflow_runner.run_business_research_analyst_research_brief_sdk
    research_typed, research_packet = None, None

    def research(typed_input, **kwargs):
        nonlocal research_typed, research_packet
        research_typed = typed_input
        research_packet = json.loads(typed_input.source_context)
        try:
            return actual_research(typed_input, **{
                **kwargs, "run_config": build_local_run_config(Provider(ResearchModel())),
            })
        except Exception as exc:
            research_errors.append(repr(exc))
            raise

    def extract(url, **kwargs):
        extraction_calls.append(url)
        assert url == ARTICLE_URL
        return extraction_tool.WebsiteExtractionResult(
            url=url, title="Local model review", provider="trafilatura", status="success",
            text_or_markdown="A local drafting model is evaluated by a separate local reviewer.",
            claims=["The article describes a local review workflow."],
        )

    def bundle(**kwargs):
        return extraction_tool.build_selected_url_source_bundle(
            **kwargs, extractor=extract, primary_provider="trafilatura", fallback_providers=[],
        )

    monkeypatch.setenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION", "true")
    monkeypatch.setattr(workflow_runner, "build_selected_url_source_bundle", bundle)
    monkeypatch.setattr(
        workflow_runner, "run_business_research_analyst_research_brief_sdk", research,
    )
    database_url = f"sqlite:///{tmp_path / 'lazy-matrix.db'}"
    request = WorkflowRunRequest(
        request_text=execution.current_request, save=True, database_url=database_url,
        context_file_path=str(path), live_sdk=True, live_search=False,
        sdk_session_enabled=False,
        manual_request_plan=preflight.manual_request_plan.model_dump(mode="json"),
        orchestrator_preflight=compact_orchestrator_preflight_payload(preflight),
    )
    result = langgraph_workflow.advance_work_item_with_optional_langgraph(
        request, use_langgraph=use_graph, require_langgraph=use_graph,
    )
    assert result.advanced, [research_errors, [blocker.message for blocker in result.blockers]]
    assert extraction_calls == [ARTICLE_URL]
    assert len(research_inputs) == 1
    assert len(ks_backend) == 2, "The engine/handoff must reuse retained channel evidence."
    events = SQLiteStore(database_url).list_work_item_events(result.work_item.id)
    graph_events = [event for event in events if event.event_type == "langgraph_orchestration"]
    assert bool(graph_events) is use_graph
    if use_graph:
        assert graph_events[-1].metadata["runtime"] == "langgraph"
        assert "run_business_research" in graph_events[-1].metadata["node_path"]
    assert result.work_item.target.url == ARTICLE_URL
    retained = result.work_item.target.metadata["channel_context_evidence"]
    assert len(retained) == 1 and retained[0]["operation"] == "read"
    assert retained[0]["messages"][0]["candidate_id"] == (
        model.tool_outputs[-1]["messages"][0]["candidate_id"]
    )
    assert retained[0]["snapshot"]["request_ts"] == REQUEST_TS
    assert retained[0]["snapshot"]["oldest_ts"] == OLDEST_TS
    assert retained[0]["messages"][0]["message_ts"] == post_ts
    assert ARTICLE_URL in json.dumps(retained)
    usage = result.work_item.target.metadata["channel_context_usage"]
    assert usage["query_calls"] == 1 and usage["read_calls"] == 1
    assert usage["adapter_calls"] == 2
    assert usage["slack_api_calls"] == 2 and usage["slack_api_calls_observed"] == 2
    assert usage["slack_api_calls_complete"] is True
    if repair_limit:
        assert usage["argument_rejections"] == 1
    persisted = SQLiteStore(database_url).get_work_item(result.work_item.id)
    assert persisted is not None
    assert persisted.target.metadata["channel_context_evidence"] == retained
    assert result.artifact_refs
