"""A natural Slack retry resumes one saved ResearchBrief and its exact source."""

from __future__ import annotations

import ast
import json

import pytest
from agents.models.interface import Model
from openai.types.responses import ResponseFunctionToolCall
from test_lazy_slack_context_backend_matrix import Provider, _message, _response
from test_lazy_slack_context_backend_matrix import (
    strict_offline_boundaries as strict_offline_boundaries,
)

from keystone_agents import response_synthesis, workflow_runner
from keystone_agents.agents.business_research_analyst import (
    run_business_research_analyst_research_brief_sdk,
)
from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.schemas.manual_request_plan import AskShapePolicy, ManualRequestPlan
from keystone_agents.schemas.orchestrator import OrchestratorPlanningResult
from keystone_agents.schemas.research import ResearchBrief
from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemApprovalGate,
    WorkItemBlocker,
    WorkItemKind,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemStatus,
    WorkItemTarget,
)
from keystone_agents.sdk import build_local_run_config
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools import website_extraction_tool as extraction

URL = "https://example.org/studies/synthetic-light-review"
OTHER_URL = "https://example.org/studies/unrelated-review"
ITEM_ID = "wi_" + "a" * 32
ROOT = (
    "Save a review of the selected article about a synthetic treatment. "
    "Read only that article, describe the study, results, and limitations. "
    "Keep the research task saved for follow-up; do not search or publish."
)
REPLY = "Try that review again using the saved source."


@pytest.mark.parametrize("variant", [
    "valid", "legacy_source_summary", "different_source", "different_artifact",
    "pending_approval", "write_requested",
])
def test_blocked_research_brief_retry_uses_same_work_item_and_cached_article(
    tmp_path, monkeypatch, capsys, strict_offline_boundaries, variant: str,
) -> None:
    db = f"sqlite:///{tmp_path / 'research.db'}"
    store = SQLiteStore(db)
    text = (
        "Synthetic review methods and context. " * 50
        + "Study results: LATE_RESULT_SENTINEL one bounded improvement. "
        + "Evidence limits: short follow-up and small studies. " * 20
    )
    preview, access = extraction.project_web_source(
        extraction.WebsiteExtractionResult(
            url=URL, title="Synthetic light review", provider="trafilatura",
            status="success", text_or_markdown=text,
        ),
        source_id="selected-synthetic-review", selected_url=URL, max_chars=450,
    )
    source = WorkItemSourceRef(
        source_id=access.source_id, url=URL, title="Synthetic light review",
        source_type="company_site", provider="selected-url",
        extraction_status="extracted", evidence_excerpt=preview,
        web_source_access=access,
    )
    saved_plan = ManualRequestPlan(
        source="canonical:orchestrator_selected_source", intent="research_brief",
        target_agent="business_research_analyst", primary_target=URL,
        target_type="url", task_objective="source_research",
        expected_artifact_type="research_brief", provider_operations=["read"],
        requires_durable_state=True, requires_live_search=False,
        side_effect_policy="draft_or_read_only", workflow=["business_research_analyst"],
        ask_shape=AskShapePolicy(
            strict_filter_mode="exact", prior_context_dependency="selected_context",
            permission_state="read_only",
        ),
    )
    if variant == "legacy_source_summary":
        saved_plan = saved_plan.model_copy(update={
            "source": "canonical:work_item_selected_source_resume",
            "intent": "company_research", "expected_artifact_type": "source_summary",
        })
    thread_ts, request_ts = "1794675500.000001", "1794675501.000001"
    item = WorkItem(
        id=ITEM_ID, kind=WorkItemKind.COMPANY_RESEARCH,
        status=WorkItemStatus.BLOCKED,
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        title="Saved synthetic article review", request_text=ROOT,
        target=WorkItemTarget(
            name="Synthetic light review", object_type="url", url=URL,
            metadata={
                "manual_request_plan": saved_plan.model_dump(mode="json"),
                "manual_expected_artifact_type": saved_plan.expected_artifact_type,
                "slack_context": {
                    "team_id": "T111TEST", "channel_id": "G111TEST",
                    "thread_ts": thread_ts,
                },
            },
        ),
        sources=[source],
        blockers=[WorkItemBlocker(
            code="business_research_analyst_agent_decision_invalid",
            message="The first research answer failed citation validation.",
        )],
        next_action=WorkItemNextAction(
            action="review_business_research_analyst_execution_failure",
            agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        ),
        approval_gates=([WorkItemApprovalGate(
            scope="external_use", required=True, state="pending",
        )] if variant == "pending_approval" else []),
    )
    store.save_work_item(item)
    context_path = tmp_path / "slack-context.json"
    context_path.write_text(json.dumps({
        "schema": "keystone.slack.history_context.v1",
        "team_id": "T111TEST", "channel_id": "G111TEST", "user_id": "U111TEST",
        "thread_ts": thread_ts, "request_ts": request_ts,
        "thread_root_request": ROOT, "slack_thread_root": ROOT,
        "request_text": REPLY, "read_context": "", "thread_messages": [],
        "channel_context_capability": {
            "schema_version": "keystone.slack.channel_context.v1",
            "team_id": "T111TEST", "channel_id": "G111TEST",
            "request_ts": request_ts, "oldest_ts": "1794070801.000001",
        },
    }))
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED", "true")
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_TEAM_IDS", "T111TEST")
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_CHANNEL_IDS", "C999OTHER")
    monkeypatch.setenv("KEYSTONE_SDK_SESSIONS", "false")
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(tmp_path / "eval.db"))
    monkeypatch.setattr(
        cli, "_run_live_ask_with_environment", cli._run_ask_with_current_environment,
    )
    monkeypatch.setattr(cli, "_record_eval_slack_run_if_requested", lambda *a, **k: None)
    calls = {"route": 0, "research": 0, "window": 0, "extract": 0}

    class RouteModel(Model):
        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            visible = json.dumps(input)
            calls["route"] += 1
            assert REPLY in visible and ROOT in visible and ITEM_ID in visible
            assert "business_research_analyst_agent_decision_invalid" in visible
            return _response([_message(OrchestratorPlanningResult(
                route="business_research_analyst", workflow=["business_research_analyst"],
                selected_source_url=(OTHER_URL if variant == "different_source" else URL),
                requires_live_search=False,
                linked_work_item_relation="continue_existing",
                durable_task_requested=variant != "legacy_source_summary",
                rationale="Retry the saved read-only research brief from its selected article.",
                decision={
                    "decision_owner": "orchestrator",
                    "decision_stage": "orchestrator_route_selection",
                    "selected_candidate_id": "business_research_analyst",
                    "candidate_assessments": [{
                        "candidate_id": "business_research_analyst",
                        "disposition": "selected",
                        "rationale": "The saved article review belongs to Research.",
                    }],
                    "reasoning": "The linked task and selected source are current.",
                },
            ))])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("No streaming model call expected")

    actual_preflight = cli.run_orchestrator_preflight
    def preflight(text, **kwargs):
        result = actual_preflight(text, **{
            **kwargs, "run_config": build_local_run_config(Provider(RouteModel())),
        })
        if variant == "different_artifact":
            result = result.model_copy(update={
                "manual_request_plan": result.manual_request_plan.model_copy(update={
                    "expected_artifact_type": "opportunity_brief",
                }),
            })
        elif variant == "write_requested":
            result = result.model_copy(update={
                "manual_request_plan": result.manual_request_plan.model_copy(update={
                    "provider_operations": ["read", "update"],
                }),
            })
        return result

    monkeypatch.setattr(cli, "run_orchestrator_preflight", preflight)

    def no_extraction(*args, **kwargs):
        calls["extract"] += 1
        raise AssertionError("A saved source must not trigger new extraction")

    monkeypatch.setattr(workflow_runner, "build_selected_url_source_bundle", no_extraction)
    actual_window = extraction.read_web_source_window_impl

    def read_window(**kwargs):
        calls["window"] += 1
        assert kwargs["selected_url"] == URL
        assert kwargs["expected_snapshot_sha256"] == access.snapshot_sha256
        return actual_window(**kwargs)

    monkeypatch.setattr(extraction, "read_web_source_window_impl", read_window)

    class ResearchModel(Model):
        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            visible = json.dumps(input)
            assert ROOT in visible and REPLY in visible and ITEM_ID in visible
            assert URL in visible and access.snapshot_sha256 in visible
            assert "search_web" not in {tool.name for tool in tools}
            outputs = [v for v in input if isinstance(v, dict)
                       and v.get("type") == "function_call_output"]
            if not outputs:
                assert {tool.name for tool in tools} == {"read_web_source_window"}
                calls["research"] += 1
                return _response([ResponseFunctionToolCall(
                    type="function_call", name="read_web_source_window",
                    call_id="saved-window", status="completed",
                    arguments=json.dumps({
                        "source_id": access.source_id, "selected_url": URL,
                        "expected_snapshot_sha256": access.snapshot_sha256,
                        "start_char": access.next_start_char, "max_chars": 8000,
                    }),
                )])
            try:
                observed = json.loads(outputs[-1]["output"])
            except json.JSONDecodeError:
                observed = ast.literal_eval(outputs[-1]["output"])
            assert observed["status"] == "success"
            assert "LATE_RESULT_SENTINEL" in observed["text"]
            return _response([_message(ResearchBrief(
                target_name="Synthetic light review", target_type="other",
                summary="The synthetic review reported one bounded improvement.",
                facts=[{
                    "text": "The reviewed treatment had one bounded improvement.",
                    "source_ids": [source.source_id],
                }],
                source_ids_used=[source.source_id],
                sources=[{
                    "source_id": source.source_id, "url": URL,
                    "title": source.title, "source_type": source.source_type,
                    "provider": source.provider, "extraction_status": "extracted",
                }],
                limitations=["Short follow-up and small studies limit certainty."],
                decision={
                    "decision_owner": "specialist_agent",
                    "decision_stage": "research_source_selection",
                    "selected_candidate_ids": [source.source_id],
                    "candidate_assessments": [{
                        "candidate_id": source.source_id, "disposition": "selected",
                        "rationale": "The scoped saved page window was read.",
                    }],
                    "reasoning": "The answer cites only the selected source.",
                },
            ))])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("No streaming model call expected")

    actual_research = run_business_research_analyst_research_brief_sdk
    monkeypatch.setattr(
        workflow_runner, "run_business_research_analyst_research_brief_sdk",
        lambda typed_input, **kwargs: actual_research(typed_input, **{
            **kwargs, "run_config": build_local_run_config(Provider(ResearchModel())),
        }),
    )

    class FinalModel(Model):
        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            assert not tools and URL in json.dumps(input)
            return _response([_message(response_synthesis.UserFacingResponseSynthesis(
                answer=f"The synthetic review reported one bounded improvement. Source: {URL}",
            ))])

    actual_synthesis = response_synthesis.run_typed_sdk_agent
    monkeypatch.setattr(response_synthesis, "run_typed_sdk_agent", lambda **kwargs: (
        actual_synthesis(**{
            **kwargs, "run_config": build_local_run_config(Provider(FinalModel())),
        })
    ))
    result = cli.main([
        "ask", "--live-sdk", "--json", "--no-sdk-session",
        "--max-openai-requests", "8", "--max-manager-steps", "1",
        "--database-url", db, "--context-file", str(context_path),
        "--linked-work-item-id", ITEM_ID, "--input", REPLY,
    ])
    output = json.loads(capsys.readouterr().out)
    if variant not in {"valid", "legacy_source_summary"}:
        assert result != 0 or output.get("status") in {"blocked", "needs_input"}, output
        assert calls["route"] == 1
        assert calls["research"] == calls["window"] == calls["extract"] == 0
        assert len(store.list_work_items(status=None)) == 1
        saved = store.get_work_item(ITEM_ID)
        assert saved is not None and saved.status == WorkItemStatus.BLOCKED
        assert not saved.artifact_refs
        return
    assert result == 0, output.get("failure", output)
    assert calls == {"route": 1, "research": 1, "window": 1, "extract": 0}
    assert len(store.list_work_items(status=None)) == 1
    saved = store.get_work_item(ITEM_ID)
    assert saved is not None and saved.status == WorkItemStatus.DONE
    assert saved.target.url == URL
    assert any(ref.artifact_type == "research_brief" for ref in saved.artifact_refs)
    assert URL in output["human_summary"]
