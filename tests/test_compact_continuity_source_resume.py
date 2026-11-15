"""Public CLI resume of one cached source under a current Slack identity."""

from __future__ import annotations

import ast
import json
from pathlib import Path

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
    WorkItemArtifactRef,
    WorkItemBlocker,
    WorkItemFact,
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

CHANNEL = "C111TEST"
SOURCE_URL = "https://example.org/studies/selected-public-study"
OTHER_URL = "https://example.org/studies/other-public-study"
SAVED_ID = "wi_" + "1" * 32
OTHER_ID = "wi_" + "2" * 32
OLD_ANSWER = "UNACCEPTED_PRIOR_ANSWER_SENTINEL"


def _saved_exact_source_plan() -> dict:
    return ManualRequestPlan(
        source="canonical:work_item_selected_source_resume",
        intent="company_research", target_agent="business_research_analyst",
        primary_target=SOURCE_URL, target_type="url", task_objective="source_research",
        expected_artifact_type="source_summary", provider_operations=["read"],
        requires_durable_state=True, requires_live_search=False,
        side_effect_policy="draft_or_read_only", workflow=["business_research_analyst"],
        ask_shape=AskShapePolicy(
            strict_filter_mode="exact", prior_context_dependency="selected_context",
            permission_state="read_only",
        ),
    ).model_dump(mode="json")


_RESUME_VARIANTS = [
        "exact", "omitted_model_url", "short_reply", "short_retry", "missing_saved_plan",
        "branch_reply", "branch_selected_source",
        "new_reply", "clarify_reply", "unspecified_reply", "no_thread_link",
        "wrong_thread_link", "conflicting_current_id", "different_url", "different_work_item",
        "missing_snapshot", "ambiguous_sources", "current_url_conflict",
]


@pytest.mark.parametrize(
    "variant,enrollment",
    [(variant, "legacy") for variant in _RESUME_VARIANTS]
    + [(variant, enrollment)
       for enrollment in ("workspace_public", "workspace_private")
       for variant in ("exact", "short_reply", "branch_selected_source")],
)
def test_public_cli_graph_resume_reads_only_exact_cached_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, strict_offline_boundaries,
    variant: str, enrollment: str,
) -> None:
    channel = "G111TEST" if enrollment == "workspace_private" else CHANNEL
    database_url = f"sqlite:///{tmp_path / 'source-resume.db'}"
    store = SQLiteStore(database_url)
    article_text = (
        "A synthetic public study enrolled 120 adults in one observational cohort. "
        "It reported an association, not a causal treatment effect. " * 45
    )
    preview, access = extraction.project_web_source(
        extraction.WebsiteExtractionResult(
            url=SOURCE_URL, title="Selected public study", provider="trafilatura",
            status="success", text_or_markdown=article_text,
        ),
        source_id="selected-public-study", selected_url=SOURCE_URL, max_chars=450,
    )
    source = WorkItemSourceRef(
        source_id="selected-public-study", url=SOURCE_URL,
        title="Selected public study", provider="selected-url",
        source_type="company_site", extraction_status="extracted",
        evidence_excerpt=preview,
        web_source_access=None if variant == "missing_snapshot" else access,
    )
    sources = [source]
    if variant == "ambiguous_sources":
        sources.append(source.model_copy(update={"source_id": "second-public-study"}))
    thread_ts, request_ts = "1794675600.000001", "1794675601.000001"
    item = WorkItem(
        id=SAVED_ID, kind=WorkItemKind.COMPANY_RESEARCH,
        status=(WorkItemStatus.BLOCKED if variant == "short_retry" else WorkItemStatus.DONE),
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        title="Saved public article evidence review",
        request_text="Read the selected public article and save its evidence summary.",
        target=WorkItemTarget(
            name="Selected public study", object_type="url", url=SOURCE_URL,
            metadata={
                "slack_context": {"channel_id": channel, "thread_ts": thread_ts},
                **({"manual_request_plan": _saved_exact_source_plan()}
                   if variant in {"short_reply", "short_retry"} else {}),
                "project_context": {
                    "project_id": "synthetic-project",
                    "name": "Synthetic evidence review",
                    "objective": "Summarize the selected public study for internal review.",
                    "approved_for_agent_use": True,
                    "allowed_actions": ["read_cached_source"],
                    "blocked_actions": ["post_externally"],
                    "source_refs": [{
                        "source_id": "unrelated-project-source", "url": OTHER_URL,
                        "evidence_excerpt": "UNRELATED_PROJECT_SOURCE_SENTINEL",
                    }],
                },
            },
        ),
        sources=sources, audit_notes=[OLD_ANSWER],
        facts=[WorkItemFact(
            key="historical", value="STALE_APPROVED_FACT_SENTINEL",
            approval_state="approved_for_research",
            source_refs=[WorkItemSourceRef(
                source_id="unrelated-study", url=OTHER_URL,
                source_type="company_site", provider="selected-url",
                evidence_excerpt="UNRELATED_SOURCE_EXCERPT",
            )],
        )],
        artifact_refs=[WorkItemArtifactRef(
            artifact_type="research_brief", artifact_id="prior-research-brief",
            source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST, selected=True,
            title="Prior research brief", summary=OLD_ANSWER,
        )],
        blockers=([WorkItemBlocker(
            code="business_research_analyst_execution_failed",
            message="A prior read stopped before completion.",
        )] if variant == "short_retry" else []),
        next_action=(WorkItemNextAction(
            action="review_business_research_analyst_execution_failure",
            agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        ) if variant == "short_retry" else None),
    )
    store.save_work_item(item)
    canonical_facts = [fact.model_dump(mode="json") for fact in item.facts]
    canonical_project_sources = item.target.metadata["project_context"]["source_refs"]
    current_id = OTHER_ID if variant == "different_work_item" else SAVED_ID
    request = (
        f"Resume WorkItem {current_id} in this channel using a graph run. Re-read only "
        "its already selected cached article snapshot and finish a concise Business "
        "Research evidence summary: what was studied, findings with their exact "
        "populations, source URL, and evidence limits. Use my approved compact-context "
        "and source-reuse preference where relevant. Keep the same WorkItem saved for "
        "later review. No web search, new extraction, other sources, drafts, or posts elsewhere."
    )
    if variant == "current_url_conflict":
        request += f" Use {OTHER_URL} for this continuation."
    elif variant in {
        "short_reply", "missing_saved_plan", "branch_reply", "branch_selected_source",
        "new_reply", "clarify_reply",
        "unspecified_reply", "no_thread_link", "wrong_thread_link",
    }:
        request = "Continue this."
    elif variant == "short_retry":
        request = "Try again."
    elif variant == "conflicting_current_id":
        request = f"Continue WorkItem {OTHER_ID}."
    context = {
        "schema": "keystone.slack.history_context.v1",
        "team_id": "T111TEST", "channel_id": channel, "user_id": "U111TEST",
        "thread_ts": thread_ts, "request_ts": request_ts,
        "thread_root_request": "Read the selected public article and save its evidence summary.",
        "slack_thread_root": "Read the selected public article and save its evidence summary.",
        "request_text": request, "read_context": "", "thread_messages": [],
        "channel_context_capability": {
            "schema_version": "keystone.slack.channel_context.v1",
            "channel_id": channel, "request_ts": request_ts,
            "oldest_ts": "1794070801.000001",
        },
    }
    if enrollment != "legacy":
        context["channel_context_capability"]["team_id"] = context["team_id"]
    if variant == "wrong_thread_link":
        context["thread_ts"] = "1794675600.999999"
    context_path = tmp_path / "current-slack-context.json"
    context_path.write_text(json.dumps(context))
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED", "true")
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_CHANNEL_IDS", CHANNEL)
    if enrollment == "legacy":
        monkeypatch.delenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_TEAM_IDS", raising=False)
    else:
        monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_TEAM_IDS", "T111TEST")
    monkeypatch.setenv("KEYSTONE_SDK_SESSIONS", "false")
    monkeypatch.setenv("KEYSTONE_SDK_SESSION_DB", str(tmp_path / "sessions.sqlite"))
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(tmp_path / "eval.sqlite"))
    monkeypatch.setattr(
        cli, "_run_live_ask_with_environment", cli._run_ask_with_current_environment,
    )
    monkeypatch.setattr(cli, "_record_eval_slack_run_if_requested", lambda *a, **k: None)
    calls = {"route": [], "research": [], "windows": [], "extract": [], "errors": []}

    class RouteModel(Model):
        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            serialized = json.dumps(input)
            calls["route"].append(serialized)
            assert request in serialized
            assert OLD_ANSWER not in serialized
            if variant != "different_work_item":
                assert SAVED_ID in serialized
            if variant in {"short_reply", "short_retry"}:
                assert "linked_thread_context" in serialized
                assert "current_work_item" in serialized
            if variant == "short_retry":
                assert "business_research_analyst_execution_failed" in serialized
                assert "blocked" in serialized
            selected = (
                OTHER_URL if variant == "different_url"
                else "" if variant in {
                    "omitted_model_url", "short_reply", "short_retry", "missing_saved_plan",
                    "branch_reply",
                    "new_reply", "clarify_reply", "unspecified_reply", "no_thread_link",
                } else SOURCE_URL
            )
            relation = {
                "branch_reply": "branch_existing",
                "branch_selected_source": "branch_existing",
                "new_reply": "new_request",
                "clarify_reply": "clarify",
                "unspecified_reply": "unspecified",
            }.get(variant, "continue_existing")
            return _response([_message(OrchestratorPlanningResult(
                route="business_research_analyst", workflow=["business_research_analyst"],
                selected_source_url=selected, requires_live_search=False,
                linked_work_item_relation=relation,
                rationale="Resume the exact saved source summary from its cached snapshot.",
                decision={
                    "decision_owner": "orchestrator",
                    "decision_stage": "orchestrator_route_selection",
                    "selected_candidate_id": "business_research_analyst",
                    "candidate_assessments": [{
                        "candidate_id": "business_research_analyst", "disposition": "selected",
                        "rationale": "The existing source summary belongs to Business Research.",
                    }],
                    "reasoning": "The current WorkItem and exact source are selected.",
                },
            ))])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("No streaming route call expected.")

    actual_preflight = cli.run_orchestrator_preflight

    def preflight(input_text, **kwargs):
        result = actual_preflight(input_text, **{
            **kwargs, "run_config": build_local_run_config(Provider(RouteModel())),
        })
        if variant == "branch_selected_source":
            assert result.route_result.selected_source_url == SOURCE_URL, (
                result.route_result.selected_source_url,
                result.block_kind,
            )
        if variant in {
            "short_reply", "short_retry", "missing_saved_plan", "branch_reply",
            "branch_selected_source", "new_reply",
            "clarify_reply", "unspecified_reply", "no_thread_link",
        }:
            assert result.route_result.linked_work_item_relation == {
                "branch_reply": "branch_existing",
                "branch_selected_source": "branch_existing",
                "new_reply": "new_request",
                "clarify_reply": "clarify",
                "unspecified_reply": "unspecified",
            }.get(variant, "continue_existing"), result.route_result.model_dump(mode="json")
            assert (
                result.manual_request_plan.target_agent == "business_research_analyst"
                and result.manual_request_plan.ask_shape.prior_context_dependency
                in {"selected_context", "unspecified"}
            ), (
                result.manual_request_plan.model_dump(mode="json"),
                result.route_result.model_dump(mode="json"),
            )
        if variant not in {
            "different_work_item", "short_reply", "short_retry", "missing_saved_plan",
            "branch_reply", "branch_selected_source",
            "new_reply", "clarify_reply", "unspecified_reply", "no_thread_link",
        }:
            plan = result.manual_request_plan
            assert plan.source == "heuristic"
            assert plan.target_agent == "business_research_analyst"
            assert plan.expected_artifact_type == "source_summary"
            assert plan.intent == "research_brief"
            assert plan.target_type == "unknown"
            assert not plan.provider_operations and not plan.requires_live_search
            assert plan.requires_durable_state
            assert plan.ask_shape.prior_context_dependency == "unspecified"
            assert plan.ask_shape.permission_state == "read_only"
            assert plan.side_effect_policy == "draft_or_read_only"
        return result

    monkeypatch.setattr(cli, "run_orchestrator_preflight", preflight)

    def no_extraction(*args, **kwargs):
        calls["extract"].append(True)
        raise AssertionError("The cached source must not be extracted again.")

    monkeypatch.setattr(workflow_runner, "build_selected_url_source_bundle", no_extraction)
    actual_window = extraction.read_web_source_window_impl

    def read_window(**kwargs):
        calls["windows"].append(kwargs)
        assert kwargs["selected_url"] == SOURCE_URL
        assert kwargs["expected_snapshot_sha256"] == access.snapshot_sha256
        return actual_window(**kwargs)

    monkeypatch.setattr(extraction, "read_web_source_window_impl", read_window)

    class ResearchModel(Model):
        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            serialized = json.dumps(input)
            assert request in serialized and SAVED_ID in serialized
            assert SOURCE_URL in serialized and source.source_id in serialized
            assert access.snapshot_sha256 in serialized
            assert OLD_ANSWER not in serialized
            assert "STALE_APPROVED_FACT_SENTINEL" not in serialized
            assert OTHER_URL not in serialized
            assert "UNRELATED_SOURCE_EXCERPT" not in serialized
            assert "UNRELATED_PROJECT_SOURCE_SENTINEL" not in serialized
            assert "synthetic-project" in serialized
            assert "Summarize the selected public study for internal review." in serialized
            assert "post_externally" in serialized
            assert "search_web" not in {tool.name for tool in tools}
            outputs = [entry for entry in input if isinstance(entry, dict)
                       and entry.get("type") == "function_call_output"]
            if not outputs:
                assert {tool.name for tool in tools} == {"read_web_source_window"}
                calls["research"].append(serialized)
                return _response([ResponseFunctionToolCall(
                    type="function_call", name="read_web_source_window",
                    call_id="cached-window", status="completed",
                    arguments=json.dumps({
                        "source_id": access.source_id,
                        "selected_url": access.selected_url,
                        "expected_snapshot_sha256": access.snapshot_sha256,
                        "start_char": access.next_start_char, "max_chars": 8000,
                    }),
                )])
            try:
                observed = json.loads(outputs[-1]["output"])
            except json.JSONDecodeError:
                observed = ast.literal_eval(outputs[-1]["output"])
            assert observed["status"] == "success" and observed["reached_end"]
            source_row = {
                "source_id": source.source_id, "url": SOURCE_URL,
                "title": source.title, "source_type": source.source_type,
                "provider": source.provider, "extraction_status": "extracted",
            }
            return _response([_message(ResearchBrief(
                target_name="Selected public study", target_type="other",
                summary="The observational cohort reported an association, not causation.",
                facts=[{
                    "text": "The cohort enrolled 120 adults.",
                    "source_ids": [source.source_id],
                }],
                source_ids_used=[source.source_id], sources=[source_row],
                limitations=["One observational cohort does not establish causation."],
                decision={
                    "decision_owner": "specialist_agent",
                    "decision_stage": "research_source_selection",
                    "selected_candidate_ids": [source.source_id],
                    "candidate_assessments": [{
                        "candidate_id": source.source_id, "disposition": "selected",
                        "rationale": "The exact cached article window was read.",
                    }],
                    "reasoning": "The summary uses only the selected source.",
                },
            ))])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("No streaming research call expected.")

    def native_research(typed_input, **kwargs):
        try:
            return run_business_research_analyst_research_brief_sdk(
                typed_input, **{
                    **kwargs, "run_config": build_local_run_config(Provider(ResearchModel())),
                },
            )
        except Exception as exc:
            calls["errors"].append((type(exc).__name__, str(exc)[:300]))
            raise

    monkeypatch.setattr(
        workflow_runner, "run_business_research_analyst_research_brief_sdk", native_research,
    )

    class FinalModel(Model):
        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            assert not tools and not handoffs
            assert SOURCE_URL in json.dumps(input)
            return _response([_message(response_synthesis.UserFacingResponseSynthesis(
                answer=(
                    "The saved observational study enrolled 120 adults and reported an "
                    f"association, not causation. Source: {SOURCE_URL}"
                ),
            ))])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("No streaming final synthesis expected.")

    actual_synthesis = response_synthesis.run_typed_sdk_agent
    monkeypatch.setattr(response_synthesis, "run_typed_sdk_agent", lambda **kwargs: (
        actual_synthesis(**{
            **kwargs, "run_config": build_local_run_config(Provider(FinalModel())),
        })
    ))
    argv = [
        "ask", "--live-sdk", "--json", "--no-sdk-session",
        "--max-openai-requests", "8", "--max-manager-steps", "1",
        "--database-url", database_url, "--context-file", str(context_path),
        "--input", request,
    ]
    if variant in {
        "short_reply", "short_retry", "missing_saved_plan", "branch_reply",
        "branch_selected_source", "new_reply",
        "clarify_reply", "unspecified_reply", "wrong_thread_link",
        "conflicting_current_id",
    }:
        argv.extend(["--linked-work-item-id", SAVED_ID])
    if variant in {"different_work_item", "wrong_thread_link", "conflicting_current_id"}:
        message = (
            "does not match the authenticated Slack thread"
            if variant == "wrong_thread_link" else "Linked WorkItem not found"
        )
        with pytest.raises(SystemExit, match=message):
            cli.main(argv)
        assert not calls["route"]
        assert not calls["research"] and not calls["windows"]
        assert not calls["extract"] and not store.list_memory_items()
        assert len(store.list_work_items(status=None)) == 1
        assert [fact.model_dump(mode="json") for fact in store.get_work_item(SAVED_ID).facts] == (
            canonical_facts
        )
        assert store.get_work_item(SAVED_ID).target.metadata["project_context"]["source_refs"] == (
            canonical_project_sources
        )
        return
    exit_code = cli.main(argv)
    payload = json.loads(capsys.readouterr().out)
    if variant == "no_thread_link":
        assert not calls["windows"] and not calls["extract"]
        assert store.get_work_item(SAVED_ID) is not None
        assert [fact.model_dump(mode="json") for fact in store.get_work_item(SAVED_ID).facts] == (
            canonical_facts
        )
        assert payload.get("work_item", {}).get("id") != SAVED_ID
        return
    if variant == "missing_saved_plan":
        assert not calls["windows"] and not calls["research"] and not calls["extract"]
        assert len(store.list_work_items(status=None)) == 1
        assert store.get_work_item(SAVED_ID) is not None
        assert payload.get("status") in {"failed", "needs_input"}
        return
    if variant in {"branch_reply", "branch_selected_source", "new_reply"}:
        assert not calls["extract"]
        assert [fact.model_dump(mode="json") for fact in store.get_work_item(SAVED_ID).facts] == (
            canonical_facts
        )
        assert payload.get("work_item", {}).get("id") != SAVED_ID, payload
        assert "work_item" in payload, (
            payload.get("status"), payload.get("failure"), payload.get("block_kind"),
        )
        created = store.get_work_item(payload["work_item"]["id"])
        assert created is not None
        if variant == "branch_selected_source":
            assert source in created.sources
            assert all(ref.url != OTHER_URL for ref in created.sources)
        else:
            assert all(ref.url != SOURCE_URL for ref in created.sources)
        assert not created.facts and not created.approval_gates
        assert all(ref.artifact_id != "prior-research-brief" for ref in created.artifact_refs)
        assert OLD_ANSWER not in json.dumps(created.model_dump(mode="json"))
        parent = created.target.metadata.get("branch_parent_work_item")
        if variant in {"branch_reply", "branch_selected_source"}:
            assert parent == {
                "schema": "keystone.work_item_branch_parent.v1",
                "work_item_id": SAVED_ID,
                **({"selected_source_id": source.source_id}
                   if variant == "branch_selected_source" else {}),
            }
        else:
            assert parent is None
        return
    if variant in {"clarify_reply", "unspecified_reply"}:
        assert not calls["windows"] and not calls["research"] and not calls["extract"]
        assert len(store.list_work_items(status=None)) == 1
        assert [fact.model_dump(mode="json") for fact in store.get_work_item(SAVED_ID).facts] == (
            canonical_facts
        )
        assert payload.get("status") == "needs_input"
        assert payload.get("block_kind") == "linked_work_item_relation_unclear"
        return
    if variant != "different_work_item":
        assert calls["route"]
    assert not calls["extract"]
    assert not store.list_memory_items(), (
        variant, payload.get("work_item", {}).get("id"), payload.get("status"),
        payload.get("human_summary"), calls["errors"],
    )
    if variant in {"exact", "omitted_model_url", "short_reply", "short_retry"}:
        assert exit_code == 0, (
            payload.get("failure", payload), payload.get("work_item", {}).get("id"),
            len(calls["route"]), len(calls["research"]), calls["errors"],
        )
        assert calls["research"] and calls["windows"], (
            payload.get("status"), payload.get("block_kind"),
            payload.get("human_summary"), payload.get("blockers"), calls["errors"],
        )
        assert len(store.list_work_items(status=None)) == 1
        saved = store.get_work_item(SAVED_ID)
        assert saved is not None and saved.status == WorkItemStatus.DONE, (
            payload.get("status"), payload.get("human_summary"),
            [(blocker.code, blocker.message) for blocker in saved.blockers],
            calls["errors"],
        )
        assert [fact.model_dump(mode="json") for fact in saved.facts] == canonical_facts
        assert saved.target.metadata["project_context"]["source_refs"] == (
            canonical_project_sources
        )
        assert SOURCE_URL in payload["human_summary"]
    else:
        assert exit_code != 0 or payload.get("status") == "blocked", payload
        assert not calls["research"] and not calls["windows"]
        assert len(store.list_work_items(status=None)) == 1
        assert [fact.model_dump(mode="json") for fact in store.get_work_item(SAVED_ID).facts] == (
            canonical_facts
        )
        assert store.get_work_item(SAVED_ID).target.metadata["project_context"]["source_refs"] == (
            canonical_project_sources
        )
