"""Offline backend wiring controls; scripted routing is not live model quality proof."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from keystone_agents import langgraph_workflow, workflow_runner
from keystone_agents.agents import orchestrator
from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.execution_request import build_execution_request
from keystone_agents.orchestrator.preflight_context import compact_orchestrator_preflight_payload
from keystone_agents.schemas.orchestrator import OrchestratorResult
from keystone_agents.schemas.work_item import WorkflowRunRequest
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools import website_extraction_tool as extraction_tool


@pytest.fixture(autouse=True)
def deny_unstubbed_boundaries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Fail even when workflow exception handling would swallow an attempted call."""
    import httpx
    import requests
    from agents import OpenAIProvider

    from keystone_agents import sdk

    denied: list[str] = []

    def deny(*args, **kwargs):
        denied.append("Unstubbed model/provider/network boundary")
        raise AssertionError(denied[-1])

    async def deny_async(*args, **kwargs):
        deny()

    # Native SDK scripted ModelProvider instances remain available. Production
    # provider resolution, HTTP and all sockets (including local services) fail.
    monkeypatch.setattr(OpenAIProvider, "get_model", deny)
    monkeypatch.setattr(sdk, "build_live_run_config", deny)
    monkeypatch.setattr(workflow_runner, "run_chief_of_staff_sdk", deny)
    monkeypatch.setattr(httpx.Client, "send", deny)
    monkeypatch.setattr(httpx.AsyncClient, "send", deny_async)
    monkeypatch.setattr(requests.Session, "send", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setenv("KEYSTONE_RUNTIME_STATE_DIR", str(tmp_path / "runtime-state"))
    yield
    assert not denied, "An unexpected external boundary was attempted, even if caught."


@pytest.fixture
def research_sdk_control(monkeypatch: pytest.MonkeyPatch):
    """Use the real Research SDK with a scripted native model and real schemas."""
    from agents.models.interface import Model, ModelProvider, ModelResponse
    from agents.usage import Usage
    from openai.types.responses import ResponseOutputMessage, ResponseOutputText

    from keystone_agents import response_synthesis
    from keystone_agents.schemas.research import ResearchBrief
    from keystone_agents.sdk import build_local_run_config

    actual = workflow_runner.run_business_research_analyst_research_brief_sdk
    actual_final = response_synthesis.run_typed_sdk_agent

    class ResearchCalls(list):
        final_calls: list

    calls = ResearchCalls()
    calls.final_calls = []

    class ScriptedModel(Model):
        def __init__(self, typed_input):
            self.typed_input = typed_input

        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            packet = json.loads(self.typed_input.source_context)
            sources = packet["source_catalog"]
            accesses = []

            def collect_accesses(value):
                if isinstance(value, dict):
                    if isinstance(value.get("web_source_access"), dict):
                        accesses.append(value["web_source_access"])
                    for child in value.values():
                        collect_accesses(child)
                elif isinstance(value, list):
                    for child in value:
                        collect_accesses(child)

            collect_accesses(packet)
            serialized = json.dumps(input)
            assert not handoffs
            assert {tool.name for tool in tools} <= {"read_web_source_window"}
            assert self.typed_input.research_goal in serialized
            assert all(source["source_id"] in serialized for source in sources)
            # The entire short synthetic page is already present. Further
            # snapshot windows should not be required solely to satisfy a tool count.
            assert accesses and all(access["content_complete"] for access in accesses)
            assert {source["url"] for source in sources} == {
                "https://example.org/articles/local-model-review",
            }
            assert "A local drafting model is evaluated by a separate local reviewer." in serialized
            calls.append({"typed_input": self.typed_input, "model_input": serialized})
            ids = [source["source_id"] for source in sources]
            output = ResearchBrief(
                target_name=self.typed_input.target_name, target_type=self.typed_input.target_type,
                summary="Scripted source summary: a local drafting model has a separate reviewer.",
                facts=[{"text": "The supplied article describes separate local review.",
                        "source_ids": ids}],
                limitations=["Fixture SDK wiring proof only; no live model-quality assessment."],
                source_ids_used=ids, sources=sources,
                decision={
                    "decision_owner": "specialist_agent",
                    "decision_stage": "research_source_selection",
                    "selected_candidate_ids": ids, "needs_more_context": False,
                    "reasoning": "The complete bounded article is supplied.",
                    "candidate_assessments": [{
                        "candidate_id": source["source_id"], "disposition": "selected",
                        "rationale": "The exact selected article was read.",
                    } for source in sources],
                },
            )
            return ModelResponse(
                output=[ResponseOutputMessage(
                    id="synthetic-research-output", type="message", role="assistant",
                    status="completed", content=[ResponseOutputText(
                        type="output_text", text=output.model_dump_json(), annotations=[],
                    )],
                )], usage=Usage(requests=1, input_tokens=30, output_tokens=20),
                response_id="synthetic-research-response",
            )

        def stream_response(self, *args, **kwargs):
            raise AssertionError("This fixture uses one non-streaming scripted model turn.")

    class Provider(ModelProvider):
        def __init__(self, model):
            self.model = model

        def get_model(self, model_name):
            return self.model

    def invoke(typed_input, **kwargs):
        assert kwargs["attach_tools"] is False
        assert kwargs["provider_retrieval_required"] is False
        return actual(typed_input, **{
            **kwargs, "run_config": build_local_run_config(Provider(ScriptedModel(typed_input))),
        })

    class BlockerModel(Model):
        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            assert not tools and not handoffs
            assert "selected_url_extraction_empty" in json.dumps(input)
            output = response_synthesis.UserFacingResponseSynthesis(
                answer=("The selected article could not be read, "
                        "so its contents cannot be summarized."),
                caveats=["The URL returned no source-backed claims; no broad search was used."],
            )
            return ModelResponse(
                output=[ResponseOutputMessage(
                    id="synthetic-blocker-output", type="message", role="assistant",
                    status="completed", content=[ResponseOutputText(
                        type="output_text", text=output.model_dump_json(), annotations=[],
                    )],
                )], usage=Usage(requests=1, input_tokens=30, output_tokens=20),
                response_id="synthetic-blocker-response",
            )

        def stream_response(self, *args, **kwargs):
            raise AssertionError("This fixture uses one non-streaming scripted model turn.")

    def final_invoke(**kwargs):
        typed_input = kwargs["typed_input"]
        assert not typed_input.advanced
        assert {blocker["code"] for blocker in typed_input.blockers} == {
            "selected_url_extraction_empty",
        }
        calls.final_calls.append(typed_input)
        return actual_final(**{
            **kwargs, "run_config": build_local_run_config(Provider(BlockerModel())),
        })

    monkeypatch.setattr(workflow_runner, "run_business_research_analyst_research_brief_sdk", invoke)
    monkeypatch.setattr(response_synthesis, "run_typed_sdk_agent", final_invoke)
    return calls


@pytest.mark.parametrize(
    "article_readable", [True, False], ids=["article-read", "article-unavailable"],
)
@pytest.mark.parametrize("mention", ["@KNI", "@KNI CoS"])
@pytest.mark.parametrize("use_graph", [False, True], ids=["direct-workitem", "langgraph"])
def test_channel_source_reaches_real_backend_extraction_and_sdk_synthesis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    research_sdk_control,
    mention: str,
    use_graph: bool,
    article_readable: bool,
) -> None:
    """Exercise actual backend branches with fake route/provider/model boundaries.

    The supplied seven-day active-channel digest is a fixture. Slack acquisition,
    channel authorization and model-owned source selection are not proven here.
    """

    if use_graph:
        pytest.importorskip("langgraph.graph", reason="This proof requires the native graph.")
        pytest.importorskip(
            "langgraph.checkpoint.sqlite",
            reason="Native SQLite checkpoints require the orchestration extra.",
        )

    url = "https://example.org/articles/local-model-review"
    context_path = tmp_path / "channel-context.json"
    root_request = "Use the article shared yesterday as the source for this thread."
    context_path.write_text(json.dumps({
        "schema": "keystone.slack.history_context.v1",
        "channel_id": "C-SYNTHETIC",
        "thread_ts": "1794698704.000001",
        "request_ts": "1794698704.000001",
        "thread_root_request": root_request,
        "thread_messages": [],
        "read_context": ("Slack channel history digest: active channel C-SYNTHETIC only.\n"
                         "Available window: 2026-11-07 through 2026-11-14; seven days.\n"
                         f"2026-11-13, item #3: <{url}|Local model review>\n"
                         "2026-11-12, item #3: https://example.org/articles/older-review\n")
        + "Earlier channel material. " * 180,
    }), encoding="utf-8")
    raw_request = f"{mention} provide a quick summary of article #3 posted yesterday."
    execution = build_execution_request(raw_request, slack_context_input=True)
    state = cli._orchestrator_workflow_state_from_cli_context(
        context_file_path=str(context_path), request_text=raw_request,
    )
    assert state["slack_thread_root"] == root_request
    assert url in state["slack_thread_transcript"]
    observed_prompts: list[str] = []

    def scripted_route(request, *, workflow_state, **kwargs):
        prompt = orchestrator._llm_route_prompt(request, workflow_state)
        observed_prompts.append(prompt)
        assert url in prompt
        assert request == raw_request
        assert root_request in prompt
        assert "C-SYNTHETIC" in prompt
        assert "2026-11-07 through 2026-11-14" in prompt
        return OrchestratorResult(
            route="business_research_analyst", routing_mode="llm",
            workflow=["business_research_analyst"], selected_source_url=url,
            rationale="Read the exact article identified by the bounded channel evidence.",
        )

    monkeypatch.setattr(orchestrator, "_route_ambiguous_with_llm", scripted_route)
    preflight = orchestrator.run_orchestrator_preflight(
        raw_request, requested_agent=execution.requested_agent,
        live_orchestrator=True, workflow_state=state,
    )
    assert preflight.execution_allowed
    plan = preflight.manual_request_plan
    assert plan.primary_target == url
    assert plan.expected_artifact_type == "source_summary"
    assert not plan.requires_live_search
    extraction_calls = []

    def scripted_extract(selected_url, **kwargs):
        extraction_calls.append({"selected_url": selected_url, **kwargs})
        assert selected_url == url
        assert kwargs["live"] is True
        if not article_readable:
            raise extraction_tool.WebsiteExtractionError("Synthetic article unavailable.")
        return extraction_tool.WebsiteExtractionResult(
            url=selected_url, title="Local model review", provider="trafilatura",
            status="success",
            text_or_markdown="A local drafting model is evaluated by a separate local reviewer.",
            claims=["The article describes a local review workflow."],
        )

    def fake_extract(**kwargs):
        assert kwargs["selected_urls"] == [url]
        return extraction_tool.build_selected_url_source_bundle(
            **kwargs, extractor=scripted_extract,
            primary_provider="trafilatura", fallback_providers=[],
        )

    monkeypatch.setenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION", "true")

    monkeypatch.setattr(workflow_runner, "build_selected_url_source_bundle", fake_extract)
    database_url = f"sqlite:///{tmp_path / 'matrix.db'}"
    request = WorkflowRunRequest(
        request_text=execution.current_request, save=True, database_url=database_url,
        context_file_path=str(context_path), live_sdk=True, live_search=False,
        sdk_session_enabled=False,
        manual_request_plan=plan.model_dump(mode="json"),
        orchestrator_preflight=compact_orchestrator_preflight_payload(preflight),
    )
    result = langgraph_workflow.advance_work_item_with_optional_langgraph(
        request, use_langgraph=use_graph, require_langgraph=use_graph,
    )

    assert observed_prompts
    assert len(extraction_calls) == 1
    events = SQLiteStore(database_url).list_work_item_events(result.work_item.id)
    graph_events = [event for event in events if event.event_type == "langgraph_orchestration"]
    if use_graph:
        assert graph_events
        assert graph_events[-1].metadata["runtime"] == "langgraph"
        assert "run_business_research" in graph_events[-1].metadata["node_path"]
    else:
        assert not graph_events
    if not article_readable:
        assert not result.advanced
        assert not research_sdk_control
        assert len(research_sdk_control.final_calls) == 1
        assert "could not be read" in result.human_summary
        assert not result.artifact_refs
        assert any(blocker.code == "selected_url_extraction_empty" for blocker in result.blockers)
        assert "no source-backed claims" in result.blockers[0].message
        return
    assert result.advanced, [blocker.message for blocker in result.blockers]
    assert len(research_sdk_control) == 1
    assert not research_sdk_control.final_calls
    model_input = research_sdk_control[0]["model_input"]
    assert url in model_input
    assert root_request in model_input
    assert execution.current_request in model_input
    assert "C-SYNTHETIC" in model_input
    assert "2026-11-07 through 2026-11-14" in model_input
    assert result.work_item.target.url == url
    assert any(source.url == url for source in result.work_item.sources)
    assert any(source.source_type == "slack_message" for source in result.work_item.sources)
    assert any(
        source.url == "https://example.org/articles/older-review"
        and source.source_type == "slack_thread_link"
        for source in result.work_item.sources
    )
    research_refs = result.artifact_refs[0].metadata["source_refs"]
    assert len(research_refs) == 1
    assert research_refs[0]["url"] == url
    assert research_refs[0]["web_source_access"]["content_complete"]
    read_events = [event for event in events
                   if event.event_type == "selected_url_extraction_completed"]
    assert len(read_events) == 1
    assert read_events[0].metadata["selected_urls"] == [url]
    assert not read_events[0].metadata["broad_search_performed"]
    assert not read_events[0].metadata["external_write_performed"]
