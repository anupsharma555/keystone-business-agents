"""Offline route acceptance for saved read-only research from Slack history."""

from __future__ import annotations

import json

import pytest
import test_lazy_slack_context_backend_matrix as lazy
from agents.models.interface import Model
from openai.types.responses import ResponseFunctionToolCall
from test_lazy_slack_context_backend_matrix import (
    ARTICLE_URL,
    ContextModel,
    Provider,
    _context_file,
)
from test_lazy_slack_context_backend_matrix import (
    ks_backend as ks_backend,
)
from test_lazy_slack_context_backend_matrix import (
    strict_offline_boundaries as strict_offline_boundaries,
)

from keystone_agents import langgraph_workflow, workflow_runner
from keystone_agents.agents import orchestrator
from keystone_agents.agents.business_research_analyst import (
    run_business_research_analyst_research_brief_sdk,
)
from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.orchestrator.preflight_context import compact_orchestrator_preflight_payload
from keystone_agents.orchestrator.routing import looks_like_send_side_effect
from keystone_agents.planning.compatibility import infer_manual_request_plan
from keystone_agents.schemas.research import ResearchBrief
from keystone_agents.schemas.work_item import WorkflowRunRequest, WorkItemRoute
from keystone_agents.sdk import build_local_run_config
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools import website_extraction_tool as extraction_tool

LIVE_ROOT_REQUEST = (
    "Create a resumable research-review task for item #3 in this channel’s "
    "November 13 afternoon RSS digest, about bright light therapy for "
    "nonseasonal depression. Find the exact article, read only that source, "
    "and have Business Research give a concise account of what was studied, "
    "the findings, and the evidence limits. Keep the task saved for follow-up. "
    "Do not search the web, use other sources, draft outreach, or post elsewhere."
)
NO_SOURCE_READ_REQUEST = (
    "Create a saved research task for item #3 in this channel's November 13 "
    "RSS digest. Identify the historical Slack post, but do not open or fetch "
    "the linked article; use only the Slack post."
)


@pytest.mark.parametrize("text", [
    LIVE_ROOT_REQUEST,
    (
        "Create a saved research task for item #3 in this channel's RSS digest. "
        "Analyze only its linked article. Do not publish or send anything."
    ),
    (
        "Save a review WorkItem for item #3 in the RSS digest, read the exact "
        "article, and do not draft outreach or post elsewhere."
    ),
    (
        "Please create an internal follow-up task about the third research "
        "digest item. Never post or send a message."
    ),
])
def test_saved_research_task_is_not_external_write_or_send(text: str) -> None:
    assert not looks_like_send_side_effect(text)
    plan = infer_manual_request_plan(text)
    assert plan.intent != "blocked_send"
    assert plan.target_agent != "clarification"


@pytest.mark.parametrize("text", [
    "Post this reply to Slack #announcements.",
    "Send this outreach email to the recipient.",
    "Create a research task for item #3 and send its message to the recipient.",
])
def test_affirmative_outbound_action_keeps_hard_send_gate(text: str) -> None:
    assert infer_manual_request_plan(text).intent == "blocked_send"


@pytest.mark.parametrize("text,allow_source_read", [
    (LIVE_ROOT_REQUEST, True),
    (NO_SOURCE_READ_REQUEST, False),
])
def test_workspace_source_read_follows_current_request_scope(
    tmp_path, monkeypatch, ks_backend, strict_offline_boundaries,
    text: str, allow_source_read: bool,
) -> None:
    """A scripted SDK choice remains bound by the current source-read constraint."""

    path = _context_file(tmp_path)
    context = json.loads(path.read_text())
    context.update(team_id="T111TEST", user_id="U111TEST")
    context["channel_context_capability"]["team_id"] = "T111TEST"
    path.write_text(json.dumps(context))
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED", "true")
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_TEAM_IDS", "T111TEST")
    state = cli._orchestrator_workflow_state_from_cli_context(
        context_file_path=str(path), request_text=text,
    )
    class WorkspaceContextModel(ContextModel):
        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            # Workspace enrollment also offers optional operator memory. This
            # scripted route chooses only the actual channel query/read tools.
            visible, _ = json.JSONDecoder().raw_decode(input[0]["content"])
            assert visible["request"] == text
            assert "query_operator_context" in {tool.name for tool in tools}
            channel_tools = [
                tool for tool in tools if tool.name != "query_operator_context"
            ]
            return await super().get_response(
                system_instructions, input, model_settings, channel_tools,
                output_schema, handoffs, tracing, **kwargs,
            )

    model = WorkspaceContextModel("task for item #3", backend_calls=ks_backend)
    preflight = orchestrator.run_orchestrator_preflight(
        text,
        workflow_state=state,
        run_config=build_local_run_config(Provider(model)),
    )
    assert len(preflight.workflow_state.get("channel_context_evidence", [])) == 1
    assert orchestrator._admitted_selected_source_url(
        preflight.route_result, text, preflight.workflow_state,
    ) == ARTICLE_URL
    assert len(ks_backend) == 2
    assert len(model.inputs) == 3
    if allow_source_read:
        assert preflight.execution_allowed, (
            preflight.block_reason, preflight.manual_request_plan.source,
        )
        assert preflight.route_result.selected_source_url == ARTICLE_URL
        assert preflight.manual_request_plan.primary_target == ARTICLE_URL
        assert preflight.manual_request_plan.requires_durable_state
    else:
        assert not preflight.execution_allowed
        assert preflight.manual_request_plan.source != "canonical:orchestrator_selected_source"


@pytest.mark.parametrize("text,durable,reads_source", [
    ("Try again with that review.", True, True),
    ("What is a preprint?", False, False),
])
def test_taskless_thread_reply_keeps_model_selected_durability(
    tmp_path, monkeypatch, ks_backend, strict_offline_boundaries,
    text: str, durable: bool, reads_source: bool,
) -> None:
    """An unsatisfied root may be resumed, while a new ask stays independent."""

    path = _context_file(tmp_path)
    context = json.loads(path.read_text())
    context.update(
        team_id="T111TEST", user_id="U111TEST",
        thread_ts="1794675500.000000",
        thread_root_request=LIVE_ROOT_REQUEST,
        request_text=text,
    )
    context["channel_context_capability"]["team_id"] = "T111TEST"
    path.write_text(json.dumps(context))
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED", "true")
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_TEAM_IDS", "T111TEST")
    state = cli._orchestrator_workflow_state_from_cli_context(
        context_file_path=str(path), request_text=text,
    )
    assert "current_work_item" not in state

    original_planning_result = lazy._planning_result

    def selected_plan(*, selected_url="", self_contained=False):
        return original_planning_result(
            selected_url=selected_url, self_contained=self_contained,
        ).model_copy(update={
            "durable_task_requested": durable,
            "linked_work_item_relation": (
                "continue_existing" if durable else "unspecified"
            ),
        })

    monkeypatch.setattr(lazy, "_planning_result", selected_plan)

    class ReplyModel(ContextModel):
        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            visible, _ = json.JSONDecoder().raw_decode(input[0]["content"])
            assert visible["request"] == text
            assert LIVE_ROOT_REQUEST in json.dumps(visible, ensure_ascii=False)
            assert "current_work_item" not in visible["workflow_state"]
            if durable and len(self.inputs) == 3:
                # The model receives one concrete repair for its unsupported
                # claim that a saved WorkItem already exists.
                assert not tools
                self.inputs.append(json.dumps(input))
                corrected = original_planning_result(
                    selected_url=ARTICLE_URL,
                ).model_copy(update={
                    "durable_task_requested": True,
                    "linked_work_item_relation": "unspecified",
                    "rationale": "No linked WorkItem exists; save a new source review.",
                })
                return lazy._response([lazy._message(corrected)])
            assert "query_operator_context" in {tool.name for tool in tools}
            channel_tools = [
                tool for tool in tools if tool.name != "query_operator_context"
            ]
            return await super().get_response(
                system_instructions, input, model_settings, channel_tools,
                output_schema, handoffs, tracing, **kwargs,
            )

    model = ReplyModel(text, self_contained=not reads_source, backend_calls=ks_backend)
    preflight = orchestrator.run_orchestrator_preflight(
        text, workflow_state=state,
        run_config=build_local_run_config(Provider(model)),
    )
    assert preflight.execution_allowed, preflight.block_reason
    assert preflight.route_result.linked_work_item_relation == "unspecified"
    if durable:
        assert len(model.inputs) == 4 and len(ks_backend) == 2
        assert preflight.manual_request_plan.requires_durable_state
        assert preflight.manual_request_plan.primary_target == ARTICLE_URL
        assert preflight.manual_request_plan.intent == "research_brief"
        assert preflight.manual_request_plan.expected_artifact_type == "research_brief"

        extracted_urls: list[str] = []

        def selected_bundle(**kwargs):
            def extract(url, **_options):
                extracted_urls.append(url)
                assert url == ARTICLE_URL
                return extraction_tool.WebsiteExtractionResult(
                    url=url, title="Synthetic article", provider="trafilatura",
                    status="success",
                    text_or_markdown=(
                        "Methods and context. " * 90
                        + "Study results: LATE_RESULT_SENTINEL supported one bounded conclusion. "
                        + "Limitations: small synthetic example. " * 35
                    ),
                    claims=["The synthetic article describes one bounded result."],
                )

            return extraction_tool.build_selected_url_source_bundle(
                **kwargs, extractor=extract, primary_provider="trafilatura",
                fallback_providers=[],
            )

        monkeypatch.setenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION", "true")
        monkeypatch.setattr(
            workflow_runner, "build_selected_url_source_bundle", selected_bundle,
        )
        research_inputs: list[str] = []
        research_errors: list[str] = []

        class ResearchModel(Model):
            async def get_response(self, system_instructions, input, model_settings, tools,
                                   output_schema, handoffs, tracing, **kwargs):
                serialized = json.dumps(input)
                research_inputs.append(serialized)
                packet = json.loads(research_typed.source_context)
                first_ref = packet["context_pack"]["source_refs"][0]
                assert text in serialized
                for requirement in ("bright light therapy", "findings", "evidence limits"):
                    assert requirement in serialized
                assert ARTICLE_URL in serialized
                assert "search_web" not in {tool.name for tool in tools}
                access = first_ref["web_source_access"]
                assert access["selected_url"] == ARTICLE_URL
                assert access["snapshot_sha256"]
                if len(research_inputs) == 1:
                    assert "LATE_RESULT_SENTINEL" not in serialized
                    assert {tool.name for tool in tools} == {"read_web_source_window"}
                    return lazy._response([ResponseFunctionToolCall(
                        type="function_call", name="read_web_source_window",
                        call_id="late-source-window", status="completed",
                        arguments=json.dumps({
                            "source_id": access["source_id"],
                            "selected_url": access["selected_url"],
                            "expected_snapshot_sha256": access["snapshot_sha256"],
                            "start_char": access["next_start_char"],
                            "max_chars": 8000,
                        }),
                    )])
                assert "LATE_RESULT_SENTINEL" in serialized
                catalog = packet["source_catalog"]
                source = catalog[0]
                source_id = source["source_id"]
                return lazy._response([lazy._message(ResearchBrief(
                    target_name="Synthetic article", target_type="other",
                    summary="The selected article contained one bounded result.",
                    facts=[{
                        "text": "The selected article reports one bounded result.",
                        "source_ids": [source_id],
                    }],
                    source_ids_used=[source_id], sources=[source],
                    limitations=["This is a synthetic offline source."],
                    decision={
                        "decision_owner": "specialist_agent",
                        "decision_stage": "research_source_selection",
                        "selected_candidate_ids": [source_id],
                        "candidate_assessments": [{
                            "candidate_id": source_id, "disposition": "selected",
                            "rationale": "The exact bounded source was supplied.",
                        }],
                        "reasoning": "The brief uses the selected source only.",
                    },
                ))])

            def stream_response(self, *args, **kwargs):
                raise AssertionError("Only an offline native SDK turn is expected.")

        research_typed = None

        def native_research(typed_input, **kwargs):
            nonlocal research_typed
            research_typed = typed_input
            try:
                return run_business_research_analyst_research_brief_sdk(
                    typed_input, **{
                        **kwargs, "run_config": build_local_run_config(Provider(ResearchModel())),
                    },
                )
            except Exception as exc:
                research_errors.append(f"{type(exc).__name__}: {exc}")
                raise

        monkeypatch.setattr(
            workflow_runner, "run_business_research_analyst_research_brief_sdk",
            native_research,
        )
        database = f"sqlite:///{tmp_path / 'taskless-retry.sqlite3'}"
        result = langgraph_workflow.advance_work_item_with_optional_langgraph(
            WorkflowRunRequest(
                request_text=text, save=True, database_url=database,
                context_file_path=str(path), live_sdk=True, live_search=False,
                sdk_session_enabled=False,
                manual_request_plan=preflight.manual_request_plan.model_dump(mode="json"),
                orchestrator_preflight=compact_orchestrator_preflight_payload(preflight),
            ),
            use_langgraph=True, require_langgraph=True,
        )
        assert result.advanced, (
            [blocker.message for blocker in result.blockers],
            research_errors, extracted_urls, len(research_inputs),
        )
        assert extracted_urls == [ARTICLE_URL]
        assert len(research_inputs) == 2
        assert result.work_item.current_route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
        assert any(ref.artifact_type == "research_brief" for ref in result.work_item.artifact_refs)
        persisted = SQLiteStore(database).get_work_item(result.work_item.id)
        assert persisted is not None
        assert len(persisted.sources) == 1
        assert persisted.sources[0].url == ARTICLE_URL
        assert persisted.sources[0].web_source_access is not None
        assert persisted.sources[0].web_source_access.snapshot_sha256
    else:
        assert len(model.inputs) == 1 and not ks_backend
        assert not preflight.manual_request_plan.requires_durable_state
        assert preflight.manual_request_plan.primary_target != ARTICLE_URL


@pytest.mark.parametrize("first_choice", ["valid", "orchestrator", "context_only"])
def test_taskless_topic_reply_keeps_durable_specialist_owner(
    tmp_path, monkeypatch, strict_offline_boundaries, first_choice: str,
) -> None:
    """Saving a public-topic review is model-owned across one safe repair."""

    root = (
        "Create and save a research task on open-source reproducibility "
        "practices for small software teams."
    )
    current = "Try again with that public topic review."
    path = _context_file(tmp_path)
    context = json.loads(path.read_text())
    context.update(
        team_id="T111TEST", user_id="U111TEST",
        thread_ts="1794675500.000000",
        thread_root_request=root, request_text=current,
    )
    context["channel_context_capability"]["team_id"] = "T111TEST"
    path.write_text(json.dumps(context))
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED", "true")
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_TEAM_IDS", "T111TEST")
    state = cli._orchestrator_workflow_state_from_cli_context(
        context_file_path=str(path), request_text=current,
    )
    assert "current_work_item" not in state

    class TopicModel(Model):
        inputs: list[str]

        def __init__(self):
            self.inputs = []

        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            visible, _ = json.JSONDecoder().raw_decode(input[0]["content"])
            assert visible["request"] == current
            assert root in json.dumps(visible)
            assert "current_work_item" not in visible["workflow_state"]
            self.inputs.append(json.dumps(input))
            if len(self.inputs) == 2:
                assert not tools, "Repair cannot acquire new provider evidence."
            elif len(self.inputs) == 1:
                assert {tool.name for tool in tools} >= {
                    "query_slack_channel_context", "read_slack_channel_context",
                }
            selected = lazy._planning_result().model_copy(update={
                "durable_task_requested": True,
                "requires_live_search": True,
                "rationale": "Save the root's public-topic review as a specialist task.",
            })
            if len(self.inputs) == 1 and first_choice == "orchestrator":
                selected = lazy._planning_result(self_contained=True).model_copy(update={
                    "durable_task_requested": True,
                    "rationale": "A direct answer cannot own a saved specialist task.",
                })
            elif len(self.inputs) == 1 and first_choice == "context_only":
                selected = selected.model_copy(update={"context_only_response": True})
            return lazy._response([lazy._message(selected)])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("Only offline native SDK turns are expected.")

    model = TopicModel()
    preflight = orchestrator.run_orchestrator_preflight(
        current, workflow_state=state,
        run_config=build_local_run_config(Provider(model)),
    )
    assert preflight.execution_allowed, preflight.block_reason
    assert len(model.inputs) == (1 if first_choice == "valid" else 2)
    assert preflight.route_result.route == "business_research_analyst"
    assert preflight.manual_request_plan.requires_durable_state
    assert preflight.manual_request_plan.requires_live_search
    assert preflight.manual_request_plan.primary_target != ARTICLE_URL


@pytest.mark.parametrize("label", ["named", "called", "titled"])
def test_named_artifact_action_noun_does_not_request_a_provider_write(label):
    from keystone_agents.planning.compatibility import _looks_like_external_write_side_effect

    assert not _looks_like_external_write_side_effect(
        f"Propose metadata for a Google Sheet score export {label} Review scores. "
        "Do not create files or sharing links."
    )
    assert _looks_like_external_write_side_effect(
        f"Read the Google Sheet and export {label} sheets to Drive. Do not send email."
    )
    assert _looks_like_external_write_side_effect(
        f"Export {label} sheets to Drive. Do not send email."
    )
