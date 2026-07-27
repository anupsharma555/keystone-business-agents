from __future__ import annotations

from pathlib import Path

import pytest

import keystone_agents.langgraph_workflow as langgraph_workflow
import keystone_agents.workflow_runner as workflow_runner
from keystone_agents.langgraph_quality import (
    compare_langgraph_quality,
    langgraph_quality_markers,
    render_langgraph_quality_comparison,
)
from keystone_agents.langgraph_workflow import (
    LangGraphUnavailableError,
    advance_work_item_manager_loop_with_optional_langgraph,
    advance_work_item_with_optional_langgraph,
    build_work_item_langgraph,
    langgraph_available,
    langgraph_functionality_improvements,
    next_langgraph_node_for_result,
    run_work_item_langgraph,
    should_use_langgraph_for_work_item,
    work_item_graph_thread_id,
    work_item_langgraph_enabled,
)
from keystone_agents.manual_request import infer_manual_request_plan
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.multi_target_research import (
    MultiTargetReadiness,
    MultiTargetResearchPlan,
    MultiTargetResearchResult,
    PerTargetResearchPacket,
)
from keystone_agents.reporting import render_work_item_graph_report
from keystone_agents.runtime import RequestRuntime
from keystone_agents.schemas.announcement_feed import AnnouncementFeedEvidence, AnnouncementFeedItem
from keystone_agents.schemas.approval import ApprovalQueueStatus, ApprovalState
from keystone_agents.schemas.chief_of_staff import (
    ChiefContextHandoff,
    ChiefDurableHandoff,
    ChiefNestedSpecialistResult,
    ChiefOfStaffResult,
    ChiefOfStaffRouteRecommendation,
)
from keystone_agents.schemas.weekly_ops import WeeklyOpsAssemblyInput
from keystone_agents.schemas.work_item import (
    UserFacingSummaryAuthority,
    WorkflowRunRequest,
    WorkflowRunResult,
    WorkItem,
    WorkItemArtifactRef,
    WorkItemFact,
    WorkItemKind,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemStatus,
    WorkItemTarget,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.weekly_ops_packet import build_weekly_ops_source_bundle
from keystone_agents.work_items import (
    apply_slack_approval_to_work_item_gate,
    approve_artifact_context,
    set_next_action,
)


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'langgraph_workflow.db'}"


def test_graph_write_review_does_not_infer_research_from_graph_evidence_wording() -> None:
    stages = langgraph_workflow._graph_requested_stage_review(
        request_text=(
            "Create a marked Sheet, verify cleanup status, and return graph evidence."
        ),
        manual_intent="business_system_write",
        node_path=["run_chief_of_staff", "approval_checkpoint"],
        loop_steps=[{"route": "chief_of_staff"}],
        artifact_types=["chief_of_staff_plan"],
        blocker_codes=set(),
        checkpoint_required=True,
        checkpoint_reason="Review the provider write plan.",
    )

    stage_statuses = {stage["stage"]: stage["status"] for stage in stages}
    assert "business_research" not in stage_statuses
    assert stage_statuses == {
        "chief_of_staff": "completed",
        "approval_checkpoint": "completed",
    }


def test_canonical_graph_review_does_not_invent_stages_from_incidental_words() -> None:
    stages = langgraph_workflow._graph_requested_stage_review(
        request_text=(
            "Summarize the supplied note. It mentions Gmail, research, opportunities, "
            "and outreach only as work that must not be performed."
        ),
        manual_request_plan={
            "source": "canonical",
            "target_agent": "chief_of_staff",
            "intent": "route_request",
            "task_objective": "route_or_continue",
            "workflow": [],
        },
        node_path=["run_chief_of_staff"],
        loop_steps=[{"route": "chief_of_staff"}],
        artifact_types=["chief_of_staff_plan"],
        blocker_codes=set(),
        checkpoint_required=False,
        checkpoint_reason="",
    )

    assert stages == [
        {
            "stage": "chief_of_staff",
            "status": "completed",
            "evidence": "run_chief_of_staff",
        }
    ]


@pytest.mark.parametrize("plan_source", ["llm", "heuristic"])
def test_slack_reply_stage_is_not_cancelled_by_no_gmail_draft(
    plan_source: str,
) -> None:
    stages = langgraph_workflow._graph_requested_stage_review(
        request_text=(
            "Check yesterday's inbox, choose one email, and draft a response here. "
            "Do not send it or create a Gmail draft."
        ),
        manual_request_plan={
            "source": plan_source,
            "requested_agent": "chief_of_staff",
            "target_agent": "gmail_triage",
            "intent": "outreach_draft",
            "task_objective": "outreach_draft",
            "expected_artifact_type": "outreach_draft",
            "workflow": ["gmail_triage", "outreach_composer"],
            "provider_system": "gmail",
            "provider_operations": ["read"],
            "outreach_channel": "internal_slack",
        },
        node_path=["run_gmail_triage"],
        loop_steps=[{"route": "gmail_triage"}],
        artifact_types=["gmail_triage_report"],
        blocker_codes=set(),
        checkpoint_required=False,
        checkpoint_reason="",
    )

    stage_statuses = {stage["stage"]: stage["status"] for stage in stages}
    assert stage_statuses["gmail_triage"] == "completed"
    assert stage_statuses["outreach_composer"] == "not_completed"


def test_manager_loop_continue_keeps_cursor_and_original_objective() -> None:
    request = WorkflowRunRequest(
        request_text="Review the supplied company packet and prepare a concise reply."
    )
    result = WorkflowRunResult(
        work_item=WorkItem(kind=WorkItemKind.COMPANY_RESEARCH, title="Company packet"),
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.IN_PROGRESS,
        advanced=True,
        next_action=WorkItemNextAction(
            action="draft_reply",
            agent=WorkItemRoute.OUTREACH_COMPOSER,
        ),
    )

    updated = langgraph_workflow._manager_loop_continue_node(
        {
            "original_request": request.model_dump(mode="json"),
            "request": request.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
            "node_path": ["run_business_research", "finalize_step"],
        }
    )

    assert updated["request"]["request_text"] == "continue"
    assert updated["request"]["work_item_id"] == result.work_item.id
    assert updated["request"]["manual_request_plan"]["objective"] == request.request_text


def test_langgraph_finalize_applies_same_blocking_semantic_review_as_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailedReview:
        status = "fail"
        overall_score = 40
        approval_boundary_ok = True
        observed_gaps = ["Output did not answer the request."]
        recommended_next_step = "Repair the output before presenting it."

    monkeypatch.setattr(
        workflow_runner,
        "review_specialist_output",
        lambda **_kwargs: FailedReview(),
    )
    monkeypatch.setattr(
        langgraph_workflow,
        "finalize_prepared_work_item_step",
        lambda _prepared, result, **_kwargs: result,
    )
    monkeypatch.setattr(
        langgraph_workflow,
        "apply_planned_workflow_continuation",
        lambda result, **_kwargs: result,
    )
    request = WorkflowRunRequest(
        request_text="Assess the supplied evidence.",
        save=False,
    )
    work_item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="Evidence assessment",
        request_text=request.request_text,
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.IN_PROGRESS,
    )
    prepared = workflow_runner.PreparedWorkItemStep(
        request=request,
        work_item=work_item,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        input_text=request.request_text,
        context_pack={},
    )
    result = WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.IN_PROGRESS,
        advanced=True,
        human_summary="Unrelated output.",
    )

    updated = langgraph_workflow._finalize_step_node(
        {
            "request": request.model_dump(mode="json"),
            "original_request": request.model_dump(mode="json"),
            "prepared_step": langgraph_workflow._prepared_step_payload(prepared),
            "result": result.model_dump(mode="json"),
            "manager_loop": True,
            "loop_steps": [],
            "node_path": ["run_business_research"],
        }
    )
    reconciled = WorkflowRunResult.model_validate(updated["result"])

    assert reconciled.status == WorkItemStatus.BLOCKED
    assert any(
        blocker.code == "manager_loop_review_failed"
        for blocker in reconciled.blockers
    )
    assert reconciled.next_action is not None
    assert reconciled.next_action.action == "repair_or_deepen_specialist_output"
    assert updated["status"] == WorkItemStatus.BLOCKED.value
    assert updated["loop_steps"][0]["status"] == WorkItemStatus.BLOCKED.value


def test_llm_plan_not_context_keywords_owns_graph_context_edge() -> None:
    request_text = (
        "Zotero, Google Workspace, RSS, and preprints are incidental notes. "
        "Use the selected Airtable context before the opportunity review."
    )
    request = WorkflowRunRequest(
        request_text=request_text,
        manual_request_plan={
            "source": "llm",
            "target_agent": "chief_of_staff",
            "workflow": ["airtable_context_agent", "opportunity_scout"],
            "intent": "route_request",
            "requires_durable_state": True,
        },
    )
    prepared = workflow_runner.PreparedWorkItemStep(
        request=request,
        work_item=WorkItem(kind=WorkItemKind.OPPORTUNITY, title="Context edge"),
        route=WorkItemRoute.OPPORTUNITY_SCOUT,
        input_text=request_text,
        context_pack={},
    )

    assert langgraph_workflow._airtable_context_edge_requested(request_text, prepared)
    assert langgraph_workflow._airtable_context_should_stage_before_specialist(
        request_text,
        prepared,
    )
    assert not langgraph_workflow._zotero_context_edge_requested(request_text, prepared)
    assert not langgraph_workflow._google_workspace_context_edge_requested(
        request_text,
        prepared,
    )
    assert langgraph_workflow._feed_context_edge_kind(request_text, prepared) is None


def test_llm_plan_without_context_handoff_ignores_all_context_keywords() -> None:
    request_text = (
        "Airtable context, Google Workspace, Zotero, RSS, and preprints all appear "
        "in the prior thread, but they are not part of this selected opportunity step."
    )
    request = WorkflowRunRequest(
        request_text=request_text,
        manual_request_plan={
            "source": "llm",
            "target_agent": "opportunity_scout",
            "workflow": [],
            "intent": "opportunity_search",
        },
    )
    prepared = workflow_runner.PreparedWorkItemStep(
        request=request,
        work_item=WorkItem(kind=WorkItemKind.OPPORTUNITY, title="No context edge"),
        route=WorkItemRoute.OPPORTUNITY_SCOUT,
        input_text=request_text,
        context_pack={},
    )

    assert not langgraph_workflow._airtable_context_edge_requested(request_text, prepared)
    assert not langgraph_workflow._google_workspace_context_edge_requested(
        request_text,
        prepared,
    )
    assert not langgraph_workflow._zotero_context_edge_requested(request_text, prepared)
    assert langgraph_workflow._feed_context_edge_kind(request_text, prepared) is None


@pytest.mark.parametrize(
    (
        "request_text",
        "workflow",
        "context_agent",
        "route_helper",
        "expected_route",
    ),
    [
        (
            "Use the RSS history for a research brief. An old note says to rank opportunities.",
            ["rss_context_agent", "business_research_analyst"],
            "rss_context_agent",
            "feed",
            WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        ),
        (
            "Use the RSS history, then give me the most useful next step.",
            ["rss_context_agent", "opportunity_scout"],
            "rss_context_agent",
            "feed",
            WorkItemRoute.OPPORTUNITY_SCOUT,
        ),
        (
            "Use the selected Zotero evidence. The paper mentions opportunity scoring.",
            ["zotero_context_agent", "outreach_composer"],
            "zotero_context_agent",
            "zotero",
            WorkItemRoute.OUTREACH_COMPOSER,
        ),
    ],
)
def test_canonical_plan_owns_route_after_context_staging(
    request_text: str,
    workflow: list[str],
    context_agent: str,
    route_helper: str,
    expected_route: WorkItemRoute,
) -> None:
    request = WorkflowRunRequest(
        request_text=request_text,
        manual_request_plan={
            "source": "canonical",
            "target_agent": "chief_of_staff",
            "workflow": workflow,
            "intent": "route_request",
        },
    )
    prepared = workflow_runner.PreparedWorkItemStep(
        request=request,
        work_item=WorkItem(kind=WorkItemKind.RESEARCH_BRIEF, title="Context route"),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        input_text=request_text,
        context_pack={},
    )

    if route_helper == "feed":
        actual = langgraph_workflow._route_after_feed_context(
            prepared.route,
            request_text,
            prepared=prepared,
            context_agent=context_agent,
        )
    else:
        actual = langgraph_workflow._route_after_zotero_context(
            prepared.route,
            request_text,
            prepared=prepared,
        )

    assert actual == expected_route


def test_canonical_chief_plan_owns_pre_context_coordination_without_trigger_words() -> None:
    request_text = "Please handle the selected review and return one combined answer."
    request = WorkflowRunRequest(
        request_text=request_text,
        manual_request_plan={
            "source": "canonical",
            "target_agent": "chief_of_staff",
            "workflow": [
                "rss_context_agent",
                "business_research_analyst",
            ],
            "intent": "route_request",
            "requires_durable_state": True,
        },
    )
    prepared = workflow_runner.PreparedWorkItemStep(
        request=request,
        work_item=WorkItem(kind=WorkItemKind.RESEARCH_BRIEF, title="Chief review"),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        input_text=request_text,
        context_pack={},
    )

    assert langgraph_workflow._chief_coordination_should_run_before_context_edges(
        {},
        prepared,
    )

    after_chief = workflow_runner.PreparedWorkItemStep(
        request=request,
        work_item=prepared.work_item.model_copy(
            update={
                "artifact_refs": [
                    WorkItemArtifactRef(
                        artifact_type="chief_of_staff_plan",
                        artifact_id="chief-plan-1",
                        source_agent="chief_of_staff",
                    )
                ]
            }
        ),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        input_text=request_text,
        context_pack={},
    )
    assert not langgraph_workflow._chief_coordination_should_run_before_context_edges(
        {},
        after_chief,
    )


@pytest.mark.parametrize(
    ("provider_system", "helper_name"),
    [
        ("airtable", "airtable"),
        ("google_workspace", "google_workspace"),
    ],
)
def test_canonical_provider_fields_own_graph_write_plan_admission(
    provider_system: str,
    helper_name: str,
) -> None:
    request_text = (
        "Use the selected context for review. The old architecture note says to create "
        "and update a provider record, but that is not this task."
    )

    def prepared_for(operations: list[str]) -> workflow_runner.PreparedWorkItemStep:
        request = WorkflowRunRequest(
            request_text=request_text,
            manual_request_plan={
                "source": "canonical",
                "target_agent": "opportunity_scout",
                "workflow": [
                    (
                        "airtable_context_agent"
                        if provider_system == "airtable"
                        else "google_workspace_context_agent"
                    ),
                    "opportunity_scout",
                ],
                "intent": "route_request",
                "provider_system": provider_system,
                "provider_operations": operations,
            },
        )
        return workflow_runner.PreparedWorkItemStep(
            request=request,
            work_item=WorkItem(kind=WorkItemKind.OPPORTUNITY, title="Write admission"),
            route=WorkItemRoute.OPPORTUNITY_SCOUT,
            input_text=request_text,
            context_pack={},
        )

    read_only = prepared_for(["read"])
    write_requested = prepared_for(["create", "verify"])
    if helper_name == "airtable":
        assert not langgraph_workflow._airtable_write_plan_requested(
            request_text,
            read_only,
        )
        assert langgraph_workflow._airtable_write_plan_requested(
            "Use the reviewed context and prepare the next step.",
            write_requested,
        )
    else:
        assert not langgraph_workflow._google_workspace_artifact_plan_requested(
            request_text,
            read_only,
        )
        assert langgraph_workflow._google_workspace_artifact_plan_requested(
            "Use the reviewed context and prepare the next step.",
            write_requested,
        )


def test_older_canonical_plan_without_provider_fields_keeps_bounded_write_fallback() -> None:
    request_text = "Prepare an Airtable record write plan for approval review."
    request = WorkflowRunRequest(
        request_text=request_text,
        manual_request_plan={
            "source": "canonical",
            "target_agent": "chief_of_staff",
            "workflow": ["airtable_context_agent"],
            "intent": "route_request",
        },
    )
    prepared = workflow_runner.PreparedWorkItemStep(
        request=request,
        work_item=WorkItem(kind=WorkItemKind.OPPORTUNITY, title="Compatibility plan"),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        input_text=request_text,
        context_pack={},
    )

    assert langgraph_workflow._airtable_write_plan_requested(
        request_text,
        prepared,
    )


@pytest.mark.parametrize(
    ("provider_system", "request_text", "write_helper"),
    [
        (
            "airtable",
            "Review the current Airtable context and recommend my next actions.",
            langgraph_workflow._airtable_write_plan_requested,
        ),
        (
            "google_workspace",
            "Review the current Google Workspace context and recommend my next actions.",
            langgraph_workflow._google_workspace_artifact_plan_requested,
        ),
    ],
)
def test_compatibility_read_only_plan_cannot_become_provider_write_plan(
    provider_system: str,
    request_text: str,
    write_helper: object,
) -> None:
    request = WorkflowRunRequest(
        request_text=request_text,
        manual_request_plan={
            "source": "heuristic",
            "requested_agent": "chief_of_staff",
            "target_agent": "chief_of_staff",
            "intent": "context_lookup",
            "task_objective": "context_lookup",
            "expected_artifact_type": "context_summary",
            "provider_system": provider_system,
            "provider_operations": ["read"],
            "ask_shape": {"permission_state": "read_only"},
        },
    )
    prepared = workflow_runner.PreparedWorkItemStep(
        request=request,
        work_item=WorkItem(kind=WorkItemKind.OPPORTUNITY, title="Read-only context"),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        input_text=request_text,
        context_pack={},
    )

    assert write_helper(request_text, prepared) is False


@pytest.mark.parametrize(
    ("provider_system", "write_helper"),
    [
        ("airtable", langgraph_workflow._airtable_write_plan_requested),
        (
            "google_workspace",
            langgraph_workflow._google_workspace_artifact_plan_requested,
        ),
    ],
)
def test_canonical_replay_read_only_ceiling_overrides_stored_write_operations(
    provider_system: str,
    write_helper: object,
) -> None:
    request_text = "Review the selected provider context without changing it."
    request = WorkflowRunRequest(
        request_text=request_text,
        manual_request_plan={
            "source": "canonical:replay_fixture",
            "requested_agent": "chief_of_staff",
            "target_agent": "chief_of_staff",
            "intent": "business_system_write",
            "task_objective": "business_system_write",
            "expected_artifact_type": "business_system_write_plan",
            "provider_system": provider_system,
            "provider_operations": ["read", "create", "update", "delete", "attach"],
            "ask_shape": {"permission_state": "read_only"},
        },
    )
    prepared = workflow_runner.PreparedWorkItemStep(
        request=request,
        work_item=WorkItem(kind=WorkItemKind.OPPORTUNITY, title="Read-only replay"),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        input_text=request_text,
        context_pack={},
    )

    assert write_helper(request_text, prepared) is False


@pytest.mark.parametrize("plan_source", ["llm", "heuristic"])
def test_chief_context_advisors_are_not_counted_as_post_chief_graph_stages(
    plan_source: str,
) -> None:
    requested_stages = langgraph_workflow._graph_requested_stage_review(
        request_text="Review the selected context and return one combined answer.",
        manual_request_plan={
            "source": plan_source,
            "requested_agent": "chief_of_staff",
            "target_agent": "chief_of_staff",
            "workflow": ["gmail_triage", "airtable_context_agent"],
            "intent": "context_lookup",
            "task_objective": "context_lookup",
            "expected_artifact_type": "context_summary",
            "ask_shape": {"permission_state": "read_only"},
            "requires_durable_state": True,
        },
        manual_intent="context_lookup",
        node_path=["run_chief_of_staff"],
        loop_steps=[{"route": "chief_of_staff"}],
        artifact_types=["chief_of_staff_plan"],
        blocker_codes=set(),
        checkpoint_required=False,
        checkpoint_reason="",
    )

    assert requested_stages == [
        {
            "stage": "chief_of_staff",
            "status": "completed",
            "evidence": "run_chief_of_staff",
        }
    ]


def test_chief_context_plan_counts_admitted_analytic_owner_as_graph_stage() -> None:
    requested_stages = langgraph_workflow._graph_requested_stage_review(
        request_text="Compare the selected Zotero and preprints evidence.",
        manual_request_plan={
            "source": "llm",
            "requested_agent": "chief_of_staff",
            "target_agent": "chief_of_staff",
            "workflow": [
                "preprints_context_agent",
                "zotero_context_agent",
                "business_research_analyst",
            ],
            "intent": "context_lookup",
            "task_objective": "context_lookup",
            "expected_artifact_type": "context_summary",
            "ask_shape": {"permission_state": "read_only"},
            "requires_durable_state": True,
        },
        manual_intent="context_lookup",
        node_path=["run_chief_of_staff", "run_business_research"],
        loop_steps=[
            {"route": "chief_of_staff"},
            {"route": "business_research_analyst"},
        ],
        artifact_types=["chief_of_staff_plan", "company_profile"],
        blocker_codes=set(),
        checkpoint_required=False,
        checkpoint_reason="",
    )

    assert [item["stage"] for item in requested_stages] == [
        "chief_of_staff",
        "business_research",
    ]
    assert all(item["status"] == "completed" for item in requested_stages)


def test_canonical_graph_route_survives_incidental_words_after_context_stage(
    tmp_path: Path,
) -> None:
    request_text = (
        "Use RSS context for a short research brief on behavioral-health measurement. "
        "A quoted old note says to rank opportunities, but the current task is research."
    )
    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=request_text,
            database_url=_database_url(tmp_path),
            save=True,
            manual_request_plan={
                "source": "canonical",
                "target_agent": "business_research_analyst",
                "workflow": [
                    "rss_context_agent",
                    "business_research_analyst",
                ],
                "intent": "research_brief",
                "requires_durable_state": True,
            },
        ),
        manager_loop=True,
        max_manager_steps=2,
    )

    rss_artifact = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == "rss_context_summary"
    )
    assert "stage_feed_context" in outcome.node_path
    assert "run_business_research" in outcome.node_path
    assert "run_opportunity_scout" not in outcome.node_path
    assert rss_artifact.metadata["recommended_downstream_route"] == (
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    )


@pytest.mark.parametrize(
    ("comparison_ready", "expected_status"),
    [
        (True, WorkItemStatus.DONE),
        (False, WorkItemStatus.BLOCKED),
    ],
)
def test_graph_multi_target_research_preserves_semantic_completion_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    comparison_ready: bool,
    expected_status: WorkItemStatus,
) -> None:
    def fake_multi_target(
        plan: MultiTargetResearchPlan,
        **_kwargs: object,
    ) -> MultiTargetResearchResult:
        packets = (
            [
                PerTargetResearchPacket(
                    target_name=name,
                    source_refs=[
                        {
                            "source_id": f"{name}:source",
                            "title": f"{name} source",
                            "url": f"https://{name.lower().replace(' ', '')}.example/",
                            "source_type": "company_site",
                            "supported_claims": [
                                f"{name} has source-backed multimodal behavioral-health evidence."
                            ],
                            "evidence_excerpt": (
                                f"{name} has source-backed multimodal behavioral-health evidence."
                            ),
                        }
                    ],
                    extraction_status="extracted",
                    source_sufficient=True,
                )
                for name in ("Limbic", "Ellipsis Health", "Affectiva Health")
            ]
            if comparison_ready
            else []
        )
        return MultiTargetResearchResult(
            plan=plan,
            selected_targets=[packet.target_name for packet in packets],
            packets=packets,
            comparison_ready=comparison_ready,
            blockers=[] if comparison_ready else ["No candidate had reader-usable evidence."],
            diagnostics={"ready_packet_count": len(packets)},
            pass_types=["candidate_discovery", "target_selection", "per_target_depth"],
        )

    monkeypatch.setattr(workflow_runner, "run_multi_target_research", fake_multi_target)
    monkeypatch.setattr(
        workflow_runner,
        "retrieve_company_profile_live",
        lambda **_kwargs: pytest.fail("graph must not enter single-company retrieval"),
    )
    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="Review the requested competitor landscape.",
            requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            manual_request_plan={
                "source": "llm",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "Deliberate AI",
                "target_type": "company",
                "task_objective": "entity_research",
                "expected_artifact_type": "research_brief",
                "requires_target_discovery": True,
                "requires_live_search": True,
            },
        )
    )

    assert outcome.result.status == expected_status
    assert outcome.result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert [artifact.artifact_type for artifact in outcome.result.artifact_refs] == [
        "multi_target_research"
    ]
    if comparison_ready:
        assert "Page Unavailable" not in outcome.result.human_summary
    else:
        assert any(
            blocker.code == "multi_target_research_insufficient"
            for blocker in outcome.result.blockers
        )


@pytest.mark.parametrize(
    ("bounded_search_exhausted", "expected_status"),
    [
        (False, WorkItemStatus.BLOCKED),
        (True, WorkItemStatus.DONE),
    ],
)
def test_direct_and_graph_share_assessed_maximum_count_completion_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bounded_search_exhausted: bool,
    expected_status: WorkItemStatus,
) -> None:
    def fake_multi_target(
        plan: MultiTargetResearchPlan,
        **_kwargs: object,
    ) -> MultiTargetResearchResult:
        packets = [
            PerTargetResearchPacket(
                target_name=name,
                source_refs=[
                    {
                        "source_id": f"{name}:source",
                        "title": f"{name} source",
                        "url": f"https://{name.lower()}.example/",
                        "source_type": "company_site",
                        "supported_claims": [f"{name} matches the requested rubric."],
                    }
                ],
                extraction_status="extracted",
                source_sufficient=True,
            )
            for name in ("Alpha", "Beta")
        ]
        count_satisfied = bounded_search_exhausted
        gaps = [] if count_satisfied else ["one additional qualified target remains"]
        return MultiTargetResearchResult(
            plan=plan,
            selected_targets=[packet.target_name for packet in packets],
            packets=packets,
            comparison_ready=True,
            readiness=MultiTargetReadiness(
                assessed=True,
                rubric_dimensions=["enterprise integration"],
                peer_goal=3,
                ready_peer_count=2,
                rubric_ready_peer_count=2,
                peer_comparison_ready=False,
                bounded_search_exhausted=bounded_search_exhausted,
                count_contract_satisfied=count_satisfied,
                whole_request_ready=count_satisfied,
                gaps=gaps,
            ),
            blockers=gaps,
            diagnostics={"ready_packet_count": 2},
            pass_types=["candidate_discovery", "target_selection", "per_target_depth"],
        )

    monkeypatch.setattr(workflow_runner, "run_multi_target_research", fake_multi_target)
    plan = {
        "source": "llm",
        "target_agent": "business_research_analyst",
        "intent": "company_research",
        "task_objective": "entity_research",
        "primary_target": "Anchor Health",
        "target_type": "company",
        "expected_artifact_type": "research_brief",
        "desired_count": 3,
        "desired_count_explicit": True,
        "desired_count_mode": "maximum",
        "desired_count_scope": "additional",
        "requires_target_discovery": True,
        "requires_live_search": True,
        "required_terms": ["enterprise integration"],
    }
    direct = workflow_runner.advance_work_item(
        WorkflowRunRequest(
            request_text="Review the typed competitor set.",
            requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            database_url=f"sqlite:///{tmp_path / 'direct.db'}",
            save=True,
            live_search=True,
            manual_request_plan=plan,
        )
    )
    graph = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="Review the typed competitor set.",
            requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            database_url=f"sqlite:///{tmp_path / 'graph.db'}",
            save=True,
            live_search=True,
            manual_request_plan=plan,
        )
    ).result

    assert direct.status == graph.status == expected_status, {
        "direct_status": direct.status,
        "direct_route": direct.route,
        "direct_artifacts": [artifact.artifact_type for artifact in direct.artifact_refs],
        "graph_status": graph.status,
        "graph_route": graph.route,
        "graph_artifacts": [artifact.artifact_type for artifact in graph.artifact_refs],
    }
    assert [blocker.code for blocker in direct.blockers] == [
        blocker.code for blocker in graph.blockers
    ]
    if expected_status == WorkItemStatus.BLOCKED:
        assert [blocker.code for blocker in direct.blockers] == [
            "multi_target_research_insufficient"
        ]
    else:
        assert direct.blockers == graph.blockers == []


def test_graph_terminal_summary_integrates_decision_evidence_and_draft() -> None:
    work_item = WorkItem(
        kind=WorkItemKind.OUTREACH,
        title="Northstar review",
        facts=[
            WorkItemFact(
                key="product_focus",
                value="Northstar provides workflow analytics for behavioral-health clinics.",
                approval_state=ApprovalState.APPROVED_FOR_DRAFTING.value,
            ),
            WorkItemFact(
                key="inbound_request",
                value="Jordan asked whether Keystone could advise on evaluation design.",
                approval_state=ApprovalState.APPROVED_FOR_DRAFTING.value,
            ),
        ],
        artifact_refs=[
            WorkItemArtifactRef(
                artifact_type="outreach_draft",
                artifact_id="draft-1",
                source_agent=WorkItemRoute.OUTREACH_COMPOSER.value,
                metadata={
                    "model_recommendation": {
                        "reply_recommended": True,
                        "recommended_next_step": "Ask for a short exploratory conversation.",
                        "additional_information_needed": ["Workflow specifics"],
                    }
                },
            )
        ],
    )
    result = WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.OUTREACH_COMPOSER,
        status=WorkItemStatus.NEEDS_APPROVAL,
        advanced=True,
        human_summary=(
            "*Answer:*\nExploratory fit.\n\n"
            "*Suggested reply:*\nSubject: Re: evaluation design\n\n"
            "Hi Jordan,\n\nWould a brief conversation be useful?\n\n"
            "*Supporting evidence and approval status:*\n- Source basis: fixture:id"
        ),
    )

    enhanced = langgraph_workflow._enhance_graph_terminal_summary(
        original_request=WorkflowRunRequest(
            request_text=(
                "The visible result should state the recommendation, strongest evidence, "
                "main uncertainty, next step, and draft reply."
            )
        ),
        result=result,
        graph_completion_review={
            "completed_routes": ["gmail_triage", "business_research_analyst", "outreach_composer"]
        },
    )

    assert enhanced.human_summary.startswith("*Recommendation:*")
    assert "*Strongest evidence:*" in enhanced.human_summary
    assert "*Main uncertainty:*" in enhanced.human_summary
    assert "*Next step:*" in enhanced.human_summary
    assert "*Draft for review:*" in enhanced.human_summary
    assert "Would a brief conversation be useful?" in enhanced.human_summary
    assert "Exploratory fit" not in enhanced.human_summary
    assert "fixture:id" not in enhanced.human_summary
    assert "LangGraph terminal operator brief composed deterministically" in " ".join(
        enhanced.audit_notes
    )


def test_graph_terminal_summary_handles_thread_local_gmail_draft() -> None:
    work_item = WorkItem(
        kind=WorkItemKind.OUTREACH,
        title="Gmail validation reply",
        facts=[
            WorkItemFact(
                key="synthetic_validation_scope",
                value="The selected correspondence is synthetic validation material.",
                approval_state=ApprovalState.APPROVED_FOR_DRAFTING.value,
            ),
            WorkItemFact(
                key="unsupported_claim_boundary",
                value="No real client or prior relationship is supported.",
                approval_state=ApprovalState.APPROVED_FOR_RESEARCH.value,
            ),
        ],
        artifact_refs=[
            WorkItemArtifactRef(
                artifact_type="outreach_draft",
                artifact_id="draft-2",
                source_agent=WorkItemRoute.OUTREACH_COMPOSER.value,
                metadata={"thread_local_slack_draft": True},
            )
        ],
    )
    result = WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.OUTREACH_COMPOSER,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary="Thanks for sharing this. Would a brief conversation be useful?",
    )

    enhanced = langgraph_workflow._enhance_graph_terminal_summary(
        original_request=WorkflowRunRequest(
            request_text=(
                "The visible result should state the recommendation, strongest evidence, "
                "main uncertainty, next step, and draft reply."
            )
        ),
        result=result,
        graph_completion_review={
            "completed_routes": ["gmail_triage", "outreach_composer"]
        },
    )

    assert "Proceed with a brief exploratory reply after human review." in (
        enhanced.human_summary
    )
    assert "*Main uncertainty:*\n- No real client or prior relationship is supported." in (
        enhanced.human_summary
    )
    assert "no Gmail/provider draft or send is authorized" in enhanced.human_summary


def test_graph_terminal_summary_promotes_research_datasource_evidence() -> None:
    work_item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="Research Acme Data",
        target=WorkItemTarget(
            name="Acme Data",
            metadata={
                "gmail_research_focus_terms": ["Acme Analytics"],
                "gmail_research_target": "Acme Health",
            },
        ),
        artifact_refs=[
            WorkItemArtifactRef(
                artifact_type="company_profile",
                artifact_id="profile-1",
                source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                title="Acme Data",
                metadata={
                    "source_refs": [
                        {
                            "title": "Acme platform update",
                            "url": "https://example.test/platform",
                            "key_facts": [
                                (
                                    "Acme Analytics uses de-identified clinical records "
                                    "from partner health systems."
                                ),
                                (
                                    "Tokenization allows clinical data to be linked with "
                                    "claims outcomes."
                                ),
                            ],
                        },
                        {
                            "title": "Acme company",
                            "url": "https://example.test/company",
                            "key_facts": [
                                (
                                    "The dataset contains longitudinal behavioral-health "
                                    "patient encounters."
                                )
                            ],
                        },
                        {
                            "title": "Irrelevant registry glossary",
                            "url": "https://example.test/glossary",
                            "key_facts": ["A generic clinical study glossary."],
                        },
                    ]
                },
            )
        ],
    )
    result = WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary="Generic research artifact summary.",
    )

    enhanced = langgraph_workflow._enhance_graph_terminal_summary(
        original_request=WorkflowRunRequest(
            request_text=(
                "Research the company, determine the underlying datasource, distinguish "
                "supported facts from inference, summarize limitations and the KNI "
                "collaboration angle, and include source links."
            )
        ),
        result=result,
        graph_completion_review={
            "completed_routes": ["gmail_triage", "business_research_analyst"]
        },
    )

    assert enhanced.human_summary.startswith("*Answer:*")
    assert "identifies Acme Health as the organization" in enhanced.human_summary
    assert "*Organization:*" in enhanced.human_summary
    assert "Acme Health is the sender organization" in enhanced.human_summary
    assert "Acme Analytics" in enhanced.human_summary
    assert "*Directly supported facts:*" in enhanced.human_summary
    assert "de-identified clinical records" in enhanced.human_summary
    assert "*Inference and unknowns:*" in enhanced.human_summary
    assert "*Why it matters for KNI:*" in enhanced.human_summary
    assert "*Sources:*" in enhanced.human_summary
    assert "https://example.test/platform" in enhanced.human_summary
    assert "https://example.test/glossary" not in enhanced.human_summary
    assert "source-provenance and fitness-for-purpose" in enhanced.human_summary
    assert "LangGraph research terminal brief composed deterministically" in " ".join(
        enhanced.audit_notes
    )
    assert (
        enhanced.user_facing_summary_authority
        == UserFacingSummaryAuthority.CANONICAL
    )


def test_optional_langgraph_workflow_advances_existing_work_item_runner(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="research NeuroFlow",
            database_url=database_url,
            save=True,
        )
    )

    store = SQLiteStore(database_url)
    events = store.list_work_item_events(outcome.result.work_item.id)

    assert outcome.result.advanced is True
    assert outcome.result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert outcome.result.context_pack is not None
    assert outcome.result.context_pack["pack_type"] == "research"
    assert outcome.graph_runtime in {"langgraph", "dependency_free_fallback"}
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_business_research",
        "finalize_step",
    ]
    assert "prepare_work_item" in outcome.node_path
    assert "run_business_research" in outcome.node_path
    assert any(event.event_type == "langgraph_orchestration" for event in events)
    assert any("explicit graph nodes" in item for item in outcome.improvements)


def test_langgraph_reuses_one_request_runtime_across_graph_nodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    original = RequestRuntime.from_workflow_request.__func__

    def tracked_runtime(cls, request):
        calls.append(request.database_url or "")
        return original(cls, request)

    monkeypatch.setattr(
        RequestRuntime,
        "from_workflow_request",
        classmethod(tracked_runtime),
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="research NeuroFlow",
            database_url=_database_url(tmp_path),
            save=True,
        )
    )

    assert outcome.result.advanced is True
    assert calls == [_database_url(tmp_path)]


def test_langgraph_in_memory_store_survives_checkpoint_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stores: list[SQLiteStore] = []
    original = langgraph_workflow._record_langgraph_checkpoint_event

    def capture_store(**kwargs):
        stores.append(kwargs["store"])
        return original(**kwargs)

    monkeypatch.setattr(
        langgraph_workflow,
        "_record_langgraph_checkpoint_event",
        capture_store,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="research NeuroFlow",
            database_url="sqlite:///:memory:",
            save=True,
        )
    )

    assert outcome.result.advanced is True
    events = stores[0].list_work_item_events(outcome.result.work_item.id)
    assert any(event.event_type == "langgraph_orchestration" for event in events)


def test_langgraph_request_runtime_context_resets_after_invoke_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingGraph:
        def invoke(self, *_args, **_kwargs):
            raise RuntimeError("graph failed")

    monkeypatch.setattr(langgraph_workflow, "langgraph_available", lambda: True)
    monkeypatch.setattr(
        langgraph_workflow,
        "build_work_item_langgraph",
        lambda **_kwargs: FailingGraph(),
    )

    with pytest.raises(RuntimeError, match="graph failed"):
        run_work_item_langgraph(
            WorkflowRunRequest(request_text="research NeuroFlow", save=False)
        )

    assert langgraph_workflow._ACTIVE_REQUEST_RUNTIME.get() is None


def test_langgraph_restores_outer_request_runtime_after_nested_invoke() -> None:
    outer_request = WorkflowRunRequest(request_text="outer", save=False)
    outer_runtime = RequestRuntime.from_workflow_request(outer_request)
    token = langgraph_workflow._ACTIVE_REQUEST_RUNTIME.set(outer_runtime)
    try:
        outcome = run_work_item_langgraph(
            WorkflowRunRequest(request_text="research NeuroFlow", save=False)
        )

        assert outcome.result.advanced is True
        assert langgraph_workflow._ACTIVE_REQUEST_RUNTIME.get() is outer_runtime
    finally:
        langgraph_workflow._ACTIVE_REQUEST_RUNTIME.reset(token)


def test_langgraph_quality_comparison_requires_route_and_safety_fidelity() -> None:
    control = {
        "schema": "keystone.langgraph.quality_markers.v1",
        "route": WorkItemRoute.OPPORTUNITY_SCOUT.value,
        "status": WorkItemStatus.DONE.value,
        "context_evidence_count": 0,
        "durable_stage_count": 2,
        "graph_explainability": False,
        "completed_requested_stage_count": 1,
        "missing_required_stages": [],
        "side_effect_safe": True,
    }
    graph = {
        **control,
        "context_evidence_count": 2,
        "durable_stage_count": 4,
        "graph_explainability": True,
        "completed_requested_stage_count": 2,
    }

    comparison = compare_langgraph_quality(control, graph)
    unsafe_graph = compare_langgraph_quality(
        control,
        {
            **graph,
            "route": WorkItemRoute.OUTREACH_COMPOSER.value,
            "side_effect_safe": False,
        },
    )
    report = render_langgraph_quality_comparison(comparison)
    unsafe_report = render_langgraph_quality_comparison(unsafe_graph)

    assert comparison["ready_for_live_smoke"] is True
    assert comparison["improvement_markers"] == [
        "context_evidence_added",
        "durable_stages_added",
        "graph_explainability_added",
        "requested_stage_trace_added",
    ]
    assert unsafe_graph["ready_for_live_smoke"] is False
    assert "route_changed" in unsafe_graph["regression_markers"]
    assert "side_effect_safety_regressed" in unsafe_graph["regression_markers"]
    assert "Ready for bounded live smoke: yes" in report
    assert "Context evidence delta: +2" in report
    assert "Completed requested-stage delta: +1" in report
    assert "Regression markers: route_changed, side_effect_safety_regressed" in unsafe_report
    assert "Ready for bounded live smoke: no" in unsafe_report


def test_langgraph_manager_loop_runs_distinct_next_specialist_edge(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="research NeuroFlow and find matching opportunities",
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "llm",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "NeuroFlow",
                "workflow": [
                    "business_research_analyst",
                    "opportunity_scout",
                ],
                "requires_durable_state": True,
            },
        ),
        manager_loop=True,
        max_manager_steps=2,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    graph_completion = next(
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    )

    assert outcome.result.route == WorkItemRoute.OPPORTUNITY_SCOUT
    assert outcome.result.status == WorkItemStatus.DONE
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_business_research",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_opportunity_scout",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert [step["route"] for step in graph_completion.metadata["steps"]] == [
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        WorkItemRoute.OPPORTUNITY_SCOUT.value,
    ]
    assert "max_steps=2" in graph_completion.metadata["stop_reason"]


def test_langgraph_honors_goal_based_cos_workflow_and_keeps_target(
    tmp_path: Path,
) -> None:
    request_text = (
        "CoS, using the approved Northstar Behavioral Analytics packet, review what "
        "is known and unknown, identify the highest-value advisory opportunity and "
        "validation gap, then draft a concise internal Slack recommendation for my "
        "review. Use supplied materials only. Do not search the web, create provider "
        "records, send email, or post."
    )
    plan = infer_manual_request_plan(
        request_text,
        requested_agent="chief_of_staff",
    )

    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=request_text,
            database_url=_database_url(tmp_path),
            context_file_path=str(
                Path(__file__).parent
                / "fixtures"
                / "graph_research_to_draft_source_bundle.json"
            ),
            requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            manual_request_plan=plan.model_dump(mode="json"),
            live_sdk=False,
            live_search=False,
            save=True,
        ),
        max_steps=3,
        use_langgraph=True,
    )

    events = SQLiteStore(_database_url(tmp_path)).list_work_item_events(
        result.work_item.id
    )
    handoffs = [
        event for event in events if event.event_type == "planned_workflow_handoff"
    ]

    assert result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert result.work_item.target.name == "Northstar Behavioral Analytics"
    assert "*Recommendation:*" in result.human_summary
    assert "*Most important validation gap:*" in result.human_summary
    assert "*Sources:*" in result.human_summary
    assert "fixture://graph-source/company-brief" in result.human_summary
    assert "Hi," not in result.human_summary
    assert "exploratory reply" not in result.human_summary
    assert {
        artifact.title
        for artifact in result.work_item.artifact_refs
        if artifact.artifact_type == "opportunity"
    } == {"Northstar Behavioral Analytics"}
    assert [
        (event.metadata["from_route"], event.metadata["to_route"])
        for event in handoffs
    ] == [
        ("business_research_analyst", "opportunity_scout"),
        ("opportunity_scout", "outreach_composer"),
    ]


def test_langgraph_preserves_supplied_note_across_automatic_research_opportunity_handoff(
    tmp_path: Path,
) -> None:
    request_text = (
        "CoS, I only have a minute. Here is a supplied read-only note—Harbor Bridge "
        "Health provides behavioral-health care-navigation software, works with health "
        "plans, and reports an outcomes measurement program. First assess what the note "
        "establishes, then decide the most credible advisory or research opportunity and "
        "the single validation gap. Use whoever is needed. Do not search the web, write "
        "provider records, send email, or post anywhere. Return one combined brief."
    )
    plan = infer_manual_request_plan(
        request_text,
        requested_agent="chief_of_staff",
    )

    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=request_text,
            database_url=_database_url(tmp_path),
            requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            manual_request_plan=plan.model_dump(mode="json"),
            live_sdk=False,
            live_search=False,
            save=True,
        ),
        max_steps=2,
        use_langgraph=True,
    )

    events = SQLiteStore(_database_url(tmp_path)).list_work_item_events(
        result.work_item.id
    )
    source_provided_research = next(
        artifact
        for artifact in result.work_item.artifact_refs
        if artifact.artifact_type == "company_profile"
        and artifact.metadata.get("source_provided") is True
    )
    retained_facts = source_provided_research.metadata["source_refs"][0]["key_facts"]

    assert plan.workflow == [
        "business_research_analyst",
        "opportunity_scout",
    ]
    assert result.route == WorkItemRoute.OPPORTUNITY_SCOUT
    assert any("care-navigation software" in fact for fact in retained_facts)
    assert any("health plans" in fact for fact in retained_facts)
    assert all("First assess" not in fact for fact in retained_facts)
    assert [
        (event.metadata["from_route"], event.metadata["to_route"])
        for event in events
        if event.event_type == "planned_workflow_handoff"
    ] == [("business_research_analyst", "opportunity_scout")]


def test_natural_cos_decision_brief_completes_planned_graph_without_post_checkpoint(
    tmp_path: Path,
) -> None:
    request_text = (
        "CoS, I have five minutes before a partnership discussion. Here is all I know: "
        "Harbor Bridge Health sells behavioral-health care-navigation software to "
        "health plans and says it tracks referral completion and care engagement, but "
        "it has not shared audited outcomes, customer references, implementation data, "
        "or an evaluation design. Give me one decision brief: what is actually "
        "supported, the strongest potential KNI advisory or research fit, the single "
        "validation question that should come first, and a short internal Slack note I "
        "can paste to the team. Use only this note; do not search, create or modify "
        "anything, draft or send email, or post anywhere else."
    )
    plan = infer_manual_request_plan(
        request_text,
        requested_agent="chief_of_staff",
    )
    database_url = _database_url(tmp_path)
    workflow_request = WorkflowRunRequest(
        request_text=request_text,
        database_url=database_url,
        requested_route=WorkItemRoute.CHIEF_OF_STAFF,
        manual_request_plan=plan.model_dump(mode="json"),
        live_sdk=False,
        live_search=False,
        save=True,
    )

    assert should_use_langgraph_for_work_item(workflow_request, manager_loop=True) is True

    result = advance_work_item_manager_loop_with_optional_langgraph(
        workflow_request,
        max_steps=4,
    )
    events = SQLiteStore(database_url).list_work_item_events(result.work_item.id)
    graph_event = next(
        event for event in events if event.event_type == "langgraph_orchestration"
    )

    assert result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert result.status == WorkItemStatus.DONE
    outreach = result.work_item.artifact_refs[-1]
    research = next(
        artifact
        for artifact in result.work_item.artifact_refs
        if artifact.artifact_type == "company_profile"
    )
    opportunity = next(
        artifact
        for artifact in result.work_item.artifact_refs
        if artifact.artifact_type == "opportunity"
    )
    assert research.metadata["source_provided"] is True
    assert "care-navigation software" in research.summary
    assert opportunity.metadata["source_provided"] is True
    assert outreach.artifact_type == "outreach_draft"
    assert outreach.metadata["internal_slack_copy"] is True
    assert result.human_summary.startswith("*Recommendation:*")
    assert "*What the supplied note supports:*" in result.human_summary
    assert "*Most important validation gap:*" in result.human_summary
    assert "care-navigation software" in result.human_summary
    assert result.human_summary.count("*Next safe action:*") == 1
    assert "Email draft" not in result.human_summary
    assert "Hi," not in result.human_summary
    assert "fixture://" not in result.human_summary
    assert graph_event.metadata["checkpoint_required"] is False
    assert "approval_checkpoint" not in graph_event.metadata["node_path"]
    assert [
        (event.metadata["from_route"], event.metadata["to_route"])
        for event in events
        if event.event_type == "planned_workflow_handoff"
    ] == [
        ("business_research_analyst", "opportunity_scout"),
        ("opportunity_scout", "outreach_composer"),
    ]
    assert all(
        artifact.metadata.get("external_write_performed") is not True
        and artifact.metadata.get("send_enabled") is not True
        for artifact in result.work_item.artifact_refs
    )


def test_resumable_approved_facts_internal_slack_recommendation_is_not_external_outreach(
    tmp_path: Path,
) -> None:
    request_text = (
        "CoS, track this as a resumable internal review. Use only these approved "
        "facts: Northstar Care sells behavioral-health referral-navigation software "
        "to health plans; it says it tracks closed-loop referrals and member "
        "engagement; it has not supplied audited outcomes, customer references, "
        "implementation data, or an evaluation design. Assess what is supported, "
        "decide the highest-value validation gap, and prepare a paste-ready internal "
        "Slack recommendation. Preserve the assessment and recommendation together "
        "so I can ask you to revise the recommendation in this thread later. Do not "
        "search, use provider tools, create or modify records, send email, or post "
        "anywhere else."
    )
    plan = infer_manual_request_plan(
        request_text,
        requested_agent="chief_of_staff",
    )
    database_url = _database_url(tmp_path)

    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=request_text,
            database_url=database_url,
            requested_route=WorkItemRoute.CHIEF_OF_STAFF,
            manual_request_plan=plan.model_dump(mode="json"),
            live_sdk=False,
            live_search=False,
            save=True,
        ),
        max_steps=4,
    )

    internal_artifact = result.work_item.artifact_refs[-1]
    blocker_codes = {blocker.code for blocker in result.work_item.blockers}

    assert result.status == WorkItemStatus.DONE
    assert result.work_item.id.startswith("wi_")
    assert internal_artifact.metadata["internal_slack_copy"] is True
    assert "outreach_requires_approved_context" not in blocker_codes
    assert result.human_summary.startswith("*Recommendation:*")
    assert "*Most important validation gap:*" in result.human_summary
    assert "Northstar Care" in result.human_summary
    assert "referral-navigation software" in result.human_summary
    assert "track this as a resumable" not in result.human_summary.lower()
    assert "assess what is supported" not in result.human_summary.lower()
    assert "Gmail draft" not in result.human_summary
    assert "Email draft" not in result.human_summary


def test_short_human_cos_stateful_review_completes_same_graph_contract(
    tmp_path: Path,
) -> None:
    request_text = (
        "CoS: Track this review. Northstar Care sells referral-navigation software "
        "but has no audited outcomes. Assess what is supported, choose the first "
        "validation gap, and give me a paste-ready internal Slack recommendation. "
        "No search or external actions."
    )
    plan = infer_manual_request_plan(
        request_text,
        requested_agent="chief_of_staff",
    )
    database_url = _database_url(tmp_path)

    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=request_text,
            database_url=database_url,
            requested_route=WorkItemRoute.CHIEF_OF_STAFF,
            manual_request_plan=plan.model_dump(mode="json"),
            live_sdk=False,
            live_search=False,
            save=True,
        ),
        max_steps=4,
    )

    assert plan.workflow == [
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
    ]
    assert result.status == WorkItemStatus.DONE
    assert result.route == WorkItemRoute.OUTREACH_COMPOSER
    research = next(
        artifact
        for artifact in result.work_item.artifact_refs
        if artifact.artifact_type == "company_profile"
    )
    opportunity = next(
        artifact
        for artifact in result.work_item.artifact_refs
        if artifact.artifact_type == "opportunity"
    )
    assert research.metadata["source_provided"] is True
    assert opportunity.metadata["source_provided"] is True
    assert research.metadata["source_refs"][0]["key_facts"] == [
        "Northstar Care sells referral-navigation software but has no audited outcomes."
    ]
    assert result.work_item.artifact_refs[-1].metadata["internal_slack_copy"] is True
    assert (
        result.user_facing_summary_authority
        == UserFacingSummaryAuthority.REVIEWABLE
    )
    assert result.human_summary.startswith("*Recommendation:*")
    assert "*Most important validation gap:*" in result.human_summary
    assert "Track this review" not in result.human_summary
    assert "*Answer:*" not in result.human_summary
    assert "*Detailed Summary:*" not in result.human_summary
    assert "Email draft" not in result.human_summary
    assert all(
        artifact.metadata.get("external_write_performed") is not True
        and artifact.metadata.get("send_enabled") is not True
        for artifact in result.work_item.artifact_refs
    )


def test_langgraph_goal_based_cos_internal_slack_uses_live_synthesis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_text = (
        "CoS, using the approved Northstar Behavioral Analytics packet, review what "
        "is known and unknown, identify the highest-value advisory opportunity and "
        "validation gap, then draft a concise internal Slack recommendation for my "
        "review. Use supplied materials only. Do not search the web, create provider "
        "records, send email, or post."
    )
    plan = infer_manual_request_plan(
        request_text,
        requested_agent="chief_of_staff",
    )
    captured: dict[str, object] = {"sdk_calls": 0, "final_review_calls": 0}

    def fake_run_retrieved_sdk_synthesis(**kwargs):
        captured["sdk_calls"] = int(captured["sdk_calls"]) + 1
        captured["agent_instructions"] = str(kwargs["agent"].instructions)
        context = kwargs["retrieve"]()
        typed_input = kwargs["normalize"](context)
        captured["approved_context"] = typed_input.approved_context

        class Outcome:
            usage = {"requests": 1, "input_tokens": 120, "output_tokens": 90}
            cost = {"estimated_usd": 0.001}
            request_cache = {"dynamic_prompt_chars": 420}
            final_output = {
                "company_name": "Northstar Behavioral Analytics",
                "email_subject": "Internal recommendation: Northstar Behavioral Analytics",
                "email_body": (
                    "*Recommendation:*\nPrioritize a bounded evaluation-design review.\n\n"
                    "*Most important validation gap:*\nVerify measured outcomes and the "
                    "decision owner.\n\n*Sources:*\n"
                    "fixture://graph-source/company-brief"
                ),
                "linkedin_note": "",
                "personalization_rationale": (
                    "Used only the supplied company packet and retained source."
                ),
                "source_ids_used": ["fixture:graph-source:company-brief"],
                "reply_recommended": True,
                "recommended_next_step": "Confirm the evaluation decision and owner.",
                "additional_information_needed": [
                    "Measured outcomes and decision owner."
                ],
                "request_coverage": {
                    "interpreted_request": (
                        "Return an internal recommendation and next safe action."
                    ),
                    "status": "complete",
                    "satisfied_dimensions": [
                        "internal recommendation",
                        "next safe action",
                    ],
                    "unmet_dimensions": [],
                    "output_form_status": "satisfied",
                    "stop_condition_status": "satisfied",
                },
            }

        return Outcome()

    def fake_compose_outreach_draft_llm_constrained(
        *,
        approved_context,
        llm_draft_payload,
        **_kwargs,
    ):
        return workflow_runner.OutreachDraft(
            company_name=approved_context.company_profile.name,
            email_subject=llm_draft_payload["email_subject"],
            email_body=llm_draft_payload["email_body"],
            personalization_rationale=llm_draft_payload[
                "personalization_rationale"
            ],
            source_ids_used=llm_draft_payload["source_ids_used"],
            drafting_mode="llm_constrained",
            approved_context_used=True,
            request_coverage=llm_draft_payload["request_coverage"],
        )

    def fake_terminal_review(result, *, request):
        captured["final_review_calls"] = int(captured["final_review_calls"]) + 1
        assert request.live_sdk is True
        return result.model_copy(
            update={
                "human_summary": (
                    "*Recommendation:*\n"
                    "Prioritize a bounded evaluation-design review.\n\n"
                    "*Most important validation gap:*\n"
                    "Verify measured outcomes and the decision owner.\n\n"
                    "*Next safe action:*\n"
                    "Confirm the evaluation decision and owner.\n\n"
                    "*Sources:*\nfixture://graph-source/company-brief"
                ),
                "audit_notes": [
                    *result.audit_notes,
                    "Live user-facing response synthesis executed.",
                ],
            }
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_retrieved_sdk_synthesis",
        fake_run_retrieved_sdk_synthesis,
    )
    monkeypatch.setattr(
        workflow_runner,
        "compose_outreach_draft_llm_constrained",
        fake_compose_outreach_draft_llm_constrained,
    )
    monkeypatch.setattr(
        langgraph_workflow,
        "synthesize_terminal_work_item_response",
        fake_terminal_review,
    )

    database_url = _database_url(tmp_path)
    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=request_text,
            database_url=database_url,
            context_file_path=str(
                Path(__file__).parent
                / "fixtures"
                / "graph_research_to_draft_source_bundle.json"
            ),
            requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            manual_request_plan=plan.model_dump(mode="json"),
            live_sdk=True,
            live_search=False,
            save=True,
        ),
        max_steps=3,
        use_langgraph=True,
    )

    outreach = result.work_item.artifact_refs[-1]
    assert captured["sdk_calls"] == 1
    assert captured["final_review_calls"] == 1
    assert "Internal Slack recommendation mode" in str(
        captured["agent_instructions"]
    )
    assert "Internal Slack recommendation mode" in str(captured["approved_context"])
    assert result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert "Prioritize a bounded evaluation-design review." in result.human_summary
    assert "fixture://graph-source/company-brief" in result.human_summary
    assert "Confirm the evaluation decision and owner." in result.human_summary
    assert "exploratory reply" not in result.human_summary
    assert outreach.metadata["internal_slack_copy"] is True
    assert outreach.metadata["sdk_synthesis_used"] is True
    assert outreach.metadata["model_recommendation"]["reply_recommended"] is True
    assert outreach.metadata["request_coverage"]["status"] == "complete"
    graph_event = next(
        event
        for event in SQLiteStore(database_url).list_work_item_events(
            result.work_item.id
        )
        if event.event_type == "langgraph_orchestration"
    )
    assert graph_event.metadata["graph_completion_review"]["llm_review_used"] is True


def test_langgraph_manager_loop_emits_graph_feedback_events(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    feedback_events: list[tuple[str, dict[str, object]]] = []

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="research NeuroFlow and find matching opportunities",
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "llm",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "NeuroFlow",
                "workflow": [
                    "business_research_analyst",
                    "opportunity_scout",
                ],
                "requires_durable_state": True,
            },
        ),
        manager_loop=True,
        max_manager_steps=2,
        feedback_callback=lambda event_type, payload: feedback_events.append(
            (event_type, payload)
        ),
    )

    event_types = [event_type for event_type, _payload in feedback_events]
    completed_payload = feedback_events[-1][1]

    assert event_types == ["manager_loop_graph_started", "manager_loop_completed"]
    assert completed_payload["schema"] == "keystone.langgraph.manager_loop_feedback.v1"
    assert completed_payload["runtime"] in {"langgraph", "dependency_free_fallback"}
    assert completed_payload["work_item_id"] == outcome.result.work_item.id
    assert completed_payload["route"] == WorkItemRoute.OPPORTUNITY_SCOUT.value
    assert completed_payload["status"] == outcome.result.status.value
    assert completed_payload["checkpoint_required"] is False
    assert completed_payload["node_path"] == outcome.node_path
    completion_review = completed_payload["graph_completion_review"]
    assert completion_review["schema"] == "keystone.langgraph.completion_review.v1"
    assert completion_review["review_mode"] == "deterministic"
    assert completion_review["llm_review_used"] is False
    assert completion_review["cost_guard"]["model_call"] is False
    assert completion_review["cost_guard"]["deterministic_hard_gates_authoritative"] is True
    assert completion_review["deterministic_gates_authoritative"] is True
    assert completion_review["completed_nodes"] == outcome.node_path
    assert completion_review["send_enabled"] is False
    assert completion_review["external_writes_enabled"] is False
    assert {
        (stage["stage"], stage["status"])
        for stage in completion_review["requested_stages"]
    } >= {
        ("business_research", "completed"),
        ("opportunity_scout", "completed"),
    }
    assert [step["route"] for step in completed_payload["steps"]] == [
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        WorkItemRoute.OPPORTUNITY_SCOUT.value,
    ]
    assert "max_steps=2" in str(completed_payload["stop_reason"])
    assert completed_payload["send_enabled"] is False


def test_langgraph_feedback_callback_failure_does_not_abort_run(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    attempted_events: list[str] = []

    def failing_feedback_callback(event_type: str, _payload: dict[str, object]) -> None:
        attempted_events.append(event_type)
        raise RuntimeError("feedback sink unavailable")

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="research NeuroFlow and find matching opportunities",
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "llm",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "NeuroFlow",
            },
        ),
        manager_loop=True,
        max_manager_steps=2,
        feedback_callback=failing_feedback_callback,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)

    assert outcome.result.route == WorkItemRoute.OPPORTUNITY_SCOUT
    assert attempted_events == ["manager_loop_graph_started", "manager_loop_completed"]
    assert any(event.event_type == "langgraph_manager_loop_completed" for event in events)


def test_backend_selected_langgraph_manager_loop_forwards_feedback_callback(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    feedback_events: list[tuple[str, dict[str, object]]] = []

    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text="research NeuroFlow and find matching opportunities",
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "llm",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "NeuroFlow",
            },
        ),
        max_steps=2,
        use_langgraph=True,
        feedback_callback=lambda event_type, payload: feedback_events.append(
            (event_type, payload)
        ),
    )

    assert result.route == WorkItemRoute.OPPORTUNITY_SCOUT
    assert [event_type for event_type, _payload in feedback_events] == [
        "manager_loop_graph_started",
        "manager_loop_completed",
    ]
    assert feedback_events[-1][1]["work_item_id"] == result.work_item.id
    assert feedback_events[-1][1]["send_enabled"] is False


def test_backend_selected_manager_loop_keeps_simple_specialist_on_simple_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_manager_loop(
        request: WorkflowRunRequest,
        *,
        max_steps: int,
        feedback_callback: object | None,
    ) -> WorkflowRunResult:
        captured["request"] = request
        captured["max_steps"] = max_steps
        captured["feedback_callback"] = feedback_callback
        return WorkflowRunResult(
            work_item=WorkItem(kind=WorkItemKind.COMPANY_RESEARCH, title="Research NeuroFlow"),
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            status=WorkItemStatus.DONE,
            advanced=True,
            human_summary="Simple specialist path used.",
        )

    def fail_graph(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("simple specialist request should not use LangGraph")

    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
    monkeypatch.setattr(langgraph_workflow, "advance_work_item_manager_loop", fake_manager_loop)
    monkeypatch.setattr(langgraph_workflow, "run_work_item_langgraph", fail_graph)

    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(request_text="research NeuroFlow"),
        max_steps=4,
    )

    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert captured["max_steps"] == 4
    assert isinstance(captured["request"], WorkflowRunRequest)
    assert captured["request"].request_text == "research NeuroFlow"


def test_backend_selected_manager_loop_keeps_planning_only_chief_ask_off_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    request_text = (
        "@KNI chief of staff plan the safest workflow to review NeuroFlow, "
        "research the best evidence, assess the opportunity, and prepare "
        "draft-only outreach. State the route, handoff order, blockers, and "
        "evidence needed before outreach."
    )

    def fake_manager_loop(
        request: WorkflowRunRequest,
        *,
        max_steps: int,
        feedback_callback: object | None,
    ) -> WorkflowRunResult:
        captured["request"] = request
        captured["max_steps"] = max_steps
        captured["feedback_callback"] = feedback_callback
        return WorkflowRunResult(
            work_item=WorkItem(kind=WorkItemKind.RESEARCH_BRIEF, title="Workflow plan"),
            route=WorkItemRoute.CHIEF_OF_STAFF,
            status=WorkItemStatus.DONE,
            advanced=True,
            human_summary="Planning-only Chief path used.",
        )

    def fail_graph(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("planning-only Chief request should not use LangGraph")

    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
    monkeypatch.setattr(langgraph_workflow, "advance_work_item_manager_loop", fake_manager_loop)
    monkeypatch.setattr(langgraph_workflow, "run_work_item_langgraph", fail_graph)

    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=request_text,
            manual_request_plan={"requested_agent": WorkItemRoute.CHIEF_OF_STAFF.value},
        ),
        max_steps=4,
    )

    assert result.route == WorkItemRoute.CHIEF_OF_STAFF
    assert captured["max_steps"] == 4
    assert isinstance(captured["request"], WorkflowRunRequest)
    assert captured["request"].request_text == request_text


def test_backend_selected_manager_loop_uses_graph_for_natural_chief_multistage_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    request_text = (
        "@KNI chief of staff NeuroFlow has been coming up as a behavioral-health AI "
        "company with payer partnership and outcomes-evidence signals. Do research, "
        "assess whether this is a real KNI advisory/research opportunity, identify "
        "what source-backed evidence is still missing, and decide whether it should "
        "stop at an approval checkpoint before any outreach. If the evidence supports "
        "pursuing it, include a draft-only Slack-thread sample outreach for review."
    )
    graph_result = WorkflowRunResult(
        work_item=WorkItem(kind=WorkItemKind.OPPORTUNITY, title="NeuroFlow graph path"),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.IN_PROGRESS,
        advanced=True,
        human_summary="Graph path used for natural Chief multi-stage ask.",
    )

    class FakeOutcome:
        result = graph_result

    def fail_manager_loop(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("natural Chief multi-stage ask should use LangGraph")

    def fake_graph(
        request: WorkflowRunRequest,
        *,
        thread_id: str | None,
        require_langgraph: bool,
        manager_loop: bool,
        max_manager_steps: int,
        feedback_callback: object | None,
    ) -> FakeOutcome:
        captured["request"] = request
        captured["thread_id"] = thread_id
        captured["require_langgraph"] = require_langgraph
        captured["manager_loop"] = manager_loop
        captured["max_manager_steps"] = max_manager_steps
        captured["feedback_callback"] = feedback_callback
        return FakeOutcome()

    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
    monkeypatch.setattr(langgraph_workflow, "advance_work_item_manager_loop", fail_manager_loop)
    monkeypatch.setattr(langgraph_workflow, "run_work_item_langgraph", fake_graph)

    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=request_text,
            manual_request_plan={
                "requested_agent": "chief_of_staff",
                "target_agent": "chief_of_staff",
                "intent": "route_request",
                "primary_target": "NeuroFlow has been coming up as a behavioral-health AI company",
            },
        ),
        max_steps=5,
    )

    assert result is graph_result
    assert isinstance(captured["request"], WorkflowRunRequest)
    assert captured["request"].request_text == request_text
    assert captured["thread_id"] is None
    assert captured["require_langgraph"] is False
    assert captured["manager_loop"] is True
    assert captured["max_manager_steps"] == 5


def test_backend_selected_manager_loop_uses_graph_for_graph_worthy_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    graph_result = WorkflowRunResult(
        work_item=WorkItem(kind=WorkItemKind.OPPORTUNITY, title="Graph-worthy flow"),
        route=WorkItemRoute.OPPORTUNITY_SCOUT,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary="Graph path used.",
    )

    class FakeOutcome:
        result = graph_result

    def fail_manager_loop(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("graph-worthy request should use LangGraph")

    def fake_graph(
        request: WorkflowRunRequest,
        *,
        thread_id: str | None,
        require_langgraph: bool,
        manager_loop: bool,
        max_manager_steps: int,
        feedback_callback: object | None,
    ) -> FakeOutcome:
        captured["request"] = request
        captured["thread_id"] = thread_id
        captured["require_langgraph"] = require_langgraph
        captured["manager_loop"] = manager_loop
        captured["max_manager_steps"] = max_manager_steps
        captured["feedback_callback"] = feedback_callback
        return FakeOutcome()

    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
    monkeypatch.setattr(langgraph_workflow, "advance_work_item_manager_loop", fail_manager_loop)
    monkeypatch.setattr(langgraph_workflow, "run_work_item_langgraph", fake_graph)

    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=(
                "use RSS context agent announcement history before Opportunity Scout"
            )
        ),
        max_steps=5,
    )

    assert result is graph_result
    assert isinstance(captured["request"], WorkflowRunRequest)
    assert captured["thread_id"] is None
    assert captured["require_langgraph"] is False
    assert captured["manager_loop"] is True
    assert captured["max_manager_steps"] == 5


def test_backend_selected_manager_loop_false_env_keeps_graph_worthy_request_simple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_manager_loop(
        request: WorkflowRunRequest,
        *,
        max_steps: int,
        feedback_callback: object | None,
    ) -> WorkflowRunResult:
        captured["request"] = request
        captured["max_steps"] = max_steps
        captured["feedback_callback"] = feedback_callback
        return WorkflowRunResult(
            work_item=WorkItem(kind=WorkItemKind.OPPORTUNITY, title="Override simple path"),
            route=WorkItemRoute.OPPORTUNITY_SCOUT,
            status=WorkItemStatus.BLOCKED,
            advanced=False,
            human_summary="Explicit false env override kept the simple path.",
        )

    def fail_graph(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("false env override should disable LangGraph")

    monkeypatch.setenv("KNI_BUSINESS_AGENTS_LANGGRAPH", "false")
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
    monkeypatch.setattr(langgraph_workflow, "advance_work_item_manager_loop", fake_manager_loop)
    monkeypatch.setattr(langgraph_workflow, "run_work_item_langgraph", fail_graph)

    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=(
                "research NeuroFlow, find matching opportunities, and prepare "
                "draft-only outreach."
            )
        ),
        max_steps=6,
    )

    assert result.route == WorkItemRoute.OPPORTUNITY_SCOUT
    assert captured["max_steps"] == 6
    assert isinstance(captured["request"], WorkflowRunRequest)
    assert "draft-only outreach" in captured["request"].request_text


def test_direct_work_item_backend_selection_keeps_simple_runs_off_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_advance_work_item(request: WorkflowRunRequest) -> WorkflowRunResult:
        captured["request"] = request
        return WorkflowRunResult(
            work_item=WorkItem(kind=WorkItemKind.COMPANY_RESEARCH, title="Research NeuroFlow"),
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            status=WorkItemStatus.DONE,
            advanced=True,
            human_summary="Direct simple runner used.",
        )

    def fail_graph(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("plain direct research should not use LangGraph")

    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
    monkeypatch.setattr(langgraph_workflow, "advance_work_item", fake_advance_work_item)
    monkeypatch.setattr(langgraph_workflow, "run_work_item_langgraph", fail_graph)

    result = advance_work_item_with_optional_langgraph(
        WorkflowRunRequest(request_text="research NeuroFlow")
    )

    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert isinstance(captured["request"], WorkflowRunRequest)
    assert captured["request"].request_text == "research NeuroFlow"


def test_direct_work_item_backend_selection_uses_graph_for_checkpoint_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    graph_result = WorkflowRunResult(
        work_item=WorkItem(kind=WorkItemKind.OUTREACH, title="Draft-only outreach"),
        route=WorkItemRoute.OUTREACH_COMPOSER,
        status=WorkItemStatus.NEEDS_APPROVAL,
        advanced=True,
        human_summary="Direct graph runner used.",
    )

    class FakeOutcome:
        result = graph_result

    def fail_advance_work_item(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("approval/draft checkpoint boundary should use LangGraph")

    def fake_graph(
        request: WorkflowRunRequest,
        *,
        thread_id: str | None,
        require_langgraph: bool,
    ) -> FakeOutcome:
        captured["request"] = request
        captured["thread_id"] = thread_id
        captured["require_langgraph"] = require_langgraph
        return FakeOutcome()

    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
    monkeypatch.setattr(langgraph_workflow, "advance_work_item", fail_advance_work_item)
    monkeypatch.setattr(langgraph_workflow, "run_work_item_langgraph", fake_graph)

    result = advance_work_item_with_optional_langgraph(
        WorkflowRunRequest(request_text="prepare draft-only outreach for approved context")
    )

    assert result is graph_result
    assert isinstance(captured["request"], WorkflowRunRequest)
    assert captured["thread_id"] is None
    assert captured["require_langgraph"] is False


def test_direct_work_item_env_overrides_are_diagnostic_not_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    simple_result = WorkflowRunResult(
        work_item=WorkItem(kind=WorkItemKind.OUTREACH, title="Override simple path"),
        route=WorkItemRoute.OUTREACH_COMPOSER,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary="Explicit false env override kept direct simple path.",
    )
    graph_result = WorkflowRunResult(
        work_item=WorkItem(kind=WorkItemKind.COMPANY_RESEARCH, title="Forced graph path"),
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary="Explicit true env override forced direct graph path.",
    )

    class FakeOutcome:
        result = graph_result

    def fake_advance_work_item(request: WorkflowRunRequest) -> WorkflowRunResult:
        captured["simple_request"] = request
        return simple_result

    def fake_graph(
        request: WorkflowRunRequest,
        *,
        thread_id: str | None,
        require_langgraph: bool,
    ) -> FakeOutcome:
        captured["graph_request"] = request
        captured["thread_id"] = thread_id
        captured["require_langgraph"] = require_langgraph
        return FakeOutcome()

    monkeypatch.setattr(langgraph_workflow, "advance_work_item", fake_advance_work_item)
    monkeypatch.setattr(langgraph_workflow, "run_work_item_langgraph", fake_graph)

    monkeypatch.setenv("KNI_BUSINESS_AGENTS_LANGGRAPH", "false")
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
    false_result = advance_work_item_with_optional_langgraph(
        WorkflowRunRequest(request_text="prepare draft-only outreach for approved context")
    )

    monkeypatch.setenv("KNI_BUSINESS_AGENTS_LANGGRAPH", "true")
    true_result = advance_work_item_with_optional_langgraph(
        WorkflowRunRequest(request_text="research NeuroFlow")
    )

    assert false_result is simple_result
    assert true_result is graph_result
    assert isinstance(captured["simple_request"], WorkflowRunRequest)
    assert "draft-only outreach" in captured["simple_request"].request_text
    assert isinstance(captured["graph_request"], WorkflowRunRequest)
    assert captured["graph_request"].request_text == "research NeuroFlow"
    assert captured["thread_id"] is None
    assert captured["require_langgraph"] is False


def test_backend_selected_manager_loop_uses_graph_for_research_opportunity_outreach_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)

    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=(
                "research NeuroFlow, find matching opportunities, and prepare "
                "draft-only outreach. Do not send, post, schedule, or write externally."
            ),
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "llm",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "NeuroFlow",
                "workflow": [
                    "business_research_analyst",
                    "opportunity_scout",
                    "outreach_composer",
                ],
                "requires_durable_state": True,
            },
        ),
        max_steps=3,
    )

    events = SQLiteStore(database_url).list_work_item_events(result.work_item.id)
    graph_event = next(event for event in events if event.event_type == "langgraph_orchestration")
    node_path = graph_event.metadata["node_path"]
    artifact_types = {artifact.artifact_type for artifact in result.work_item.artifact_refs}
    company_artifact = next(
        artifact
        for artifact in result.work_item.artifact_refs
        if artifact.artifact_type == "company_profile"
    )
    opportunity_artifacts = [
        artifact
        for artifact in result.work_item.artifact_refs
        if artifact.artifact_type == "opportunity"
    ]
    source_refs = result.context_pack.get("source_refs", [])

    assert result.route == WorkItemRoute.OUTREACH_COMPOSER, {
        "route": result.route.value,
        "status": result.status.value,
        "next_action": result.next_action.model_dump(mode="json")
        if result.next_action is not None
        else None,
        "node_path": node_path,
        "stop_reason": graph_event.metadata.get("stop_reason"),
    }
    assert result.status == WorkItemStatus.BLOCKED
    assert graph_event.metadata["checkpoint_required"] is True
    assert {
        "run_business_research",
        "run_opportunity_scout",
        "run_outreach_composer",
        "approval_checkpoint",
    } <= set(node_path)
    assert node_path.index("run_business_research") < node_path.index("run_opportunity_scout")
    assert node_path.index("run_opportunity_scout") < node_path.index("run_outreach_composer")
    assert node_path[-1] == "approval_checkpoint"
    assert {"company_profile", "opportunity"} <= artifact_types
    assert company_artifact.metadata["source_refs"]
    assert any(artifact.metadata.get("source_refs") for artifact in opportunity_artifacts)
    assert any(str(item.get("url") or "").startswith("fixture://") for item in source_refs)
    assert all(
        artifact.metadata.get("send_enabled") is not True
        for artifact in result.work_item.artifact_refs
    )
    assert all(
        artifact.metadata.get("external_writes_enabled") is not True
        for artifact in result.work_item.artifact_refs
    )
    assert [blocker.code for blocker in result.blockers] == [
        "outreach_requires_approved_context"
    ]


def test_backend_selected_manager_loop_uses_graph_for_gmail_research_outreach_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)

    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=(
                "gmail triage this sanitized inbound email from Mindful Care, "
                "research Mindful Care, and return a suggested reply with supporting "
                "evidence and the approval status. Email: "
                "From: Jordan Lee, Operations at Mindful Care. Subject: Follow-up "
                "on measurement support. Body: Hi Jordan, our team is reviewing "
                "measurement-based care workflows and may need advisory help on "
                "evaluation design. Do not send, create Gmail drafts, post, schedule, "
                "or write externally."
            ),
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "heuristic",
                "requested_agent": "orchestrator",
                "target_agent": "gmail_triage",
                "intent": "gmail_triage",
                "primary_target": "Mindful Care",
                "target_type": "gmail_thread",
                "task_objective": "gmail_triage",
            },
        ),
        max_steps=3,
    )

    events = SQLiteStore(database_url).list_work_item_events(result.work_item.id)
    graph_event = next(event for event in events if event.event_type == "langgraph_orchestration")
    node_path = graph_event.metadata["node_path"]
    checkpoint_payload = graph_event.metadata["checkpoint_payload"]
    artifact_types = {artifact.artifact_type for artifact in result.work_item.artifact_refs}
    triage_artifact = next(
        artifact
        for artifact in result.work_item.artifact_refs
        if artifact.artifact_type == "gmail_triage_report"
    )
    company_artifact = next(
        artifact
        for artifact in result.work_item.artifact_refs
        if artifact.artifact_type == "company_profile"
    )
    source_refs = result.context_pack.get("source_refs", [])

    assert result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert result.status == WorkItemStatus.NEEDS_APPROVAL
    assert graph_event.metadata["checkpoint_required"] is True
    assert checkpoint_payload["schema"] == "keystone.langgraph.approval_checkpoint.v1"
    assert checkpoint_payload["work_item_id"] == result.work_item.id
    assert checkpoint_payload["route"] == WorkItemRoute.OUTREACH_COMPOSER.value
    assert checkpoint_payload["status"] == WorkItemStatus.NEEDS_APPROVAL.value
    assert checkpoint_payload["target"]["name"] == "Mindful Care"
    assert checkpoint_payload["target"]["metadata"]["gmail_research_target"] == "Mindful Care"
    assert checkpoint_payload["send_enabled"] is False
    assert checkpoint_payload["external_writes_enabled"] is False
    assert {
        "run_gmail_triage",
        "run_business_research",
        "run_outreach_composer",
        "approval_checkpoint",
    } <= set(node_path)
    assert node_path.index("run_gmail_triage") < node_path.index("run_business_research")
    assert node_path.index("run_business_research") < node_path.index("run_outreach_composer")
    assert node_path[-1] == "approval_checkpoint"
    assert {"gmail_triage_report", "company_profile"} <= artifact_types
    assert triage_artifact.metadata["send_enabled"] is False
    assert triage_artifact.metadata["draft_created"] is False
    assert triage_artifact.metadata["labels_modified"] is False
    assert company_artifact.metadata["source_refs"]
    assert company_artifact.approval_state == ApprovalState.APPROVED_FOR_DRAFTING.value
    assert any(str(item.get("url") or "").startswith("fixture://") for item in source_refs)
    assert all(
        artifact.metadata.get("send_enabled") is not True
        for artifact in result.work_item.artifact_refs
    )
    assert all(
        artifact.metadata.get("external_writes_enabled") is not True
        for artifact in result.work_item.artifact_refs
    )
    assert result.blockers == []
    assert checkpoint_payload["blockers"] == []
    assert "*Answer:*" in result.human_summary
    assert "*Organization context:*" in result.human_summary
    assert "*Suggested reply:*" in result.human_summary
    assert "*Supporting evidence and approval status:*" in result.human_summary
    assert {"gmail_triage_report", "company_profile"} <= {
        artifact["artifact_type"] for artifact in checkpoint_payload["artifact_refs"]
    }
    assert any(
        str(source.get("url") or "").startswith("fixture://")
        for source in checkpoint_payload["source_refs"]
    )
    assert "readiness_gates" in checkpoint_payload["context_pack"]


def test_backend_selected_manager_loop_uses_graph_for_rss_opportunity_artifact_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
    store = SQLiteStore(database_url)
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="Behavioral health AI partnership announcement",
            url="https://example.org/behavioral-ai-partnership",
            source="#announcements",
            feed="rss",
            tags=["behavioral health", "ai", "partnership"],
            selected=True,
            selection_reason="Relevant partnership signal for Keystone opportunity scouting.",
            summary=(
                "A behavioral health AI vendor announced a provider partnership around "
                "measurement workflows."
            ),
            evidence=[
                AnnouncementFeedEvidence(
                    kind="article",
                    title="Partnership source",
                    url="https://example.org/behavioral-ai-partnership",
                    snippet="The announcement describes a behavioral health AI partnership.",
                    source="trafilatura",
                    status="success",
                    char_count=900,
                )
            ],
        )
    )

    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=(
                "use RSS context agent announcement history as source-provided signal, "
                "then scout behavioral health AI partnership opportunities. After the "
                "opportunity scan, use Google Workspace Context agent to plan where the "
                "opportunity packet should live in Drive, Docs, and Sheets for approval "
                "review. Use only source-provided context; do not run live research, "
                "create files, share links, update sheets, send, post, publish, "
                "schedule, or write externally."
            ),
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "heuristic",
                "requested_agent": "orchestrator",
                "target_agent": "rss_context_agent",
                "intent": "context_lookup",
                "primary_target": "behavioral health AI partnerships",
            },
        ),
        max_steps=3,
    )

    events = store.list_work_item_events(result.work_item.id)
    graph_event = next(event for event in events if event.event_type == "langgraph_orchestration")
    node_path = graph_event.metadata["node_path"]
    artifact_types = {artifact.artifact_type for artifact in result.work_item.artifact_refs}
    source_providers = {source.provider for source in result.work_item.sources}
    workspace_artifact = next(
        artifact
        for artifact in result.work_item.artifact_refs
        if artifact.artifact_type == "google_workspace_artifact_plan"
    )

    assert result.route == WorkItemRoute.CHIEF_OF_STAFF
    assert result.status == WorkItemStatus.NEEDS_APPROVAL
    assert graph_event.metadata["checkpoint_required"] is True
    assert {
        "stage_feed_context",
        "run_opportunity_scout",
        "stage_google_workspace_context",
        "approval_checkpoint",
    } <= set(node_path)
    assert node_path.index("stage_feed_context") < node_path.index("run_opportunity_scout")
    assert node_path.index("run_opportunity_scout") < node_path.index(
        "stage_google_workspace_context"
    )
    assert node_path[-1] == "approval_checkpoint"
    assert {
        "rss_context_summary",
        "opportunity",
        "google_workspace_artifact_plan",
    } <= artifact_types
    assert {"rss_context_agent", "google_workspace_context_agent"} <= source_providers
    assert workspace_artifact.metadata["write_plan"]["target_system"] == "google_workspace"
    assert workspace_artifact.metadata["external_writes_enabled"] is False
    assert workspace_artifact.metadata["send_enabled"] is False


def test_backend_selected_manager_loop_uses_graph_for_preprints_zotero_research_artifact_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
    store = SQLiteStore(database_url)
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="Preprint on depression evidence packet workflows",
            url="https://doi.org/10.1101/2026.02.05.345678",
            source="medRxiv",
            feed="preprints",
            doi="10.1101/2026.02.05.345678",
            tags=["preprint", "depression", "evidence packet"],
            selected=True,
            selection_reason="Relevant preliminary evidence for an internal packet.",
            summary="A preprint discusses evidence-packet workflow needs.",
        )
    )

    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=(
                "use preprints context agent history and Zotero context agent handoff, "
                "then research NeuroFlow for an internal evidence packet. After research, "
                "use Google Workspace Context agent to plan where the packet should live "
                "in Drive, Docs, and Sheets for approval review. Do not create files, "
                "share links, update sheets, send, publish, schedule, or write externally."
            ),
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "heuristic",
                "requested_agent": "orchestrator",
                "target_agent": "preprints_context_agent",
                "intent": "context_lookup",
                "primary_target": "NeuroFlow",
            },
        ),
        max_steps=3,
    )

    events = store.list_work_item_events(result.work_item.id)
    graph_event = next(event for event in events if event.event_type == "langgraph_orchestration")
    node_path = graph_event.metadata["node_path"]
    artifact_types = {artifact.artifact_type for artifact in result.work_item.artifact_refs}
    source_providers = {source.provider for source in result.work_item.sources}
    workspace_artifact = next(
        artifact
        for artifact in result.work_item.artifact_refs
        if artifact.artifact_type == "google_workspace_artifact_plan"
    )

    assert result.route == WorkItemRoute.CHIEF_OF_STAFF
    assert result.status == WorkItemStatus.NEEDS_APPROVAL
    assert graph_event.metadata["checkpoint_required"] is True
    assert {
        "stage_feed_context",
        "stage_zotero_context",
        "run_business_research",
        "stage_google_workspace_context",
        "approval_checkpoint",
    } <= set(node_path)
    assert node_path.index("stage_feed_context") < node_path.index("stage_zotero_context")
    assert node_path.index("stage_zotero_context") < node_path.index("run_business_research")
    assert node_path.index("run_business_research") < node_path.index(
        "stage_google_workspace_context"
    )
    assert node_path[-1] == "approval_checkpoint"
    assert {
        "preprints_context_summary",
        "zotero_context_summary",
        "company_profile",
        "google_workspace_artifact_plan",
    } <= artifact_types
    assert {
        "preprints_context_agent",
        "zotero_context_agent",
        "google_workspace_context_agent",
    } <= source_providers
    assert workspace_artifact.metadata["write_plan"]["target_system"] == "google_workspace"
    assert workspace_artifact.metadata["external_writes_enabled"] is False
    assert workspace_artifact.metadata["send_enabled"] is False


def test_backend_selected_manager_loop_uses_graph_for_chief_context_business_research(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
    store = SQLiteStore(database_url)
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="RPM AI validation announcement",
            url="https://example.org/rpm-ai-validation",
            source="#announcements",
            feed="rss",
            tags=["remote patient monitoring", "ai", "validation"],
            selected=True,
            selection_reason="Relevant context for a validation-workflow review.",
            summary=(
                "A remote patient monitoring vendor announced an AI validation "
                "workflow."
            ),
            evidence=[
                AnnouncementFeedEvidence(
                    kind="article",
                    title="RPM validation source",
                    url="https://example.org/rpm-ai-validation",
                    snippet="The announcement describes an AI validation workflow.",
                    source="trafilatura",
                    status="success",
                    char_count=700,
                )
            ],
        )
    )

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Business Research Agent is the best next owner "
                    "after RSS and Zotero context agents are staged."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=(
                "chief of staff coordinate RSS context agent announcement history "
                "and Zotero context agent handoff before Business Research reviews "
                "Example Health remote patient monitoring AI validation workflow. If "
                "recommending another agent, use Chief of Staff -> Business Research "
                "Agent notation. Do not send, post, publish, schedule, run live "
                "research, or write externally."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Example Health",
            },
        ),
        max_steps=4,
    )

    events = store.list_work_item_events(result.work_item.id)
    graph_event = next(event for event in events if event.event_type == "langgraph_orchestration")
    completion_event = next(
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    )
    node_path = graph_event.metadata["node_path"]
    artifact_types = {artifact.artifact_type for artifact in result.work_item.artifact_refs}
    source_providers = {source.provider for source in result.work_item.sources}

    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert result.advanced is True
    assert graph_event.metadata["checkpoint_required"] is False
    assert {
        "run_chief_of_staff",
        "stage_feed_context",
        "stage_zotero_context",
        "run_business_research",
        "manager_loop_finalize",
    } <= set(node_path)
    assert node_path.index("run_chief_of_staff") < node_path.index("stage_feed_context")
    assert node_path.index("stage_feed_context") < node_path.index("stage_zotero_context")
    assert node_path.index("stage_zotero_context") < node_path.index("run_business_research")
    assert {
        "chief_of_staff_plan",
        "rss_context_summary",
        "zotero_context_summary",
        "company_profile",
    } <= artifact_types
    assert {"rss_context_agent", "zotero_context_agent"} <= source_providers
    assert [step["route"] for step in completion_event.metadata["steps"]] == [
        WorkItemRoute.CHIEF_OF_STAFF.value,
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
    ]
    assert completion_event.metadata["send_enabled"] is False


def test_backend_selected_manager_loop_uses_graph_for_chief_research_airtable_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Business Research Agent is the best next owner "
                    "before Airtable Context prepares the internal write plan."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=(
                "chief of staff coordinate Business Research for Example Health's "
                "remote patient monitoring AI validation workflow, then use Airtable "
                "Context agent to plan the internal Airtable record update for approval "
                "review. If recommending another agent, use Chief of Staff -> Business "
                "Research Agent notation. Do not create records, update fields, upload "
                "attachments, send, post, publish, schedule, run live research, or write "
                "externally."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Example Health",
            },
        ),
        max_steps=4,
    )

    events = SQLiteStore(database_url).list_work_item_events(result.work_item.id)
    graph_event = next(event for event in events if event.event_type == "langgraph_orchestration")
    node_path = graph_event.metadata["node_path"]
    artifact_types = {artifact.artifact_type for artifact in result.work_item.artifact_refs}
    source_providers = {source.provider for source in result.work_item.sources}
    airtable_artifact = next(
        artifact
        for artifact in result.work_item.artifact_refs
        if artifact.artifact_type == "airtable_write_plan"
    )

    assert result.route == WorkItemRoute.CHIEF_OF_STAFF
    assert result.status == WorkItemStatus.NEEDS_APPROVAL
    assert graph_event.metadata["checkpoint_required"] is True
    assert graph_event.metadata["checkpoint_payload"]["schema"] == (
        "keystone.langgraph.approval_checkpoint.v1"
    )
    assert graph_event.metadata["checkpoint_payload"]["send_enabled"] is False
    assert graph_event.metadata["checkpoint_payload"]["external_writes_enabled"] is False
    assert node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_business_research",
        "finalize_step",
        "stage_airtable_context",
        "approval_checkpoint",
    ]
    assert {"chief_of_staff_plan", "company_profile", "airtable_write_plan"} <= artifact_types
    assert "airtable_context_agent" in source_providers
    assert airtable_artifact.metadata["write_plan"]["target_system"] == "airtable"
    assert airtable_artifact.metadata["write_plan"]["live_write_allowed_for_specialist"] is False
    assert airtable_artifact.metadata["external_writes_enabled"] is False
    assert airtable_artifact.metadata["send_enabled"] is False
    assert any(
        gate.scope == "airtable_write_plan"
        and gate.required
        and gate.state == "pending"
        for gate in result.work_item.approval_gates
    )
    assert not any(event.event_type == "langgraph_manager_loop_completed" for event in events)


def test_backend_selected_manager_loop_uses_graph_for_chief_context_gmail_research(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Gmail Triage Agent is the best next owner "
                    "after Google Workspace Context is staged. Business Research "
                    "should review the sender organization after triage."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="gmail-triage",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=(
                "chief of staff coordinate Google Workspace Context agent artifact "
                "context before Gmail Triage reviews this sanitized inbound email, "
                "then research the sender organization for KNI advisory relevance. "
                "Use Google Drive and Docs only as read-only context for the specialist "
                "handoff. Email: From: Jordan Lee, Operations at Cedar Valley Rehab. "
                "Subject: Measurement workflow support. Body: Hi Jordan, our team is "
                "reviewing measurement-based care workflows and may need advisory help. "
                "No sends, Gmail drafts, posts, scheduling, publication, file creation, "
                "sharing, updates, uploads, or external system mutation."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Cedar Valley Rehab",
            },
        ),
        max_steps=5,
    )

    events = SQLiteStore(database_url).list_work_item_events(result.work_item.id)
    graph_event = next(event for event in events if event.event_type == "langgraph_orchestration")
    completion_event = next(
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    )
    node_path = graph_event.metadata["node_path"]
    artifact_types = {artifact.artifact_type for artifact in result.work_item.artifact_refs}
    source_providers = {source.provider for source in result.work_item.sources}

    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert result.status == WorkItemStatus.DONE
    assert graph_event.metadata["checkpoint_required"] is False
    assert {
        "run_chief_of_staff",
        "stage_google_workspace_context",
        "run_gmail_triage",
        "run_business_research",
        "manager_loop_finalize",
    } <= set(node_path)
    assert node_path.index("run_chief_of_staff") < node_path.index(
        "stage_google_workspace_context"
    )
    assert node_path.index("stage_google_workspace_context") < node_path.index(
        "run_gmail_triage"
    )
    assert node_path.index("run_gmail_triage") < node_path.index("run_business_research")
    assert {
        "chief_of_staff_plan",
        "google_workspace_context_summary",
        "gmail_triage_report",
        "company_profile",
    } <= artifact_types
    assert "google_workspace_context_agent" in source_providers
    assert [step["route"] for step in completion_event.metadata["steps"]] == [
        WorkItemRoute.CHIEF_OF_STAFF.value,
        WorkItemRoute.GMAIL_TRIAGE.value,
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
    ]
    assert completion_event.metadata["send_enabled"] is False
    assert completion_event.metadata["external_writes_enabled"] is False


def test_backend_selected_manager_loop_uses_graph_for_chief_gmail_research(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Gmail Triage Agent is the best next owner "
                    "to review the sanitized inbound email before Business Research "
                    "assesses the sender organization."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="gmail-triage",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=(
                "chief of staff review this sanitized inbound email, use Gmail Triage "
                "first, then research the sender organization for KNI advisory relevance. "
                "Email: From: Jordan Lee, Operations at Mindful Care. Subject: Follow-up "
                "on measurement support. Body: Hi Jordan, our team is reviewing "
                "measurement-based care workflows and may need advisory help. Do not "
                "send, create Gmail drafts, post, schedule, publish, create files, or "
                "write externally."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Mindful Care",
            },
        ),
        max_steps=4,
    )

    events = SQLiteStore(database_url).list_work_item_events(result.work_item.id)
    graph_event = next(event for event in events if event.event_type == "langgraph_orchestration")
    completion_event = next(
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    )
    node_path = graph_event.metadata["node_path"]
    artifact_types = {artifact.artifact_type for artifact in result.work_item.artifact_refs}

    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert result.status == WorkItemStatus.DONE
    assert graph_event.metadata["checkpoint_required"] is False
    assert {
        "run_chief_of_staff",
        "run_gmail_triage",
        "run_business_research",
        "manager_loop_finalize",
    } <= set(node_path)
    assert node_path.index("run_chief_of_staff") < node_path.index("run_gmail_triage")
    assert node_path.index("run_gmail_triage") < node_path.index("run_business_research")
    assert {"chief_of_staff_plan", "gmail_triage_report", "company_profile"} <= artifact_types
    assert result.work_item.target.name == "Mindful Care"
    assert result.work_item.target.metadata["gmail_research_target"] == "Mindful Care"
    assert [step["route"] for step in completion_event.metadata["steps"]] == [
        WorkItemRoute.CHIEF_OF_STAFF.value,
        WorkItemRoute.GMAIL_TRIAGE.value,
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
    ]
    assert completion_event.metadata["send_enabled"] is False
    assert completion_event.metadata["external_writes_enabled"] is False


def test_langgraph_chief_specialist_tools_are_advisory_and_do_not_replace_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    captured: dict[str, object] = {}

    def fake_run_chief_of_staff_sdk(
        sdk_input: dict[str, object],
        **kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        captured["sdk_include_specialist_tools"] = sdk_input.get("include_specialist_tools")
        captured["kwarg_include_specialist_tools"] = kwargs.get("include_specialist_tools")
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff used Business Research as advisory context, but "
                    "Chief of Staff -> Business Research Agent is still the durable "
                    "next WorkItem owner."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                ),
                nested_specialist_results=[
                    ChiefNestedSpecialistResult(
                        route_name=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                        tool_name="business_research_analyst_tool",
                        parsed_output_status="text",
                        output_type="advisory_read_plan",
                        summary=(
                            "Advisory context only; durable Business Research must "
                            "still run as a WorkItem graph node."
                        ),
                        validation_status="ok",
                    )
                ],
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "@KNI chief of staff use Business Research Agent as advisory context "
                "for NeuroFlow, then hand off durable source-backed research to "
                "Business Research Agent. Do not send, post, schedule, publish, "
                "create drafts, or write externally."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "requested_agent": "chief_of_staff",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "NeuroFlow",
            },
        ),
        manager_loop=True,
        max_manager_steps=3,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    graph_completion = next(
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    )
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}

    assert captured == {
        "sdk_include_specialist_tools": True,
        "kwarg_include_specialist_tools": True,
    }
    assert outcome.result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_business_research",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert {"chief_of_staff_plan", "company_profile"} <= artifact_types
    assert [step["route"] for step in graph_completion.metadata["steps"]] == [
        WorkItemRoute.CHIEF_OF_STAFF.value,
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
    ]
    assert graph_completion.metadata["send_enabled"] is False
    assert all(
        artifact.metadata.get("send_enabled") is not True
        and artifact.metadata.get("external_writes_enabled") is not True
        for artifact in outcome.result.work_item.artifact_refs
    )


def test_langgraph_uses_structured_chief_durable_handoff_without_prose_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Business Research should own the next canonical WorkItem step "
                    "because the request needs source-backed company review."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                    command_text="Run the selected durable specialist.",
                ),
                durable_handoff=ChiefDurableHandoff(
                    agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                    rationale=(
                        "A company evidence pass should become durable WorkItem state "
                        "before any downstream opportunity or outreach step."
                    ),
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "@KNI chief of staff coordinate source-backed company review for "
                "NeuroFlow, then run the durable research step. Do not send, post, "
                "schedule, publish, create drafts, or write externally."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "requested_agent": "chief_of_staff",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "NeuroFlow",
            },
        ),
        manager_loop=True,
        max_manager_steps=3,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    graph_completion = next(
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    )
    chief_artifact = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == "chief_of_staff_plan"
    )
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}

    assert "Chief of Staff -> Business Research Agent" not in chief_artifact.summary
    assert outcome.result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert {"chief_of_staff_plan", "company_profile"} <= artifact_types
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_business_research",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert [step["route"] for step in graph_completion.metadata["steps"]] == [
        WorkItemRoute.CHIEF_OF_STAFF.value,
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
    ]


def test_langgraph_structured_chief_durable_handoff_respects_advisory_only_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Business Research could be a later owner, but the current "
                    "operator request should remain advisory-only."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                    command_text="Review only for now.",
                ),
                durable_handoff=ChiefDurableHandoff(
                    agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                    rationale=(
                        "This field should not override the operator's advisory-only "
                        "boundary."
                    ),
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "@KNI chief of staff review NeuroFlow as an internal advisory-only "
                "planning note. Do not hand off, delegate, route, or run a downstream "
                "specialist yet."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "requested_agent": "chief_of_staff",
                "target_agent": "chief_of_staff",
                "intent": "internal_review",
                "primary_target": "NeuroFlow",
            },
        ),
        manager_loop=True,
        max_manager_steps=3,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    checkpoint = next(
        event for event in events if event.event_type == "langgraph_orchestration"
    )

    assert outcome.result.route == WorkItemRoute.CHIEF_OF_STAFF
    assert "run_business_research" not in outcome.node_path
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "approval_checkpoint",
    ]
    assert checkpoint.metadata["checkpoint_payload"]["route"] == (
        WorkItemRoute.CHIEF_OF_STAFF.value
    )
    assert checkpoint.metadata["checkpoint_payload"]["send_enabled"] is False
    assert checkpoint.metadata["checkpoint_payload"]["external_writes_enabled"] is False
    assert not any(
        event.event_type == "langgraph_manager_loop_completed" for event in events
    )


def test_langgraph_uses_structured_chief_context_handoff_without_prose_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Stage read-only schema context first, then have the selected "
                    "research owner assess Example Health."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                    command_text="Run the selected durable specialist with context.",
                ),
                durable_handoff=ChiefDurableHandoff(
                    agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                    rationale="Business Research should own the canonical evidence pass.",
                ),
                context_handoffs=[
                    ChiefContextHandoff(
                        agent="airtable_context_agent",
                        before_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                        rationale=(
                            "Read-only Airtable schema context should inform the "
                            "Business Research handoff."
                        ),
                    )
                ],
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "@KNI chief of staff coordinate the best workflow for Example Health's "
                "remote patient monitoring AI validation review. Do not send, post, "
                "schedule, publish, create drafts, or write externally."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "requested_agent": "chief_of_staff",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Example Health",
            },
        ),
        manager_loop=True,
        max_manager_steps=4,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    chief_artifact = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == "chief_of_staff_plan"
    )
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    source_providers = {source.provider for source in outcome.result.work_item.sources}

    assert "Airtable Context Agent" not in chief_artifact.summary
    assert chief_artifact.metadata["context_handoffs"][0]["agent"] == "airtable_context_agent"
    assert outcome.result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert outcome.result.status == WorkItemStatus.DONE
    assert {
        "chief_of_staff_plan",
        "airtable_context_summary",
        "company_profile",
    } <= artifact_types
    assert "airtable_context_agent" in source_providers
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "stage_airtable_context",
        "run_business_research",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert any(
        event.event_type == "context_evidence_staged"
        and event.actor == "airtable_context_agent"
        and event.metadata["downstream_route"] == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
        for event in events
    )


def test_langgraph_uses_structured_chief_feed_and_zotero_context_handoffs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="Preprint on behavioral-health outcomes evidence workflows",
            url="https://doi.org/10.1101/2026.03.07.456789",
            source="medRxiv",
            feed="preprints",
            doi="10.1101/2026.03.07.456789",
            tags=["preprint", "behavioral health", "outcomes evidence"],
            selected=True,
            selection_reason="Relevant preliminary evidence for a research packet.",
            summary="A preprint discusses outcomes-evidence workflow needs.",
        )
    )

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Stage the selected read-only evidence context first, then have "
                    "the selected research owner assess NeuroFlow."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                    command_text="Run the selected durable specialist with context.",
                ),
                durable_handoff=ChiefDurableHandoff(
                    agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                    rationale="Business Research should own the canonical evidence pass.",
                ),
                context_handoffs=[
                    ChiefContextHandoff(
                        agent="preprints_context_agent",
                        before_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                        rationale="Use selected preprint history as preliminary evidence.",
                    ),
                    ChiefContextHandoff(
                        agent="zotero_context_agent",
                        before_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                        rationale="Use saved collection context as read-only evidence.",
                    ),
                ],
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "@KNI chief of staff coordinate the best evidence workflow for "
                "NeuroFlow before the research pass. Do not send, post, schedule, "
                "publish, create drafts, or write externally."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "requested_agent": "chief_of_staff",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "NeuroFlow",
            },
        ),
        manager_loop=True,
        max_manager_steps=4,
    )

    events = store.list_work_item_events(outcome.result.work_item.id)
    chief_artifact = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == "chief_of_staff_plan"
    )
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    source_providers = {source.provider for source in outcome.result.work_item.sources}

    assert "preprints context agent" not in chief_artifact.summary.lower()
    assert "zotero context agent" not in chief_artifact.summary.lower()
    assert [item["agent"] for item in chief_artifact.metadata["context_handoffs"]] == [
        "preprints_context_agent",
        "zotero_context_agent",
    ]
    assert outcome.result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert outcome.result.status == WorkItemStatus.DONE
    assert {
        "chief_of_staff_plan",
        "preprints_context_summary",
        "zotero_context_summary",
        "company_profile",
    } <= artifact_types
    assert {"preprints_context_agent", "zotero_context_agent"} <= source_providers
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "stage_feed_context",
        "stage_zotero_context",
        "run_business_research",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert [
        event.actor
        for event in events
        if event.event_type == "context_evidence_staged"
        and event.actor in {"preprints_context_agent", "zotero_context_agent"}
    ] == ["preprints_context_agent", "zotero_context_agent"]


def test_specific_chief_ask_compares_prior_state_and_backend_graph_context_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_text = (
        "@KNI chief of staff NeuroFlow has been coming up in recent announcements "
        "and saved papers around behavioral-health AI, payer partnership, and "
        "outcomes-evidence signals. Assess whether this is a real KNI advisory "
        "or research opportunity, what source-backed evidence is still missing, "
        "and whether it should stop at an approval checkpoint before any outreach. "
        "Do not draft, send, post, schedule, publish, or write externally."
    )

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Opportunity Scout Agent is the best next owner "
                    "for source-backed opportunity triage before any outreach."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    def seed_store(database_url: str) -> SQLiteStore:
        store = SQLiteStore(database_url)
        store.save_announcement_feed_item(
            AnnouncementFeedItem(
                title="NeuroFlow payer partnership outcomes update",
                url="https://example.org/neuroflow-payer-outcomes",
                source="#announcements",
                feed="rss",
                tags=["behavioral health", "payer partnership", "outcomes evidence"],
                selected=True,
                selection_reason=(
                    "Relevant announcement signal for behavioral-health opportunity triage."
                ),
                summary=(
                    "A behavioral-health AI vendor announced payer partnership and "
                    "outcomes-evidence signals."
                ),
                evidence=[
                    AnnouncementFeedEvidence(
                        kind="article",
                        title="NeuroFlow outcomes source",
                        url="https://example.org/neuroflow-payer-outcomes",
                        snippet=(
                            "The announcement describes payer partnership and "
                            "outcomes-evidence signals."
                        ),
                        source="trafilatura",
                        status="success",
                        char_count=850,
                    )
                ],
            )
        )
        return store

    def run_variant(
        database_name: str,
        *,
        langgraph_env: str | None,
    ) -> tuple[WorkflowRunResult, list[object]]:
        database_url = f"sqlite:///{tmp_path / database_name}"
        store = seed_store(database_url)
        if langgraph_env is None:
            monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
            monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
        else:
            monkeypatch.setenv("KNI_BUSINESS_AGENTS_LANGGRAPH", langgraph_env)
            monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
        result = advance_work_item_manager_loop_with_optional_langgraph(
            WorkflowRunRequest(
                request_text=request_text,
                database_url=database_url,
                save=True,
                live_sdk=True,
                live_search=False,
                manual_request_plan={
                    "source": "slack",
                    "requested_agent": "chief_of_staff",
                    "target_agent": "chief_of_staff",
                    "intent": "internal_review_handoff",
                    "primary_target": "NeuroFlow",
                },
            ),
            max_steps=4,
        )
        return result, store.list_work_item_events(result.work_item.id)

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    prior_result, prior_events = run_variant(
        "chief_neuroflow_prior.sqlite3",
        langgraph_env="false",
    )
    graph_result, graph_events = run_variant(
        "chief_neuroflow_graph.sqlite3",
        langgraph_env=None,
    )

    prior_artifacts = {artifact.artifact_type for artifact in prior_result.work_item.artifact_refs}
    graph_artifacts = {artifact.artifact_type for artifact in graph_result.work_item.artifact_refs}
    graph_event = next(
        event for event in graph_events if event.event_type == "langgraph_orchestration"
    )
    graph_node_path = graph_event.metadata["node_path"]
    graph_context_actors = [
        event.actor for event in graph_events if event.event_type == "context_evidence_staged"
    ]
    prior_quality = langgraph_quality_markers(prior_result, prior_events)
    graph_quality = langgraph_quality_markers(graph_result, graph_events)
    quality_comparison = compare_langgraph_quality(prior_quality, graph_quality)

    assert prior_result.route == graph_result.route == WorkItemRoute.OPPORTUNITY_SCOUT
    assert prior_result.status == WorkItemStatus.BLOCKED
    assert graph_result.status == WorkItemStatus.DONE
    assert prior_result.advanced is False
    assert graph_result.advanced is True
    assert {"chief_of_staff_plan"} <= prior_artifacts
    assert "opportunity" not in prior_artifacts
    expected_graph_artifacts = {
        "chief_of_staff_plan",
        "rss_context_summary",
        "zotero_context_summary",
        "opportunity",
    }
    assert expected_graph_artifacts <= graph_artifacts
    assert not {"rss_context_summary", "zotero_context_summary"} & prior_artifacts
    assert not any(event.event_type.startswith("langgraph_") for event in prior_events)
    assert graph_context_actors == ["rss_context_agent", "zotero_context_agent"]
    assert graph_node_path.index("run_chief_of_staff") < graph_node_path.index(
        "stage_feed_context"
    )
    assert graph_node_path.index("stage_feed_context") < graph_node_path.index(
        "stage_zotero_context"
    )
    assert graph_node_path.index("stage_zotero_context") < graph_node_path.index(
        "run_opportunity_scout"
    )
    assert {"rss_context_agent", "zotero_context_agent"} <= {
        source.provider for source in graph_result.work_item.sources
    }
    assert graph_event.metadata["checkpoint_required"] is False
    assert prior_quality["route"] == graph_quality["route"]
    assert graph_quality["context_evidence_count"] > prior_quality["context_evidence_count"]
    assert graph_quality["durable_stage_count"] > prior_quality["durable_stage_count"]
    assert graph_quality["graph_explainability"] is True
    assert prior_quality["graph_explainability"] is False
    assert graph_quality["side_effect_safe"] is True
    assert prior_quality["side_effect_safe"] is True
    assert prior_quality["status"] == WorkItemStatus.BLOCKED.value
    assert graph_quality["status"] == WorkItemStatus.DONE.value
    assert graph_quality["stage_statuses"].get("opportunity_scout") == "completed"
    assert quality_comparison["same_route"] is True
    assert quality_comparison["same_status"] is False
    assert quality_comparison["status_improved"] is True
    assert quality_comparison["context_evidence_delta"] == 2
    assert quality_comparison["durable_stage_delta"] > 0
    assert quality_comparison["graph_explainability_added"] is True
    assert quality_comparison["side_effect_safe_both"] is True
    assert "status_improved" in quality_comparison["improvement_markers"]
    assert quality_comparison["regression_markers"] == []
    assert quality_comparison["ready_for_live_smoke"] is True
    for result in (prior_result, graph_result):
        for artifact in result.work_item.artifact_refs:
            assert artifact.metadata.get("send_enabled") is not True
            assert artifact.metadata.get("external_writes_enabled") is not True


def test_natural_chief_research_opportunity_sample_outreach_reaches_approval_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_text = (
        "@KNI chief of staff NeuroFlow has been coming up as a "
        "behavioral-health AI company with payer partnership and "
        "outcomes-evidence signals. Do research, assess whether this is "
        "a real KNI advisory/research opportunity, identify what "
        "source-backed evidence is still missing, and decide whether it "
        "should stop at an approval checkpoint before any outreach. If "
        "the evidence supports pursuing it, include a draft-only "
        "Slack-thread sample outreach for review. Do not send email, "
        "create Gmail drafts, post outside this thread, schedule, publish, "
        "or write external systems."
    )

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Business Research Agent is the best next owner "
                    "before opportunity triage and any draft-only Slack-thread outreach."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    def run_variant(
        database_name: str,
        *,
        langgraph_env: str | None,
    ) -> tuple[WorkflowRunResult, list[object]]:
        database_url = f"sqlite:///{tmp_path / database_name}"
        if langgraph_env is None:
            monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
            monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
        else:
            monkeypatch.setenv("KNI_BUSINESS_AGENTS_LANGGRAPH", langgraph_env)
            monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
        result = advance_work_item_manager_loop_with_optional_langgraph(
            WorkflowRunRequest(
                request_text=request_text,
                database_url=database_url,
                save=True,
                live_sdk=True,
                live_search=False,
                manual_request_plan={
                    "source": "slack",
                    "requested_agent": "chief_of_staff",
                    "target_agent": "chief_of_staff",
                    "intent": "internal_review_handoff",
                    "primary_target": "NeuroFlow",
                },
            ),
            max_steps=4,
        )
        return result, SQLiteStore(database_url).list_work_item_events(result.work_item.id)

    open_result, open_events = run_variant("natural_chief_open.sqlite3", langgraph_env=None)
    forced_false_result, forced_false_events = run_variant(
        "natural_chief_forced_false.sqlite3",
        langgraph_env="false",
    )
    forced_true_result, forced_true_events = run_variant(
        "natural_chief_forced_true.sqlite3",
        langgraph_env="true",
    )
    open_graph_event = next(
        event for event in open_events if event.event_type == "langgraph_orchestration"
    )
    forced_true_graph_event = next(
        event for event in forced_true_events if event.event_type == "langgraph_orchestration"
    )
    open_graph_completion = [
        event for event in open_events if event.event_type == "langgraph_manager_loop_completed"
    ]
    forced_true_graph_completion = [
        event
        for event in forced_true_events
        if event.event_type == "langgraph_manager_loop_completed"
    ]
    expected_graph_node_path = [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_business_research",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_opportunity_scout",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_outreach_composer",
        "finalize_step",
        "manager_loop_finalize",
    ]

    for result in (open_result, forced_false_result, forced_true_result):
        artifact_types = {artifact.artifact_type for artifact in result.work_item.artifact_refs}
        assert result.route == WorkItemRoute.OUTREACH_COMPOSER
        assert result.status == WorkItemStatus.DONE
        assert result.advanced is True
        assert result.blockers == []
        assert {"chief_of_staff_plan", "company_profile", "opportunity", "outreach_draft"} <= (
            artifact_types
        )
        outreach_artifact = next(
            artifact
            for artifact in result.work_item.artifact_refs
            if artifact.artifact_type == "outreach_draft"
        )
        assert outreach_artifact.metadata["thread_local_slack_draft"] is True
        assert outreach_artifact.metadata["approval_queue_created"] is False
        assert outreach_artifact.metadata["gmail_draft_created"] is False
        assert all(
            artifact.metadata.get("send_enabled") is not True
            for artifact in result.work_item.artifact_refs
        )
        assert all(
            artifact.metadata.get("external_writes_enabled") is not True
            for artifact in result.work_item.artifact_refs
        )
    assert not any(
        event.event_type == "langgraph_orchestration" for event in forced_false_events
    )
    assert open_graph_event.metadata["node_path"] == expected_graph_node_path
    assert forced_true_graph_event.metadata["node_path"] == expected_graph_node_path
    assert open_graph_event.metadata["checkpoint_required"] is False
    assert forced_true_graph_event.metadata["checkpoint_required"] is False
    assert open_graph_completion
    assert forced_true_graph_completion


def test_backend_selected_manager_loop_uses_graph_for_chief_opportunity_outreach_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Opportunity Scout Agent is the best next owner "
                    "for source-backed opportunity triage before any draft-only outreach."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=(
                "chief of staff agent: Use only sanitized inline context. Harbor "
                "Pediatrics is considering whether Keystone could help review an internal "
                "pediatric behavioral-health referral dashboard before an October pilot. "
                "No PHI is included. If recommending another agent, use Chief of Staff -> "
                "Opportunity Scout Agent notation, then prepare draft-only outreach if the "
                "opportunity path is useful. Do not send, post, publish, schedule, or write "
                "externally."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Harbor Pediatrics",
            },
        ),
        max_steps=4,
    )

    events = SQLiteStore(database_url).list_work_item_events(result.work_item.id)
    graph_event = next(event for event in events if event.event_type == "langgraph_orchestration")
    graph_completion = [
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    ]
    node_path = graph_event.metadata["node_path"]
    artifact_types = {artifact.artifact_type for artifact in result.work_item.artifact_refs}

    assert result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert result.status == WorkItemStatus.BLOCKED
    assert result.advanced is False
    assert graph_event.metadata["checkpoint_required"] is True
    assert {
        "run_chief_of_staff",
        "run_opportunity_scout",
        "run_outreach_composer",
        "approval_checkpoint",
    } <= set(node_path)
    assert node_path.index("run_chief_of_staff") < node_path.index("run_opportunity_scout")
    assert node_path.index("run_opportunity_scout") < node_path.index("run_outreach_composer")
    assert node_path[-1] == "approval_checkpoint"
    assert {"chief_of_staff_plan", "opportunity"} <= artifact_types
    assert [blocker.code for blocker in result.blockers] == [
        "outreach_requires_approved_context"
    ]
    assert "Approve source-backed claims" in graph_event.metadata["checkpoint_reason"]
    assert not graph_completion


def test_langgraph_gmail_multistep_reports_only_the_immediate_blocker(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    prompt = (
        "A Gmail consulting inquiry came in. Triage it, research the company, "
        "create an opportunity record, and draft a response only after approval."
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "heuristic",
                "requested_agent": "orchestrator",
                "target_agent": "gmail_triage",
                "intent": "gmail_triage",
                "primary_target": "consulting inquiry",
            },
        ),
        manager_loop=True,
        max_manager_steps=5,
    )
    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    blocker_codes = {blocker.code for blocker in outcome.result.blockers}
    completion_events = [
        event for event in events if event.event_type == "manager_loop_completed"
    ]
    missing_codes = {
        item["code"] for item in completion_events[-1].metadata["missing_required_stages"]
    }

    assert outcome.result.route == WorkItemRoute.GMAIL_TRIAGE
    assert outcome.result.status == WorkItemStatus.BLOCKED
    assert blocker_codes == {"gmail_context_required"}
    assert missing_codes == set()
    assert any(event.event_type == "langgraph_manager_loop_completed" for event in events)


def test_langgraph_manager_loop_routes_opportunity_to_outreach_gate_when_requested(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "research NeuroFlow, find matching opportunities, and prepare "
                "draft-only outreach. Do not send, post, schedule, or write externally."
            ),
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "llm",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "NeuroFlow",
                "workflow": [
                    "business_research_analyst",
                    "opportunity_scout",
                    "outreach_composer",
                ],
                "requires_durable_state": True,
            },
        ),
        manager_loop=True,
        max_manager_steps=3,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    graph_completion = [
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    ]

    assert outcome.result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert outcome.result.status == WorkItemStatus.BLOCKED
    assert outcome.checkpoint_required is True
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_business_research",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_opportunity_scout",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_outreach_composer",
        "finalize_step",
        "approval_checkpoint",
    ]
    assert [blocker.code for blocker in outcome.result.blockers] == [
        "outreach_requires_approved_context"
    ]
    assert not graph_completion


def test_chief_owned_read_context_plan_runs_chief_after_cli_prefix_is_removed(
    tmp_path: Path,
) -> None:
    request_text = (
        "review today's Gmail, open WorkItems, and current Airtable context, then "
        "recommend my top three actions. Don't change anything."
    )
    plan = infer_manual_request_plan(
        request_text,
        requested_agent="chief_of_staff",
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=request_text,
            database_url=_database_url(tmp_path),
            save=True,
            requested_route=WorkItemRoute.CHIEF_OF_STAFF,
            manual_request_plan=plan.model_dump(mode="json"),
        ),
        manager_loop=True,
        max_manager_steps=4,
    )

    assert outcome.result.route == WorkItemRoute.CHIEF_OF_STAFF
    assert outcome.result.status == WorkItemStatus.DONE
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert [artifact.artifact_type for artifact in outcome.result.artifact_refs] == [
        "chief_of_staff_plan"
    ]
    assert outcome.checkpoint_required is False
    assert outcome.result.blockers == []


def test_natural_opportunity_research_airtable_plan_outreach_uses_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_text = (
        "Find the best opportunity, research it, create an Airtable record plan, and "
        "draft outreach for review without sending."
    )
    plan = infer_manual_request_plan(request_text, requested_agent="orchestrator")

    def fake_advance_opportunity(
        work_item: WorkItem,
        *,
        request: WorkflowRunRequest,
        store: SQLiteStore | None,
    ) -> WorkflowRunResult:
        del request
        source = WorkItemSourceRef(
            source_id="fixture:hj-019:opportunity",
            title="AffectAI evidence collaboration signal",
            url="https://affectai.example/evidence-collaboration",
            source_type="company_page",
            provider="fixture",
            extraction_status="supplied_material",
            source_quality="operator_supplied",
            supported_claim=(
                "AffectAI is seeking an independent behavioral-health AI evidence review."
            ),
            key_facts=[
                "AffectAI is seeking an independent behavioral-health AI evidence review."
            ],
        )
        updated = work_item.model_copy(
            update={
                "target": work_item.target.model_copy(
                    update={
                        "name": "AffectAI Research",
                        "object_type": "company",
                    }
                ),
                "sources": [source],
                "facts": [
                    WorkItemFact(
                        key="opportunity_signal",
                        value=source.supported_claim,
                        confidence=0.9,
                        source_refs=[source],
                        approval_state=ApprovalState.APPROVED_FOR_DRAFTING.value,
                    )
                ],
                "artifact_refs": [
                    WorkItemArtifactRef(
                        artifact_type="opportunity",
                        artifact_id="fixture-hj-019-opportunity",
                        source_agent=WorkItemRoute.OPPORTUNITY_SCOUT.value,
                        approval_state=ApprovalState.APPROVED_FOR_DRAFTING.value,
                        selected=True,
                        title="AffectAI Research evidence-review opportunity",
                        summary=source.supported_claim,
                        metadata={"source_refs": [source.model_dump(mode="json")]},
                    )
                ],
                "last_agent": WorkItemRoute.OPPORTUNITY_SCOUT.value,
                "status": WorkItemStatus.DONE,
                "next_action": None,
            }
        ).touch()
        if store is not None:
            store.save_work_item(updated)
        return WorkflowRunResult(
            work_item=updated,
            route=WorkItemRoute.OPPORTUNITY_SCOUT,
            status=WorkItemStatus.DONE,
            advanced=True,
            human_summary="Selected a source-backed opportunity for research.",
        )

    monkeypatch.setattr(workflow_runner, "_advance_opportunity", fake_advance_opportunity)

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=request_text,
            database_url=_database_url(tmp_path),
            save=True,
            live_sdk=False,
            live_search=False,
            requested_route=WorkItemRoute.OPPORTUNITY_SCOUT,
            manual_request_plan=plan.model_dump(mode="json"),
        ),
        manager_loop=True,
        max_manager_steps=6,
    )

    artifact_types = {
        artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs
    }
    assert "run_business_research" in outcome.node_path, outcome.node_path
    assert outcome.node_path.index("run_opportunity_scout") < outcome.node_path.index(
        "run_business_research"
    )
    assert outcome.node_path.index("run_business_research") < outcome.node_path.index(
        "stage_airtable_context"
    )
    assert outcome.node_path.index("stage_airtable_context") < outcome.node_path.index(
        "run_outreach_composer"
    )
    assert {
        "opportunity",
        "company_profile",
        "airtable_write_plan",
        "outreach_draft",
    } <= artifact_types, (
        outcome.node_path,
        outcome.result.status,
        [blocker.code for blocker in outcome.result.blockers],
        artifact_types,
    )
    assert outcome.result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert outcome.checkpoint_required is True
    assert outcome.node_path[-1] == "approval_checkpoint"
    assert outcome.result.work_item.request_text == request_text
    assert outcome.result.work_item.status == WorkItemStatus.NEEDS_APPROVAL
    assert all(
        artifact.metadata.get("external_writes_enabled") is not True
        and artifact.metadata.get("send_enabled") is not True
        for artifact in outcome.result.work_item.artifact_refs
    )


def test_langgraph_manager_loop_routes_gmail_to_research_to_outreach_gate(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    feedback_events: list[tuple[str, dict[str, object]]] = []

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "gmail triage this sanitized inbound email from Mindful Care, "
                "research Mindful Care, and prepare draft-only outreach. "
                "Email: From: Jordan Lee, Operations at Mindful Care. "
                "Subject: Follow-up on measurement support. Body: Hi Jordan, our team "
                "is reviewing measurement-based care workflows and may need advisory "
                "help on evaluation design. Could you let me know if this is relevant "
                "for Keystone? Do not send, create Gmail drafts, post, schedule, or "
                "write externally."
            ),
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "heuristic",
                "requested_agent": "orchestrator",
                "target_agent": "gmail_triage",
                "intent": "gmail_triage",
                "primary_target": "Mindful Care",
                "target_type": "gmail_thread",
                "task_objective": "gmail_triage",
            },
        ),
        manager_loop=True,
        max_manager_steps=3,
        feedback_callback=lambda event_type, payload: feedback_events.append(
            (event_type, payload)
        ),
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    graph_completion = [
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    ]
    graph_event = next(event for event in events if event.event_type == "langgraph_orchestration")
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    completed_payload = feedback_events[-1][1]
    feedback_checkpoint = completed_payload["checkpoint_payload"]
    completion_review = completed_payload["graph_completion_review"]

    assert outcome.result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert outcome.result.status == WorkItemStatus.NEEDS_APPROVAL
    assert outcome.checkpoint_required is True
    assert [event_type for event_type, _payload in feedback_events] == [
        "manager_loop_graph_started",
        "manager_loop_completed",
    ]
    assert completed_payload["checkpoint_required"] is True
    assert feedback_checkpoint["schema"] == "keystone.langgraph.approval_checkpoint.v1"
    assert feedback_checkpoint["work_item_id"] == outcome.result.work_item.id
    assert feedback_checkpoint["route"] == WorkItemRoute.OUTREACH_COMPOSER.value
    assert feedback_checkpoint["target"]["name"] == "Mindful Care"
    assert feedback_checkpoint["send_enabled"] is False
    assert feedback_checkpoint["external_writes_enabled"] is False
    assert graph_event.metadata["graph_completion_review"]["checkpoint_required"] is True
    assert completion_review["checkpoint_required"] is True
    assert {
        (stage["stage"], stage["status"])
        for stage in completion_review["requested_stages"]
    } >= {
        ("gmail_triage", "completed"),
        ("business_research", "completed"),
        ("outreach_composer", "completed"),
        ("approval_checkpoint", "completed"),
    }
    assert any(
        "Approval checkpoint:" in line
        for line in completion_review["renderer_summary_lines"]
    )
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_gmail_triage",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_business_research",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_outreach_composer",
        "finalize_step",
        "approval_checkpoint",
    ]
    assert outcome.result.work_item.target.name == "Mindful Care"
    assert outcome.result.work_item.target.metadata["gmail_research_target"] == "Mindful Care"
    assert {"gmail_triage_report", "company_profile"} <= artifact_types
    assert outcome.result.blockers == []
    assert not graph_completion


def test_langgraph_supplied_material_packet_survives_research_to_draft_handoffs(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    fixture_path = Path(__file__).parent / "fixtures/graph_research_to_draft_source_bundle.json"

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "Gmail triage the supplied inbound request, research Northstar Behavioral "
                "Analytics using only the provided source bundle, and prepare a draft-only "
                "reply for review. Do not use live search. Do not send, create a Gmail "
                "draft, post, schedule, or write externally."
            ),
            context_file_path=str(fixture_path),
            database_url=database_url,
            save=True,
            live_sdk=False,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "requested_agent": "orchestrator",
                "target_agent": "gmail_triage",
                "intent": "gmail_triage",
                "primary_target": "Northstar Behavioral Analytics",
                "target_type": "gmail_thread",
                "task_objective": "gmail_triage",
            },
        ),
        manager_loop=True,
        max_manager_steps=3,
    )

    work_item = outcome.result.work_item
    source_ids = {source.source_id for source in work_item.sources}
    approved_fact_keys = {
        fact.key for fact in work_item.facts if fact.approval_state == "approved_for_drafting"
    }
    artifact_types = {artifact.artifact_type for artifact in work_item.artifact_refs}

    assert outcome.result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert outcome.checkpoint_required is True
    assert outcome.node_path[-1] == "approval_checkpoint"
    assert outcome.node_path.index("run_gmail_triage") < outcome.node_path.index(
        "run_business_research"
    )
    assert outcome.node_path.index("run_business_research") < outcome.node_path.index(
        "run_outreach_composer"
    )
    assert {
        "fixture:graph-source:company-brief",
        "fixture:graph-source:inbound-email",
    } <= source_ids
    assert {"company_product_focus", "inbound_request"} <= approved_fact_keys
    assert work_item.target.metadata["thread_id"] == "thread-northstar-001"
    assert work_item.target.metadata["message_id"] == "message-northstar-001"
    assert work_item.target.name == "Northstar Behavioral Analytics"
    assert work_item.target.metadata["gmail_research_target"] == (
        "Northstar Behavioral Analytics"
    )
    assert "fixture:example" not in source_ids
    assert {"gmail_triage_report", "company_profile"} <= artifact_types
    assert outcome.checkpoint_payload["send_enabled"] is False
    assert outcome.checkpoint_payload["external_writes_enabled"] is False
    assert all(
        artifact.metadata.get("send_enabled") is not True
        and artifact.metadata.get("external_writes_enabled") is not True
        for artifact in work_item.artifact_refs
    )


def test_langgraph_blocks_mismatched_source_bundle_before_specialists(
    tmp_path: Path,
) -> None:
    fixture_path = Path(__file__).parent / "fixtures/graph_research_to_draft_source_bundle.json"
    request_text = (
        "Research NeuroFlow as a behavioral-health AI opportunity with payer partnership "
        "and outcomes-evidence signals, then have Opportunity Scout assess whether this is "
        "a real KNI advisory/research opportunity. Stop before outreach."
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=request_text,
            context_file_path=str(fixture_path),
            database_url=_database_url(tmp_path),
            save=True,
            live_sdk=False,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "target_agent": "opportunity_scout",
                "intent": "opportunity_search",
                "primary_target": "NeuroFlow",
                "target_type": "topic",
                "task_objective": "opportunity_discovery",
            },
        ),
        manager_loop=True,
        max_manager_steps=3,
    )

    work_item = outcome.result.work_item
    assert work_item.target.name == "NeuroFlow"
    assert work_item.sources == []
    assert work_item.facts == []
    assert outcome.result.advanced is False
    assert "source_bundle_target_mismatch" in {
        blocker.code for blocker in outcome.result.blockers
    }
    assert any(
        "stopped before using a source bundle for a different target" in note
        for note in outcome.result.audit_notes
    )


def test_langgraph_storage_events_render_final_run_report(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "gmail triage this sanitized inbound email from Mindful Care, "
                "research Mindful Care, and prepare draft-only outreach. Email: "
                "From: Jordan Lee, Operations at Mindful Care. Subject: Follow-up "
                "on measurement support. Body: Hi Jordan, our team is reviewing "
                "measurement-based care workflows and may need advisory help. Do not "
                "send, create Gmail drafts, post, schedule, publish, or write externally."
            ),
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "heuristic",
                "requested_agent": "orchestrator",
                "target_agent": "gmail_triage",
                "intent": "gmail_triage",
                "primary_target": "Mindful Care",
                "target_type": "gmail_thread",
                "task_objective": "gmail_triage",
            },
        ),
        manager_loop=True,
        max_manager_steps=3,
    )
    store = SQLiteStore(database_url)
    stored = store.get_work_item(outcome.result.work_item.id)
    assert stored is not None

    report = render_work_item_graph_report(
        stored,
        store.list_work_item_events(stored.id),
    )

    assert report.startswith("Keystone LangGraph Run Report")
    assert f"WorkItem: {stored.id}" in report
    assert "Graph Path:" in report
    assert "run_gmail_triage" in report
    assert "run_business_research" in report
    assert "run_outreach_composer" in report
    assert "approval_checkpoint" in report
    assert "Specialist Steps:" in report
    assert "gmail_triage -> business_research_analyst -> outreach_composer" in report
    assert "Approval Checkpoint:" in report
    assert "Required: yes" in report
    assert "Artifacts:" in report
    assert "gmail_triage_report" in report
    assert "company_profile" in report
    assert "outreach_requires_approved_context" not in report
    assert "Sources:" in report
    assert "fixture://" in report
    assert "send enabled: no" in report
    assert "external writes enabled: no" in report


def test_langgraph_manager_loop_runs_chief_managed_business_research_edge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Business Research Agent is the best next owner "
                    "for a source-backed internal review of Example Health's remote "
                    "patient monitoring AI validation workflow."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "chief of staff agent: Use only sanitized inline context. Example "
                "Health asked whether Keystone could help review its remote patient "
                "monitoring AI validation workflow before a July pilot proposal. No PHI "
                "is included. Return the best next owner and what remains blocked. If "
                "recommending another agent, use Chief of Staff -> Business Research "
                "Agent notation. Do not send, post, publish, schedule, or write externally."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Example Health",
            },
        ),
        manager_loop=True,
        max_manager_steps=3,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    graph_completion = next(
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    )
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}

    assert outcome.result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert outcome.result.advanced is True
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_business_research",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert {"chief_of_staff_plan", "company_profile"} <= artifact_types
    assert [step["route"] for step in graph_completion.metadata["steps"]] == [
        WorkItemRoute.CHIEF_OF_STAFF.value,
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
    ]
    assert graph_completion.metadata["send_enabled"] is False
    assert "Example Health" in outcome.result.human_summary


def test_langgraph_chief_gmail_triage_can_handoff_to_business_research(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Gmail Triage Agent is the best next owner "
                    "to review the sanitized inbound email before downstream company "
                    "research decides whether this is relevant for Keystone."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="gmail-triage",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "chief of staff review this sanitized inbound email, use Gmail Triage "
                "first, then research the company for KNI advisory relevance. Email: "
                "From: Jordan Lee, Operations at Mindful Care. Subject: Follow-up on "
                "measurement support. Body: Hi Jordan, our team is reviewing "
                "measurement-based care workflows and may need advisory help on "
                "evaluation design. Do not send, create Gmail drafts, post, schedule, "
                "publish, create files, or write externally."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Mindful Care",
            },
        ),
        manager_loop=True,
        max_manager_steps=4,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    graph_completion = next(
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    )
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    completion_review = graph_completion.metadata["graph_completion_review"]

    assert outcome.result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert outcome.result.status == WorkItemStatus.DONE
    assert outcome.checkpoint_required is False
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_gmail_triage",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_business_research",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert outcome.result.work_item.target.name == "Mindful Care"
    assert outcome.result.work_item.target.metadata["gmail_research_target"] == "Mindful Care"
    assert {"chief_of_staff_plan", "gmail_triage_report", "company_profile"} <= artifact_types
    assert [step["route"] for step in graph_completion.metadata["steps"]] == [
        WorkItemRoute.CHIEF_OF_STAFF.value,
        WorkItemRoute.GMAIL_TRIAGE.value,
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
    ]
    assert {
        (stage["stage"], stage["status"])
        for stage in completion_review["requested_stages"]
    } >= {
        ("chief_of_staff", "completed"),
        ("gmail_triage", "completed"),
        ("business_research", "completed"),
    }
    assert graph_completion.metadata["send_enabled"] is False
    assert graph_completion.metadata["external_writes_enabled"] is False


def test_langgraph_chief_research_opportunity_no_draft_stops_before_outreach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Business Research Agent is the best next owner "
                    "before Opportunity Scout assesses the KNI advisory/research fit."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "@KNI chief of staff NeuroFlow has payer partnership and outcomes "
                "evidence signals. Do research, assess whether this is a real KNI "
                "advisory/research opportunity, and identify what source-backed "
                "evidence is still missing. Do not draft outreach, send email, "
                "create Gmail drafts, post, schedule, publish, or write externally."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "requested_agent": "chief_of_staff",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "NeuroFlow",
                "target_type": "company",
            },
        ),
        manager_loop=True,
        max_manager_steps=4,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    graph_completion = next(
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    )
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}

    assert outcome.result.route == WorkItemRoute.OPPORTUNITY_SCOUT
    assert outcome.result.status == WorkItemStatus.DONE
    assert outcome.checkpoint_required is False
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_business_research",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_opportunity_scout",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert {"chief_of_staff_plan", "company_profile", "opportunity"} <= artifact_types
    assert "outreach_draft" not in artifact_types
    assert [step["route"] for step in graph_completion.metadata["steps"]] == [
        WorkItemRoute.CHIEF_OF_STAFF.value,
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        WorkItemRoute.OPPORTUNITY_SCOUT.value,
    ]
    completion_review = graph_completion.metadata["graph_completion_review"]
    stage_statuses = {
        stage["stage"]: stage["status"] for stage in completion_review["requested_stages"]
    }
    assert stage_statuses["chief_of_staff"] == "completed"
    assert stage_statuses["business_research"] == "completed"
    assert stage_statuses["opportunity_scout"] == "completed"
    assert stage_statuses["outreach_composer"] == "intentionally_skipped"
    assert completion_review["send_enabled"] is False
    assert completion_review["external_writes_enabled"] is False
    assert graph_completion.metadata["send_enabled"] is False
    assert all(
        artifact.metadata.get("send_enabled") is not True
        and artifact.metadata.get("external_writes_enabled") is not True
        for artifact in outcome.result.work_item.artifact_refs
    )


def test_langgraph_runs_chief_before_selected_context_agents_and_business_research(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="RPM AI validation announcement",
            url="https://example.org/rpm-ai-validation",
            source="#announcements",
            feed="rss",
            tags=["remote patient monitoring", "ai", "validation"],
            selected=True,
            selection_reason="Relevant context for a validation-workflow review.",
            summary=(
                "A remote patient monitoring vendor announced an AI validation "
                "workflow."
            ),
            evidence=[
                AnnouncementFeedEvidence(
                    kind="article",
                    title="RPM validation source",
                    url="https://example.org/rpm-ai-validation",
                    snippet="The announcement describes an AI validation workflow.",
                    source="trafilatura",
                    status="success",
                    char_count=700,
                )
            ],
        )
    )

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Business Research Agent is the best next owner "
                    "after RSS and Zotero context agents are staged."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "chief of staff coordinate RSS context agent announcement history "
                "and Zotero context agent handoff before Business Research reviews "
                "Example Health remote patient monitoring AI validation workflow. If "
                "recommending another agent, use Chief of Staff -> Business Research "
                "Agent notation. Do not send, post, publish, schedule, run live "
                "research, or write externally."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Example Health",
            },
        ),
        manager_loop=True,
        max_manager_steps=4,
    )

    events = store.list_work_item_events(outcome.result.work_item.id)
    graph_completion = next(
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    )
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    source_providers = {source.provider for source in outcome.result.work_item.sources}
    context_events = [
        event for event in events if event.event_type == "context_evidence_staged"
    ]

    assert outcome.result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert outcome.result.advanced is True
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "stage_feed_context",
        "stage_zotero_context",
        "run_business_research",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert {
        "chief_of_staff_plan",
        "rss_context_summary",
        "zotero_context_summary",
        "company_profile",
    } <= artifact_types
    assert {"rss_context_agent", "zotero_context_agent"} <= source_providers
    assert [event.actor for event in context_events] == [
        "rss_context_agent",
        "zotero_context_agent",
    ]
    assert [step["route"] for step in graph_completion.metadata["steps"]] == [
        WorkItemRoute.CHIEF_OF_STAFF.value,
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
    ]
    assert graph_completion.metadata["send_enabled"] is False


def test_langgraph_runs_chief_to_business_research_to_workspace_approval_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Business Research Agent is the best next owner "
                    "before Google Workspace Context plans the internal evidence packet."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "chief of staff coordinate Business Research for Example Health's "
                "remote patient monitoring AI validation workflow, then use Google "
                "Workspace Context agent to plan the internal evidence packet in Drive, "
                "Docs, and Sheets for approval review. If recommending another agent, "
                "use Chief of Staff -> Business Research Agent notation. Do not create "
                "files, share links, update sheets, send, post, publish, schedule, run "
                "live research, or write externally."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Example Health",
            },
        ),
        manager_loop=True,
        max_manager_steps=4,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    source_providers = {source.provider for source in outcome.result.work_item.sources}
    workspace_artifact = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == "google_workspace_artifact_plan"
    )
    graph_completion = [
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    ]

    assert outcome.result.route == WorkItemRoute.CHIEF_OF_STAFF
    assert outcome.result.status == WorkItemStatus.NEEDS_APPROVAL
    assert outcome.checkpoint_required is True
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_business_research",
        "finalize_step",
        "stage_google_workspace_context",
        "approval_checkpoint",
    ]
    assert {
        "chief_of_staff_plan",
        "company_profile",
        "google_workspace_artifact_plan",
    } <= artifact_types
    assert "google_workspace_context_agent" in source_providers
    assert workspace_artifact.metadata["write_plan"]["target_system"] == "google_workspace"
    assert workspace_artifact.metadata["write_plan"]["live_write_allowed_for_specialist"] is False
    assert workspace_artifact.metadata["external_writes_enabled"] is False
    assert workspace_artifact.metadata["send_enabled"] is False
    assert any(
        gate.scope == "google_workspace_artifact_plan"
        and gate.required
        and gate.state == "pending"
        for gate in outcome.result.work_item.approval_gates
    )
    assert any(
        event.event_type == "context_evidence_staged"
        and event.actor == "google_workspace_context_agent"
        for event in events
    )
    assert not graph_completion


def test_langgraph_runs_chief_to_business_research_to_airtable_approval_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Business Research Agent is the best next owner "
                    "before Airtable Context prepares the internal write plan."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "chief of staff coordinate Business Research for Example Health's "
                "remote patient monitoring AI validation workflow, then use Airtable "
                "Context agent to plan the internal Airtable record update for approval "
                "review. If recommending another agent, use Chief of Staff -> Business "
                "Research Agent notation. Do not create records, update fields, upload "
                "attachments, send, post, publish, schedule, run live research, or write "
                "externally."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Example Health",
            },
        ),
        manager_loop=True,
        max_manager_steps=4,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    source_providers = {source.provider for source in outcome.result.work_item.sources}
    airtable_artifact = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == "airtable_write_plan"
    )
    graph_completion = [
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    ]

    assert outcome.result.route == WorkItemRoute.CHIEF_OF_STAFF
    assert outcome.result.status == WorkItemStatus.NEEDS_APPROVAL
    assert outcome.checkpoint_required is True
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_business_research",
        "finalize_step",
        "stage_airtable_context",
        "approval_checkpoint",
    ]
    assert {
        "chief_of_staff_plan",
        "company_profile",
        "airtable_write_plan",
    } <= artifact_types
    assert "airtable_context_agent" in source_providers
    assert airtable_artifact.metadata["write_plan"]["target_system"] == "airtable"
    assert airtable_artifact.metadata["write_plan"]["live_write_allowed_for_specialist"] is False
    assert airtable_artifact.metadata["external_writes_enabled"] is False
    assert airtable_artifact.metadata["send_enabled"] is False
    assert any(
        gate.scope == "airtable_write_plan"
        and gate.required
        and gate.state == "pending"
        for gate in outcome.result.work_item.approval_gates
    )
    assert any(
        event.event_type == "context_evidence_staged"
        and event.actor == "airtable_context_agent"
        for event in events
    )
    assert not graph_completion


def test_langgraph_runs_chief_selected_airtable_context_before_business_research(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Airtable Context Agent should inspect schema context "
                    "first, then Chief of Staff -> Business Research Agent should assess "
                    "Example Health."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "chief of staff coordinate Airtable Context agent schema context before "
                "Business Research assesses Example Health's remote patient monitoring "
                "AI validation workflow. Use Airtable only as read-only context for the "
                "specialist handoff. No sends, posts, scheduling, publication, or external "
                "system mutation."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Example Health",
            },
        ),
        manager_loop=True,
        max_manager_steps=4,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    source_providers = {source.provider for source in outcome.result.work_item.sources}
    airtable_artifact = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == "airtable_context_summary"
    )
    graph_completion = next(
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    )

    assert outcome.result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert outcome.result.status == WorkItemStatus.DONE
    assert outcome.checkpoint_required is False
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "stage_airtable_context",
        "run_business_research",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert {
        "chief_of_staff_plan",
        "airtable_context_summary",
        "company_profile",
    } <= artifact_types
    assert "airtable_write_plan" not in artifact_types
    assert "airtable_context_agent" in source_providers
    assert airtable_artifact.approval_state == "approved_for_research"
    assert airtable_artifact.metadata["live_reads_enabled"] is False
    assert airtable_artifact.metadata["external_writes_enabled"] is False
    assert any(
        event.event_type == "context_evidence_staged"
        and event.actor == "airtable_context_agent"
        and event.metadata["downstream_route"] == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
        for event in events
    )
    assert [step["route"] for step in graph_completion.metadata["steps"]] == [
        WorkItemRoute.CHIEF_OF_STAFF.value,
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
    ]
    assert graph_completion.metadata["send_enabled"] is False
    assert (
        graph_completion.metadata["stop_reason"]
        == "stopped after one specialist step; no multi-step workflow was requested"
    )


def test_langgraph_runs_chief_selected_workspace_context_before_business_research(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Google Workspace Context Agent should inspect "
                    "artifact context first, then Chief of Staff -> Business Research "
                    "Agent should assess Example Health."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "chief of staff coordinate Google Workspace Context agent artifact "
                "context before Business Research assesses Example Health's remote "
                "patient monitoring AI validation workflow. Use Google Drive and Docs "
                "only as read-only context for the specialist handoff. No file creation, "
                "sharing, sends, posts, scheduling, publication, or external system mutation."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Example Health",
            },
        ),
        manager_loop=True,
        max_manager_steps=4,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    source_providers = {source.provider for source in outcome.result.work_item.sources}
    workspace_artifact = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == "google_workspace_context_summary"
    )
    graph_completion = next(
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    )

    assert outcome.result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert outcome.result.status == WorkItemStatus.DONE
    assert outcome.checkpoint_required is False
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "stage_google_workspace_context",
        "run_business_research",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert {
        "chief_of_staff_plan",
        "google_workspace_context_summary",
        "company_profile",
    } <= artifact_types
    assert "google_workspace_artifact_plan" not in artifact_types
    assert "google_workspace_context_agent" in source_providers
    assert workspace_artifact.approval_state == "approved_for_research"
    assert workspace_artifact.metadata["live_reads_enabled"] is False
    assert workspace_artifact.metadata["external_writes_enabled"] is False
    assert any(
        event.event_type == "context_evidence_staged"
        and event.actor == "google_workspace_context_agent"
        and event.metadata["downstream_route"] == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
        for event in events
    )
    assert [step["route"] for step in graph_completion.metadata["steps"]] == [
        WorkItemRoute.CHIEF_OF_STAFF.value,
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
    ]
    assert graph_completion.metadata["send_enabled"] is False


@pytest.mark.parametrize(
    (
        "context_request",
        "expected_node",
        "expected_summary_artifact",
        "expected_plan_artifact",
        "expected_provider",
        "gate_scope",
        "target_system",
    ),
    [
        (
            (
                "Airtable Context agent schema context before Business Research assesses "
                "Example Health's remote patient monitoring AI validation workflow, then "
                "after Business Research use Airtable Context agent to plan the internal "
                "Airtable record update for approval review. Use the pre-research "
                "Airtable context as read-only handoff material."
            ),
            "stage_airtable_context",
            "airtable_context_summary",
            "airtable_write_plan",
            "airtable_context_agent",
            "airtable_write_plan",
            "airtable",
        ),
        (
            (
                "Google Workspace Context agent artifact context before Business Research "
                "assesses Example Health's remote patient monitoring AI validation "
                "workflow, then after Business Research use Google Workspace Context "
                "agent to plan where the evidence packet should live in Drive, Docs, and "
                "Sheets for approval review. Use the pre-research Workspace context as "
                "read-only handoff material."
            ),
            "stage_google_workspace_context",
            "google_workspace_context_summary",
            "google_workspace_artifact_plan",
            "google_workspace_context_agent",
            "google_workspace_artifact_plan",
            "google_workspace",
        ),
    ],
)
def test_langgraph_preserves_pre_context_and_post_specialist_approval_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    context_request: str,
    expected_node: str,
    expected_summary_artifact: str,
    expected_plan_artifact: str,
    expected_provider: str,
    gate_scope: str,
    target_system: str,
) -> None:
    database_url = _database_url(tmp_path)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Business Research Agent is the best next owner "
                    "before the requested context-agent approval plan is staged."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                f"chief of staff coordinate {context_request} Do not create records, "
                "update fields, upload attachments, create files, share links, update "
                "sheets, send, post, publish, schedule, run live research, or write "
                "externally."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Example Health",
            },
        ),
        manager_loop=True,
        max_manager_steps=4,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    source_providers = {source.provider for source in outcome.result.work_item.sources}
    plan_artifact = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == expected_plan_artifact
    )
    graph_completion = [
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    ]

    assert outcome.result.route == WorkItemRoute.CHIEF_OF_STAFF
    assert outcome.result.status == WorkItemStatus.NEEDS_APPROVAL
    assert outcome.checkpoint_required is True
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        expected_node,
        "run_business_research",
        "finalize_step",
        expected_node,
        "approval_checkpoint",
    ]
    assert {
        "chief_of_staff_plan",
        "company_profile",
        expected_summary_artifact,
        expected_plan_artifact,
    } <= artifact_types
    assert expected_provider in source_providers
    assert plan_artifact.metadata["write_plan"]["target_system"] == target_system
    assert plan_artifact.metadata["write_plan"]["live_write_allowed_for_specialist"] is False
    assert plan_artifact.metadata["external_writes_enabled"] is False
    assert plan_artifact.metadata["send_enabled"] is False
    assert any(
        gate.scope == gate_scope
        and gate.required
        and gate.state == "pending"
        for gate in outcome.result.work_item.approval_gates
    )
    assert [
        event.actor
        for event in events
        if event.event_type == "context_evidence_staged"
    ].count(expected_provider) == 2
    assert not graph_completion


def test_langgraph_stages_multiple_read_only_context_lanes_before_specialist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Business Research Agent is the best next owner "
                    "after Airtable and Workspace read-only context lanes are staged."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "chief of staff coordinate Airtable Context agent schema context and "
                "Google Workspace Context agent artifact context before Business "
                "Research assesses Example Health's remote patient monitoring AI "
                "validation workflow. Use both context agents only as read-only handoff material. "
                "Do not update Airtable, upload attachments, create files, share links, "
                "update sheets, send, post, publish, schedule, or write externally."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Example Health",
            },
        ),
        manager_loop=True,
        max_manager_steps=4,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    source_providers = {source.provider for source in outcome.result.work_item.sources}
    graph_completion = next(
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    )

    assert outcome.result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert outcome.result.status == WorkItemStatus.DONE
    assert outcome.checkpoint_required is False
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "stage_airtable_context",
        "stage_google_workspace_context",
        "run_business_research",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert {
        "chief_of_staff_plan",
        "airtable_context_summary",
        "google_workspace_context_summary",
        "company_profile",
    } <= artifact_types
    assert "airtable_write_plan" not in artifact_types
    assert "google_workspace_artifact_plan" not in artifact_types
    assert {"airtable_context_agent", "google_workspace_context_agent"} <= source_providers
    assert [step["route"] for step in graph_completion.metadata["steps"]] == [
        WorkItemRoute.CHIEF_OF_STAFF.value,
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
    ]
    assert graph_completion.metadata["send_enabled"] is False


def test_langgraph_preserves_weekly_slack_gmail_completed_run_packet_for_chief(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    weekly_packet = build_weekly_ops_source_bundle(
        WeeklyOpsAssemblyInput.model_validate(
            {
                "window": {
                    "time_min": "2026-07-04T00:00:00-04:00",
                    "time_max": "2026-07-11T00:00:00-04:00",
                    "packet_date": "2026-07-10",
                },
                "slack": [
                    {
                        "message_ts": "100.1",
                        "summary": "The weekly packet contract is ready for review.",
                    }
                ],
                "gmail": [
                    {
                        "thread_id": "thread-1",
                        "subject": "Packet follow-up",
                        "summary": "An internal decision is pending.",
                    }
                ],
                "completed_runs": [
                    {
                        "run_id": "run-1",
                        "agent_name": "chief_of_staff",
                        "completed_at": "2026-07-10T21:58:00-04:00",
                        "outcome_summary": "Weekly packet contract validation completed.",
                        "packet_role": "primary",
                        "relevance_reason": "Directly supports the requested packet.",
                    }
                ],
                "calendar": [
                    {
                        "event_id": "event-focus",
                        "title": "Client review",
                        "start": "2026-07-06T10:00:00-04:00",
                    },
                    {
                        "event_id": "event-recurring",
                        "title": "Weekly operations sync",
                        "start": "2026-07-08T09:00:00-04:00",
                        "is_recurring": True,
                        "recurring_event_id": "series-1",
                    },
                ],
            }
        )
    )
    source_ids = {source["source_id"] for source in weekly_packet["sources"]}

    chief_output = ChiefOfStaffResult(
        mode="llm",
        summary=(
            "Weekly operations summary: Slack and Gmail activity are available; "
            "the relevant completed Chief run is the primary operational evidence. "
            "One-time calendar events are focus areas; recurring events are noted "
            "briefly. Source message bodies remain private."
        ),
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type="portfolio-review",
            target_channel="current thread",
        ),
        recommended_actions=[
            "Carry forward the action explicitly linked to the completed run.",
            "Use selected Slack or Gmail threads for any deeper follow-up.",
        ],
        approval_required=True,
        audit_notes=["No post, send, or external write was requested."],
    )

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=chief_output,
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "plan_chief_of_staff_request",
        lambda *_args, **_kwargs: chief_output,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "Chief of Staff: prepare a weekly ops summary from the supplied seven-day "
                "Slack and Gmail activity plus relevant completed agent runs and Calendar. "
                "Identify focus areas, workstreams, and next actions. Keep operational "
                "health and metadata succinct. Do not post, send, or write."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            external_context=weekly_packet,
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "portfolio_review",
                "primary_target": "Weekly operations review",
                "task_objective": "portfolio_summary",
            },
        ),
        manager_loop=True,
        max_manager_steps=2,
    )

    final_source_ids = {source.source_id for source in outcome.result.work_item.sources}
    context_source_ids = {
        str(source.get("source_id") or "")
        for source in outcome.result.context_pack.get("source_refs", [])
    }

    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert outcome.result.route == WorkItemRoute.CHIEF_OF_STAFF
    assert outcome.result.status == WorkItemStatus.DONE
    assert source_ids <= final_source_ids
    assert source_ids <= context_source_ids
    assert "relevant completed Chief run" in outcome.result.human_summary
    assert "One-time calendar events are focus areas" in outcome.result.human_summary
    assert "recurring events are noted briefly" in outcome.result.human_summary
    assert outcome.checkpoint_required is False
    assert not outcome.result.blockers


@pytest.mark.parametrize(
    (
        "context_request",
        "expected_node",
        "expected_artifact",
        "excluded_artifact",
        "expected_provider",
        "handoff_key",
    ),
    [
        (
            (
                "Airtable Context agent schema context before Opportunity Scout assesses "
                "Example Health's remote patient monitoring AI validation workflow. Use "
                "Airtable only as read-only context for the specialist handoff. Live SDK "
                "is approved only for this bounded read-only smoke if the backend would "
                "normally use it; live web search is not approved. Use local/dry-run "
                "retrieval where possible. Do not update Airtable or upload attachments."
            ),
            "stage_airtable_context",
            "airtable_context_summary",
            "airtable_write_plan",
            "airtable_context_agent",
            "airtable_context_handoff",
        ),
        (
            (
                "Google Workspace Context agent artifact context before Opportunity Scout "
                "assesses Example Health's remote patient monitoring AI validation workflow. "
                "Use Google Drive and Docs only as read-only context for the specialist handoff."
            ),
            "stage_google_workspace_context",
            "google_workspace_context_summary",
            "google_workspace_artifact_plan",
            "google_workspace_context_agent",
            "google_workspace_context_handoff",
        ),
    ],
)
def test_langgraph_runs_chief_selected_business_context_before_opportunity_scout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    context_request: str,
    expected_node: str,
    expected_artifact: str,
    excluded_artifact: str,
    expected_provider: str,
    handoff_key: str,
) -> None:
    database_url = _database_url(tmp_path)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Opportunity Scout Agent is the best next owner "
                    "to assess Example Health after the selected read-only context lane "
                    "is staged."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                f"chief of staff coordinate {context_request} No file creation, "
                "sharing, sends, posts, scheduling, publication, or external system "
                "mutation."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Example Health",
            },
        ),
        manager_loop=True,
        max_manager_steps=4,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    source_providers = {source.provider for source in outcome.result.work_item.sources}
    assert expected_artifact in artifact_types, {
        "artifact_types": sorted(artifact_types),
        "node_path": outcome.node_path,
        "route": outcome.result.route.value,
        "status": outcome.result.status.value,
        "source_providers": sorted(source_providers),
    }
    context_artifact = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == expected_artifact
    )
    graph_completion = next(
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    )

    assert outcome.result.route == WorkItemRoute.OPPORTUNITY_SCOUT
    assert outcome.result.status == WorkItemStatus.DONE
    assert outcome.checkpoint_required is False
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        expected_node,
        "run_opportunity_scout",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert {"chief_of_staff_plan", expected_artifact, "opportunity"} <= artifact_types
    assert excluded_artifact not in artifact_types
    assert expected_provider in source_providers
    assert context_artifact.approval_state == "approved_for_research"
    assert context_artifact.metadata["live_reads_enabled"] is False
    assert context_artifact.metadata["external_writes_enabled"] is False
    assert (
        outcome.result.work_item.target.metadata[handoff_key]["agent_name"]
        == expected_provider
    )
    assert any(
        event.event_type == "context_evidence_staged"
        and event.actor == expected_provider
        and event.metadata["downstream_route"] == WorkItemRoute.OPPORTUNITY_SCOUT.value
        for event in events
    )
    assert [step["route"] for step in graph_completion.metadata["steps"]] == [
        WorkItemRoute.CHIEF_OF_STAFF.value,
        WorkItemRoute.OPPORTUNITY_SCOUT.value,
    ]
    assert graph_completion.metadata["send_enabled"] is False


def test_langgraph_stages_concrete_quarterly_finance_context_before_bd_priority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    finance_source_id = "airtable:finance_tax_tracker:Q3-2026"

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff reviewed the supplied current-quarter finance "
                    "guardrails and selected Opportunity Scout as the next owner to "
                    "rank the two supplied business-development options."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "Chief of Staff: use Airtable Context agent current-quarter finance "
                "evidence before choosing our next business-development priority from "
                "Option Alpha and Option Beta. Have Opportunity Scout rank the supplied "
                "options. Read-only; do not search, write, send, post, or publish."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            external_context={
                "schema": "keystone.work_item.source_bundle.v1",
                "source": "bounded_finance_and_opportunity_packet",
                "supplied_material_only": True,
                "target": {
                    "name": "Option Alpha vs Option Beta",
                    "object_type": "opportunity_comparison",
                },
                "sources": [
                    {
                        "source_id": finance_source_id,
                        "title": "Current-quarter finance guardrails",
                        "source_type": "live_airtable_aggregate",
                        "provider": "airtable_context_agent",
                        "extraction_status": "aggregated",
                        "source_quality": "provider_aggregate",
                        "supported_claim": (
                            "The current quarter supports one bounded BD experiment; "
                            "avoid a high fixed-cost commitment."
                        ),
                        "key_facts": [
                            "Current-quarter income and expense tables were read.",
                            "One uncategorized expense requires human review.",
                        ],
                    },
                    {
                        "source_id": "supplied:option-alpha",
                        "title": "Option Alpha",
                        "source_type": "supplied_opportunity",
                        "provider": "operator",
                        "supported_claim": "Low fixed cost and fast validation cycle.",
                    },
                    {
                        "source_id": "supplied:option-beta",
                        "title": "Option Beta",
                        "source_type": "supplied_opportunity",
                        "provider": "operator",
                        "supported_claim": "Higher fixed cost and longer validation cycle.",
                    },
                ],
            },
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Option Alpha vs Option Beta",
            },
        ),
        manager_loop=True,
        max_manager_steps=4,
    )

    finance_source = next(
        source
        for source in outcome.result.work_item.sources
        if source.source_id == finance_source_id
    )
    assert any(
        artifact.artifact_type == "airtable_context_summary"
        for artifact in outcome.result.work_item.artifact_refs
    ), {
        "node_path": outcome.node_path,
        "route": outcome.result.route.value,
        "status": outcome.result.status.value,
        "artifact_types": [
            artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs
        ],
    }
    airtable_artifact = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == "airtable_context_summary"
    )
    context_source_ids = {
        str(source.get("source_id") or "")
        for source in outcome.result.context_pack.get("source_refs", [])
    }

    assert outcome.node_path.index("run_chief_of_staff") < outcome.node_path.index(
        "stage_airtable_context"
    )
    assert outcome.node_path.index("stage_airtable_context") < outcome.node_path.index(
        "run_opportunity_scout"
    )
    assert outcome.result.route == WorkItemRoute.OPPORTUNITY_SCOUT
    assert outcome.result.status == WorkItemStatus.DONE, {
        "blockers": [blocker.code for blocker in outcome.result.blockers],
        "next_action": (
            outcome.result.next_action.action if outcome.result.next_action else ""
        ),
    }
    assert finance_source.provider == "airtable_context_agent"
    assert airtable_artifact.metadata["mode"] == "provider_source_handoff"
    assert airtable_artifact.metadata["provider_evidence"] is True
    assert airtable_artifact.metadata["live_reads_enabled"] is True
    assert finance_source_id in context_source_ids
    assert {"supplied:option-alpha", "supplied:option-beta"} <= context_source_ids
    assert airtable_artifact.metadata["external_writes_enabled"] is False


def test_langgraph_slack_airtable_read_only_smoke_does_not_stage_write_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Airtable Context Agent should stage read-only "
                    "context, then Chief of Staff -> Opportunity Scout Agent should "
                    "assess Example Health."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                ),
                durable_handoff=ChiefDurableHandoff(
                    agent=WorkItemRoute.OPPORTUNITY_SCOUT.value,
                    rationale="The typed plan selected Opportunity Scout next.",
                ),
                context_handoffs=[
                    ChiefContextHandoff(
                        agent="airtable_context_agent",
                        before_agent=WorkItemRoute.OPPORTUNITY_SCOUT.value,
                        rationale="Stage schema context before opportunity review.",
                    )
                ],
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    request_text = (
        "LangGraph smoke 3: Chief of Staff coordinate Airtable Context agent schema "
        "context before Opportunity Scout assesses Example Health as an internal "
        "opportunity-direction planning note. Use Airtable only as read-only context "
        "for the specialist handoff. Live SDK is approved only for this bounded "
        "read-only smoke if the backend would normally use it; live web search is "
        "not approved. Use local/dry-run retrieval where possible. Do not send email, "
        "create drafts, post elsewhere, publish, schedule, create files, update "
        "Airtable, upload attachments, or write external systems."
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=request_text,
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            external_context={
                "schema": "keystone.slack.query_prompt.v1",
                "source": "slack_reusable_query_prompt",
                "slack_query_prompt": {
                    "schema": "keystone.slack.query_prompt.v1",
                    "kind": "opportunity_search",
                    "target_route": "chief_of_staff",
                    "cost_profile": "slack_research_deep",
                },
            },
            manual_request_plan={
                "source": "llm",
                "target_agent": "chief_of_staff",
                "workflow": ["airtable_context_agent", "opportunity_scout"],
                "intent": "slack_operations",
                "requires_durable_state": True,
                "primary_target": (
                    "LangGraph smoke 3: Chief of Staff coordinate Airtable Context "
                    "agent schema context before Opportunity Scout assesses Example Health"
                ),
                "constraints": [
                    "read-only",
                    "dry-run-safe",
                    "no live web search",
                    "local retrieval preferred",
                    "Airtable context only for handoff",
                    "no external side effects",
                ],
            },
        ),
        manager_loop=True,
        max_manager_steps=4,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    graph_completion = next(
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    )

    assert outcome.result.route == WorkItemRoute.OPPORTUNITY_SCOUT
    assert outcome.result.status == WorkItemStatus.DONE
    assert outcome.checkpoint_required is False
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "stage_airtable_context",
        "run_opportunity_scout",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert {"chief_of_staff_plan", "airtable_context_summary", "opportunity"} <= artifact_types
    assert "airtable_write_plan" not in artifact_types
    assert [step["route"] for step in graph_completion.metadata["steps"]] == [
        WorkItemRoute.CHIEF_OF_STAFF.value,
        WorkItemRoute.OPPORTUNITY_SCOUT.value,
    ]
    assert graph_completion.metadata["send_enabled"] is False


@pytest.mark.parametrize(
    (
        "context_request",
        "expected_node",
        "expected_artifact",
        "excluded_artifact",
        "expected_provider",
        "handoff_key",
    ),
    [
        (
            (
                "Airtable Context agent schema context before Gmail Triage reviews this "
                "sanitized inbound email from Cedar Valley Rehab. Use Airtable only as "
                "read-only context for the specialist handoff."
            ),
            "stage_airtable_context",
            "airtable_context_summary",
            "airtable_write_plan",
            "airtable_context_agent",
            "airtable_context_handoff",
        ),
        (
            (
                "Google Workspace Context agent artifact context before Gmail Triage "
                "reviews this sanitized inbound email from Cedar Valley Rehab. Use Google "
                "Drive and Docs only as read-only context for the specialist handoff."
            ),
            "stage_google_workspace_context",
            "google_workspace_context_summary",
            "google_workspace_artifact_plan",
            "google_workspace_context_agent",
            "google_workspace_context_handoff",
        ),
    ],
)
def test_langgraph_runs_chief_selected_business_context_before_gmail_triage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    context_request: str,
    expected_node: str,
    expected_artifact: str,
    excluded_artifact: str,
    expected_provider: str,
    handoff_key: str,
) -> None:
    database_url = _database_url(tmp_path)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Gmail Triage Agent is the best next owner "
                    "to review the sanitized inbound email after the selected "
                    "read-only context lane is staged."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="gmail-triage",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                f"chief of staff coordinate {context_request} Email: From: Jordan Lee, "
                "Operations at Cedar Valley Rehab. Subject: Measurement workflow support. "
                "Body: Hi Jordan, our team is reviewing measurement-based care workflows "
                "and may need advisory help. No sends, Gmail drafts, posts, scheduling, "
                "publication, file creation, sharing, updates, uploads, or external "
                "system mutation."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Cedar Valley Rehab",
            },
        ),
        manager_loop=True,
        max_manager_steps=4,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    source_providers = {source.provider for source in outcome.result.work_item.sources}
    context_artifact = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == expected_artifact
    )
    graph_completion = next(
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    )

    assert outcome.result.route == WorkItemRoute.GMAIL_TRIAGE
    assert outcome.result.status == WorkItemStatus.DONE
    assert outcome.checkpoint_required is False
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        expected_node,
        "run_gmail_triage",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert {"chief_of_staff_plan", expected_artifact, "gmail_triage_report"} <= artifact_types
    assert excluded_artifact not in artifact_types
    assert expected_provider in source_providers
    assert context_artifact.approval_state == "approved_for_research"
    assert context_artifact.metadata["live_reads_enabled"] is False
    assert context_artifact.metadata["external_writes_enabled"] is False
    assert (
        outcome.result.work_item.target.metadata[handoff_key]["agent_name"]
        == expected_provider
    )
    assert any(
        event.event_type == "context_evidence_staged"
        and event.actor == expected_provider
        and event.metadata["downstream_route"] == WorkItemRoute.GMAIL_TRIAGE.value
        for event in events
    )
    assert [step["route"] for step in graph_completion.metadata["steps"]] == [
        WorkItemRoute.CHIEF_OF_STAFF.value,
        WorkItemRoute.GMAIL_TRIAGE.value,
    ]
    assert graph_completion.metadata["send_enabled"] is False


@pytest.mark.parametrize(
    (
        "context_request",
        "expected_node",
        "expected_artifact",
        "excluded_artifact",
        "expected_provider",
        "handoff_key",
    ),
    [
        (
            (
                "Airtable Context agent schema context before Gmail Triage reviews this "
                "sanitized inbound email. Use Airtable only as read-only context for "
                "the specialist handoff, then research the sender organization."
            ),
            "stage_airtable_context",
            "airtable_context_summary",
            "airtable_write_plan",
            "airtable_context_agent",
            "airtable_context_handoff",
        ),
        (
            (
                "Google Workspace Context agent artifact context before Gmail Triage "
                "reviews this sanitized inbound email. Use Google Drive and Docs only "
                "as read-only context for the specialist handoff, then research the "
                "sender organization."
            ),
            "stage_google_workspace_context",
            "google_workspace_context_summary",
            "google_workspace_artifact_plan",
            "google_workspace_context_agent",
            "google_workspace_context_handoff",
        ),
    ],
)
def test_langgraph_chief_context_backed_gmail_can_handoff_to_business_research(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    context_request: str,
    expected_node: str,
    expected_artifact: str,
    excluded_artifact: str,
    expected_provider: str,
    handoff_key: str,
) -> None:
    database_url = _database_url(tmp_path)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Gmail Triage Agent is the best next owner "
                    "after the selected read-only context lane is staged; downstream "
                    "Business Research should review the sender organization if the "
                    "email context identifies a relevant target."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="gmail-triage",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                f"chief of staff coordinate {context_request} Email: From: Jordan Lee, "
                "Operations at Cedar Valley Rehab. Subject: Measurement workflow support. "
                "Body: Hi Jordan, our team is reviewing measurement-based care workflows "
                "and may need advisory help. Use the email triage context to research "
                "the company for KNI advisory relevance. No sends, Gmail drafts, posts, "
                "scheduling, publication, file creation, sharing, updates, uploads, or "
                "external system mutation."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Cedar Valley Rehab",
            },
        ),
        manager_loop=True,
        max_manager_steps=5,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    source_providers = {source.provider for source in outcome.result.work_item.sources}
    graph_completion = next(
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    )
    context_artifact = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == expected_artifact
    )

    assert outcome.result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert outcome.result.status == WorkItemStatus.DONE
    assert outcome.checkpoint_required is False
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        expected_node,
        "run_gmail_triage",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_business_research",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert {
        "chief_of_staff_plan",
        expected_artifact,
        "gmail_triage_report",
        "company_profile",
    } <= artifact_types
    assert excluded_artifact not in artifact_types
    assert expected_provider in source_providers
    assert context_artifact.approval_state == "approved_for_research"
    assert context_artifact.metadata["live_reads_enabled"] is False
    assert context_artifact.metadata["external_writes_enabled"] is False
    assert outcome.result.work_item.target.name == "Cedar Valley Rehab"
    assert (
        outcome.result.work_item.target.metadata[handoff_key]["agent_name"]
        == expected_provider
    )
    assert any(
        event.event_type == "context_evidence_staged"
        and event.actor == expected_provider
        and event.metadata["downstream_route"] == WorkItemRoute.GMAIL_TRIAGE.value
        for event in events
    )
    assert [step["route"] for step in graph_completion.metadata["steps"]] == [
        WorkItemRoute.CHIEF_OF_STAFF.value,
        WorkItemRoute.GMAIL_TRIAGE.value,
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
    ]
    assert graph_completion.metadata["send_enabled"] is False
    assert graph_completion.metadata["external_writes_enabled"] is False


def test_langgraph_manager_loop_runs_chief_to_opportunity_to_outreach_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Chief of Staff -> Opportunity Scout Agent is the best next owner "
                    "for source-backed opportunity triage before any draft-only outreach."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                ),
                approval_required=True,
                audit_notes=[],
            ),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "chief of staff agent: Use only sanitized inline context. Harbor "
                "Pediatrics is considering whether Keystone could help review an internal "
                "pediatric behavioral-health referral dashboard before an October pilot. "
                "No PHI is included. If recommending another agent, use Chief of Staff -> "
                "Opportunity Scout Agent notation, then prepare draft-only outreach if the "
                "opportunity path is useful. Do not send, post, publish, schedule, or write "
                "externally."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan={
                "source": "test",
                "target_agent": "chief_of_staff",
                "intent": "internal_review_handoff",
                "primary_target": "Harbor Pediatrics",
            },
        ),
        manager_loop=True,
        max_manager_steps=4,
    )

    events = SQLiteStore(database_url).list_work_item_events(outcome.result.work_item.id)
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    graph_completion = [
        event for event in events if event.event_type == "langgraph_manager_loop_completed"
    ]

    assert outcome.result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert outcome.result.status == WorkItemStatus.BLOCKED
    assert outcome.result.advanced is False
    assert outcome.checkpoint_required is True
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_opportunity_scout",
        "finalize_step",
        "manager_loop_continue",
        "prepare_work_item",
        "run_outreach_composer",
        "finalize_step",
        "approval_checkpoint",
    ]
    assert {"chief_of_staff_plan", "opportunity"} <= artifact_types
    assert [blocker.code for blocker in outcome.result.blockers] == [
        "outreach_requires_approved_context"
    ]
    assert "Approve source-backed claims" in outcome.checkpoint_reason
    assert not graph_completion


def test_langgraph_stages_zotero_context_before_business_research(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "use Zotero context agent to build evidence for foundational depression "
                "reviews, then research NeuroFlow for an internal evidence packet. "
                "Do not send, publish, schedule, or write externally."
            ),
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "heuristic",
                "requested_agent": "orchestrator",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "NeuroFlow",
                "target_type": "company",
                "task_objective": "source_research",
            },
        ),
        manager_loop=True,
        max_manager_steps=2,
    )

    store = SQLiteStore(database_url)
    events = store.list_work_item_events(outcome.result.work_item.id)
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    source_providers = {source.provider for source in outcome.result.work_item.sources}
    source_refs = outcome.result.context_pack.get("source_refs", [])

    assert outcome.result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "stage_zotero_context",
        "run_business_research",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert "zotero_context_summary" in artifact_types
    assert "company_profile" in artifact_types
    assert "zotero_context_agent" in source_providers
    assert any(
        str(item.get("provider") or "") == "zotero_context_agent" for item in source_refs
    )
    assert (
        outcome.result.work_item.target.metadata["zotero_context_handoff"]["agent_name"]
        == "zotero_context_agent"
    )
    assert any(event.event_type == "context_evidence_staged" for event in events)
    assert not any(
        artifact.metadata.get("external_writes_enabled")
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.source_agent == "zotero_context_agent"
    )


def test_langgraph_preserves_concrete_zotero_item_before_business_research(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    item_key = "ITEM-LATEST-1"
    source_id = f"zotero:item:{item_key}"

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "Use Zotero context agent to stage the latest Zotero article before "
                "Business Research summarizes "
                "NeuroFlow, and explain which takeaways changed or stayed unchanged. "
                "Do not write, send, publish, or search the web."
            ),
            database_url=database_url,
            save=True,
            external_context={
                "schema": "keystone.work_item.source_bundle.v1",
                "source": "live_zotero_read",
                "supplied_material_only": True,
                "target": {"name": "NeuroFlow", "object_type": "company"},
                "sources": [
                    {
                        "source_id": source_id,
                        "title": "Recent health-data interoperability article",
                        "url": "https://example.org/article",
                        "source_type": "live_zotero:journalArticle",
                        "provider": "zotero_context_agent",
                        "extraction_status": "metadata_only",
                        "source_quality": "provider_metadata",
                        "supported_claim": (
                            "The explicitly sorted Zotero read selected this exact item."
                        ),
                        "key_facts": [
                            "The item was the newest journalArticle by dateAdded descending.",
                            "No abstract was available; company claims cannot be inferred from it.",
                        ],
                    }
                ],
            },
            manual_request_plan={
                "source": "heuristic",
                "requested_agent": "orchestrator",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "NeuroFlow",
                "target_type": "company",
                "task_objective": "source_research",
            },
        ),
        manager_loop=True,
        max_manager_steps=2,
    )

    zotero_artifact = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == "zotero_context_summary"
    )
    source_ids = {source.source_id for source in outcome.result.work_item.sources}

    assert outcome.node_path.index("stage_zotero_context") < outcome.node_path.index(
        "run_business_research"
    )
    assert source_id in source_ids
    assert zotero_artifact.metadata["mode"] == "provider_source_handoff"
    assert zotero_artifact.metadata["provider_evidence"] is True
    assert zotero_artifact.metadata["live_reads_enabled"] is True
    assert zotero_artifact.metadata["source_refs"][0]["source_id"] == source_id
    assert (
        outcome.result.work_item.target.metadata["zotero_context_handoff"][
            "provider_evidence"
        ]
        is True
    )
    assert not zotero_artifact.metadata["external_writes_enabled"]


def test_langgraph_zotero_acquisition_uses_direct_provider_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, bool] = {}

    def fake_latest_zotero_metadata() -> dict[str, object]:
        captured["called"] = True
        return {
            "status": "success",
            "provider_read": True,
            "selection_rule": "first_nonempty_abstract_in_provider_order",
            "provider_order": {
                "sort": "dateAdded",
                "direction": "desc",
                "top_level_only": True,
                "item_type": "journalArticle",
            },
            "require_abstract": True,
            "item_count": 1,
            "selected_item_title": "Provider-backed article",
            "selected_item_has_abstract": True,
            "selected_item_date_added": "2026-07-12T12:00:00Z",
            "items": [
                {
                    "key": "ITEM-LIVE-1",
                    "data": {
                        "title": "Provider-backed article",
                        "itemType": "journalArticle",
                        "dateAdded": "2026-07-12T12:00:00Z",
                        "abstractNote": "Provider-backed stored abstract.",
                        "DOI": "10.1000/synthetic",
                        "url": "https://example.test/article",
                    }
                }
            ],
        }

    monkeypatch.setattr(
        langgraph_workflow,
        "read_latest_zotero_journal_abstract_metadata",
        fake_latest_zotero_metadata,
    )

    source, receipt = langgraph_workflow._read_zotero_ordered_abstract_source(
        "Use Zotero to select the most recently added journal article with a stored "
        "abstract."
    )

    assert captured == {"called": True}
    assert source is not None
    assert source.source_id == "zotero:item:ITEM-LIVE-1"
    assert source.evidence_excerpt == "Provider-backed stored abstract."
    assert receipt["provider_read"] is True
    assert receipt["selection_rule"] == "first_nonempty_abstract_in_provider_order"
    assert "items" not in receipt


def test_langgraph_stripped_chief_advisory_context_stays_chief_owned(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "use Airtable Context as an advisory specialist to identify metadata "
                "for Airtable base alias eval_tracker and table Eval tracker: case id, "
                "agent, promptfoo status, Slack run id, human reviewer, missing evidence, "
                "next follow-up, and analysis inclusion. Return a read-only tracker-field "
                "plan with risks and unresolved record-identity questions. Do not create, "
                "update, mark complete, or write Airtable records."
            ),
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "heuristic",
                "requested_agent": "orchestrator",
                "target_agent": "chief_of_staff",
                "intent": "route_request",
                "primary_target": "eval tracker field advisory",
            },
        ),
        manager_loop=True,
        max_manager_steps=1,
    )

    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}

    assert outcome.result.route == WorkItemRoute.CHIEF_OF_STAFF
    assert outcome.result.status == WorkItemStatus.DONE
    assert outcome.checkpoint_required is False
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "run_chief_of_staff",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert "chief_of_staff_plan" in artifact_types
    assert "airtable_write_plan" not in artifact_types
    assert "Chief of Staff Airtable tracker-field plan" in outcome.result.human_summary
    assert "Record identity" in outcome.result.human_summary
    assert "Airtable write" in outcome.result.human_summary


def test_langgraph_stages_rss_context_before_opportunity_scout(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="Behavioral health AI partnership announcement",
            url="https://example.org/behavioral-ai-partnership",
            source="#announcements",
            feed="rss",
            tags=["behavioral health", "ai", "partnership"],
            selected=True,
            selection_reason="Relevant partnership signal for Keystone opportunity scouting.",
            summary=(
                "A behavioral health AI vendor announced a provider partnership around "
                "measurement workflows."
            ),
            evidence=[
                AnnouncementFeedEvidence(
                    kind="article",
                    title="Partnership source",
                    url="https://example.org/behavioral-ai-partnership",
                    snippet="The announcement describes a behavioral health AI partnership.",
                    source="trafilatura",
                    status="success",
                    char_count=900,
                )
            ],
        )
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "use RSS context agent announcement history as source-provided signal, "
                "then scout behavioral health AI partnership opportunities. Use only "
                "source-provided context; do not run live research, send, post, publish, "
                "schedule, or write externally."
            ),
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "heuristic",
                "requested_agent": "orchestrator",
                "target_agent": "rss_context_agent",
                "intent": "context_lookup",
                "primary_target": "behavioral health AI partnerships",
            },
        ),
        manager_loop=True,
        max_manager_steps=2,
    )

    events = store.list_work_item_events(outcome.result.work_item.id)
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    rss_artifact = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == "rss_context_summary"
    )
    rss_sources = [
        source
        for source in outcome.result.work_item.sources
        if source.provider == "rss_context_agent"
    ]

    assert outcome.result.route == WorkItemRoute.OPPORTUNITY_SCOUT
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "stage_feed_context",
        "run_opportunity_scout",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert {"rss_context_summary", "opportunity"} <= artifact_types
    assert rss_artifact.metadata["item_count"] == 1
    assert rss_artifact.metadata["recommended_downstream_route"] == (
        WorkItemRoute.OPPORTUNITY_SCOUT.value
    )
    assert rss_sources[0].url == "https://example.org/behavioral-ai-partnership"
    assert rss_sources[0].extraction_status == "article_extracted"
    assert (
        outcome.result.work_item.target.metadata["rss_context_handoff"]["agent_name"]
        == "rss_context_agent"
    )
    assert any(
        event.event_type == "context_evidence_staged"
        and event.actor == "rss_context_agent"
        for event in events
    )
    assert not rss_artifact.metadata["external_writes_enabled"]
    assert not rss_artifact.metadata["send_enabled"]


def test_langgraph_stages_workspace_plan_after_rss_backed_opportunity_scan(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="Behavioral health AI partnership announcement",
            url="https://example.org/behavioral-ai-partnership",
            source="#announcements",
            feed="rss",
            tags=["behavioral health", "ai", "partnership"],
            selected=True,
            selection_reason=(
                "Relevant partnership signal for opportunity packet planning."
            ),
            summary=(
                "A behavioral health AI vendor announced a provider partnership around "
                "measurement workflows."
            ),
            evidence=[
                AnnouncementFeedEvidence(
                    kind="article",
                    title="Partnership source",
                    url="https://example.org/behavioral-ai-partnership",
                    snippet="The announcement describes a behavioral health AI partnership.",
                    source="trafilatura",
                    status="success",
                    char_count=900,
                )
            ],
        )
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "use RSS context agent announcement history as source-provided signal, "
                "then scout behavioral health AI partnership opportunities. After the "
                "opportunity scan, use Google Workspace Context agent to plan where the "
                "opportunity packet should live in Drive, Docs, and Sheets for approval "
                "review. Use only source-provided context; do not run live research, "
                "create files, share links, update sheets, send, post, publish, "
                "schedule, or write externally."
            ),
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "heuristic",
                "requested_agent": "orchestrator",
                "target_agent": "rss_context_agent",
                "intent": "context_lookup",
                "primary_target": "behavioral health AI partnerships",
            },
        ),
        manager_loop=True,
        max_manager_steps=3,
    )

    events = store.list_work_item_events(outcome.result.work_item.id)
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    source_providers = {source.provider for source in outcome.result.work_item.sources}
    workspace_artifact = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == "google_workspace_artifact_plan"
    )
    opportunity_artifacts = [
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == "opportunity"
    ]

    assert outcome.result.route == WorkItemRoute.CHIEF_OF_STAFF
    assert outcome.result.status == WorkItemStatus.NEEDS_APPROVAL
    assert outcome.checkpoint_required is True
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "stage_feed_context",
        "run_opportunity_scout",
        "finalize_step",
        "stage_google_workspace_context",
        "approval_checkpoint",
    ]
    assert {
        "rss_context_summary",
        "opportunity",
        "google_workspace_artifact_plan",
    } <= artifact_types
    assert {"rss_context_agent", "google_workspace_context_agent"} <= source_providers
    assert opportunity_artifacts
    assert workspace_artifact.metadata["write_plan"]["target_system"] == "google_workspace"
    assert workspace_artifact.metadata["write_plan"]["live_write_allowed_for_specialist"] is False
    assert workspace_artifact.metadata["external_writes_enabled"] is False
    assert workspace_artifact.metadata["send_enabled"] is False
    assert any(
        gate.scope == "google_workspace_artifact_plan"
        and gate.required
        and gate.state == "pending"
        for gate in outcome.result.work_item.approval_gates
    )
    assert [
        event.actor
        for event in events
        if event.event_type == "context_evidence_staged"
    ].count("google_workspace_context_agent") == 1


def test_langgraph_stages_preprints_context_before_business_research(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="Digital phenotyping depression preprint",
            url="https://doi.org/10.1101/2026.02.03.123456",
            source="medRxiv",
            feed="preprints",
            doi="10.1101/2026.02.03.123456",
            tags=["preprint", "depression", "digital phenotyping"],
            selected=True,
            selection_reason=(
                "Useful preliminary evidence for depression measurement workflows."
            ),
            summary=(
                "A preprint evaluates passive-sensing signals for depression symptom "
                "monitoring."
            ),
        )
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "use preprints context agent history as preliminary evidence, then "
                "research an internal evidence packet on depression digital phenotyping. "
                "Do not send, publish, schedule, or write externally."
            ),
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "heuristic",
                "requested_agent": "orchestrator",
                "target_agent": "preprints_context_agent",
                "intent": "context_lookup",
                "primary_target": "depression digital phenotyping",
            },
        ),
        manager_loop=True,
        max_manager_steps=2,
    )

    events = store.list_work_item_events(outcome.result.work_item.id)
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    preprints_artifact = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == "preprints_context_summary"
    )
    preprint_sources = [
        source
        for source in outcome.result.work_item.sources
        if source.provider == "preprints_context_agent"
    ]
    source_refs = outcome.result.context_pack.get("source_refs", [])

    assert outcome.result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "stage_feed_context",
        "run_business_research",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert {"preprints_context_summary", "company_profile"} <= artifact_types
    assert preprints_artifact.metadata["item_count"] == 1
    assert preprints_artifact.metadata["recommended_downstream_route"] == (
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    )
    assert preprint_sources[0].url == "https://doi.org/10.1101/2026.02.03.123456"
    assert preprint_sources[0].extraction_status == "historical_summary_only"
    assert any(
        str(item.get("provider") or "") == "preprints_context_agent" for item in source_refs
    )
    assert (
        outcome.result.work_item.target.metadata["preprints_context_handoff"]["agent_name"]
        == "preprints_context_agent"
    )
    assert any(
        event.event_type == "context_evidence_staged"
        and event.actor == "preprints_context_agent"
        for event in events
    )
    assert not preprints_artifact.metadata["external_writes_enabled"]
    assert not preprints_artifact.metadata["send_enabled"]


def test_langgraph_stages_feed_and_zotero_context_before_business_research(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    runtime_calls: list[str] = []
    original_runtime = RequestRuntime.from_workflow_request.__func__

    def tracked_runtime(cls, request):
        runtime_calls.append(request.database_url or "")
        return original_runtime(cls, request)

    monkeypatch.setattr(
        RequestRuntime,
        "from_workflow_request",
        classmethod(tracked_runtime),
    )
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="Preprint on depression evidence workflows",
            url="https://doi.org/10.1101/2026.02.04.234567",
            source="medRxiv",
            feed="preprints",
            doi="10.1101/2026.02.04.234567",
            tags=["preprint", "depression", "evidence"],
            selected=True,
            selection_reason="Relevant preliminary evidence for an internal packet.",
            summary=(
                "A preprint reviews evidence workflow considerations for depression "
                "measurement."
            ),
        )
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "use preprints context agent history and Zotero context agent handoff "
                "to build an internal evidence packet for NeuroFlow, then research "
                "NeuroFlow. Do not "
                "send, publish, schedule, or write externally."
            ),
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "heuristic",
                "requested_agent": "orchestrator",
                "target_agent": "preprints_context_agent",
                "intent": "context_lookup",
                "primary_target": "NeuroFlow",
            },
        ),
        manager_loop=True,
        max_manager_steps=2,
    )

    events = store.list_work_item_events(outcome.result.work_item.id)
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    source_providers = {source.provider for source in outcome.result.work_item.sources}
    source_refs = outcome.result.context_pack.get("source_refs", [])
    context_events = [
        event for event in events if event.event_type == "context_evidence_staged"
    ]

    assert outcome.result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "stage_feed_context",
        "stage_zotero_context",
        "run_business_research",
        "finalize_step",
        "manager_loop_finalize",
    ]
    assert {"preprints_context_summary", "zotero_context_summary", "company_profile"} <= (
        artifact_types
    )
    assert {"preprints_context_agent", "zotero_context_agent"} <= source_providers
    assert {
        str(item.get("provider") or "")
        for item in source_refs
        if str(item.get("provider") or "")
    } >= {"preprints_context_agent", "zotero_context_agent"}
    assert (
        outcome.result.work_item.target.metadata["preprints_context_handoff"][
            "agent_name"
        ]
        == "preprints_context_agent"
    )
    assert (
        outcome.result.work_item.target.metadata["zotero_context_handoff"]["agent_name"]
        == "zotero_context_agent"
    )
    assert {event.actor for event in context_events} >= {
        "preprints_context_agent",
        "zotero_context_agent",
    }
    assert not any(
        artifact.metadata.get("external_writes_enabled")
        or artifact.metadata.get("send_enabled")
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.source_agent in {"preprints_context_agent", "zotero_context_agent"}
    )
    assert runtime_calls == [database_url]


def test_langgraph_stages_google_workspace_artifact_plan_before_approval_checkpoint(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "use Google Workspace Context agent to plan where an internal evidence "
                "packet should live in Drive, Docs, and Sheets. Return only an artifact "
                "plan for review. Do not create files, share links, update sheets, send, "
                "publish, schedule, or write externally."
            ),
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "heuristic",
                "requested_agent": "orchestrator",
                "target_agent": "google_workspace_context_agent",
                "intent": "business_system_context",
                "primary_target": "internal evidence packet",
            },
        ),
        manager_loop=True,
        max_manager_steps=2,
    )

    store = SQLiteStore(database_url)
    events = store.list_work_item_events(outcome.result.work_item.id)
    artifact = next(
        item
        for item in outcome.result.work_item.artifact_refs
        if item.artifact_type == "google_workspace_artifact_plan"
    )
    source_refs = outcome.result.context_pack.get("source_refs", [])

    assert outcome.result.route == WorkItemRoute.CHIEF_OF_STAFF
    assert outcome.result.status == WorkItemStatus.NEEDS_APPROVAL
    assert outcome.checkpoint_required is True
    assert "Workspace artifact plan" in outcome.checkpoint_reason
    assert outcome.checkpoint_payload is not None
    assert outcome.checkpoint_payload["schema"] == "keystone.langgraph.approval_checkpoint.v1"
    assert outcome.checkpoint_payload["work_item_id"] == outcome.result.work_item.id
    assert outcome.checkpoint_payload["route"] == WorkItemRoute.CHIEF_OF_STAFF.value
    assert outcome.checkpoint_payload["status"] == WorkItemStatus.NEEDS_APPROVAL.value
    assert outcome.checkpoint_payload["send_enabled"] is False
    assert outcome.checkpoint_payload["external_writes_enabled"] is False
    assert any(
        gate["scope"] == "google_workspace_artifact_plan"
        and gate["required"] is True
        for gate in outcome.checkpoint_payload["approval_gates"]
    )
    assert {"google_workspace_artifact_plan"} <= {
        artifact["artifact_type"] for artifact in outcome.checkpoint_payload["artifact_refs"]
    }
    assert any(
        source["provider"] == "google_workspace_context_agent"
        for source in outcome.checkpoint_payload["source_refs"]
    )
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "stage_google_workspace_context",
        "approval_checkpoint",
    ]
    assert artifact.source_agent == "google_workspace_context_agent"
    assert artifact.approval_state == "needs_approval"
    assert artifact.metadata["write_plan"]["target_system"] == "google_workspace"
    assert artifact.metadata["write_plan"]["live_write_allowed_for_specialist"] is False
    assert artifact.metadata["external_writes_enabled"] is False
    assert artifact.metadata["send_enabled"] is False
    assert artifact.metadata["live_reads_enabled"] is False
    assert (
        outcome.result.next_action is not None
        and outcome.result.next_action.requires_approval is True
    )
    assert any(
        gate.scope == "google_workspace_artifact_plan"
        and gate.required
        and gate.state == "pending"
        for gate in outcome.result.work_item.approval_gates
    )
    assert any(
        str(item.get("provider") or "") == "google_workspace_context_agent"
        for item in source_refs
    )
    assert (
        outcome.result.work_item.target.metadata["google_workspace_context_handoff"][
            "agent_name"
        ]
        == "google_workspace_context_agent"
    )
    assert any(
        event.event_type == "context_evidence_staged"
        and event.actor == "google_workspace_context_agent"
        for event in events
    )


def test_langgraph_stages_workspace_plan_after_context_backed_research(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="Preprint on depression evidence packet workflows",
            url="https://doi.org/10.1101/2026.02.05.345678",
            source="medRxiv",
            feed="preprints",
            doi="10.1101/2026.02.05.345678",
            tags=["preprint", "depression", "evidence packet"],
            selected=True,
            selection_reason="Relevant preliminary evidence for an internal packet.",
            summary="A preprint discusses evidence-packet workflow needs.",
        )
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "use preprints context agent history and Zotero context agent handoff, "
                "then research NeuroFlow for an internal evidence packet. After research, "
                "use Google Workspace Context agent to plan where the packet should live "
                "in Drive, Docs, and Sheets for approval review. Do not create files, "
                "share links, update sheets, send, publish, schedule, or write externally."
            ),
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "heuristic",
                "requested_agent": "orchestrator",
                "target_agent": "preprints_context_agent",
                "intent": "context_lookup",
                "primary_target": "NeuroFlow",
            },
        ),
        manager_loop=True,
        max_manager_steps=3,
    )

    events = store.list_work_item_events(outcome.result.work_item.id)
    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    source_providers = {source.provider for source in outcome.result.work_item.sources}
    workspace_artifact = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == "google_workspace_artifact_plan"
    )

    assert outcome.result.route == WorkItemRoute.CHIEF_OF_STAFF
    assert outcome.result.status == WorkItemStatus.NEEDS_APPROVAL
    assert outcome.checkpoint_required is True
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "stage_feed_context",
        "stage_zotero_context",
        "run_business_research",
        "finalize_step",
        "stage_google_workspace_context",
        "approval_checkpoint",
    ]
    assert {
        "preprints_context_summary",
        "zotero_context_summary",
        "company_profile",
        "google_workspace_artifact_plan",
    } <= artifact_types
    assert {
        "preprints_context_agent",
        "zotero_context_agent",
        "google_workspace_context_agent",
    } <= source_providers
    assert workspace_artifact.metadata["write_plan"]["target_system"] == "google_workspace"
    assert workspace_artifact.metadata["write_plan"]["live_write_allowed_for_specialist"] is False
    assert workspace_artifact.metadata["external_writes_enabled"] is False
    assert workspace_artifact.metadata["send_enabled"] is False
    assert any(
        gate.scope == "google_workspace_artifact_plan"
        and gate.required
        and gate.state == "pending"
        for gate in outcome.result.work_item.approval_gates
    )
    assert [
        event.actor
        for event in events
        if event.event_type == "context_evidence_staged"
    ].count("google_workspace_context_agent") == 1


def test_langgraph_stages_workspace_plan_for_generic_context_backed_artifact_plan(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="Preprint on depression evidence packet workflows",
            url="https://doi.org/10.1101/2026.02.05.345678",
            source="medRxiv",
            feed="preprints",
            doi="10.1101/2026.02.05.345678",
            tags=["preprint", "depression", "evidence packet"],
            selected=True,
            selection_reason="Relevant preliminary evidence for an internal packet.",
            summary="A preprint discusses evidence-packet workflow needs.",
        )
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=(
                "use preprints context agent evidence, then research NeuroFlow for an "
                "internal artifact plan. Keep it as an approval review plan only."
            ),
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "heuristic",
                "requested_agent": "orchestrator",
                "target_agent": "preprints_context_agent",
                "intent": "context_lookup",
                "primary_target": "NeuroFlow",
            },
        ),
        manager_loop=True,
        max_manager_steps=3,
    )

    artifact_types = {artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs}
    source_providers = {source.provider for source in outcome.result.work_item.sources}
    workspace_artifact = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == "google_workspace_artifact_plan"
    )

    assert outcome.result.route == WorkItemRoute.CHIEF_OF_STAFF
    assert outcome.result.status == WorkItemStatus.NEEDS_APPROVAL
    assert outcome.checkpoint_required is True
    assert outcome.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "stage_feed_context",
        "run_business_research",
        "finalize_step",
        "stage_google_workspace_context",
        "approval_checkpoint",
    ]
    assert {
        "preprints_context_summary",
        "company_profile",
        "google_workspace_artifact_plan",
    } <= artifact_types
    assert {"preprints_context_agent", "google_workspace_context_agent"} <= source_providers
    assert workspace_artifact.metadata["write_plan"]["target_system"] == "google_workspace"
    assert workspace_artifact.metadata["write_plan"]["live_write_allowed_for_specialist"] is False
    assert workspace_artifact.metadata["external_writes_enabled"] is False
    assert workspace_artifact.metadata["send_enabled"] is False
    assert any(
        gate.scope == "google_workspace_artifact_plan"
        and gate.required
        and gate.state == "pending"
        for gate in outcome.result.work_item.approval_gates
    )


def test_langgraph_manager_loop_stops_at_outreach_approval_checkpoint(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    research = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="research NeuroFlow",
            database_url=database_url,
            save=True,
        )
    ).result
    company_ref = research.artifact_refs[0]
    item = approve_artifact_context(
        research.work_item,
        company_ref.artifact_type,
        company_ref.artifact_id,
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
    )
    item = set_next_action(
        item,
        WorkItemNextAction(
            action="draft_outreach",
            agent=WorkItemRoute.OUTREACH_COMPOSER,
        ),
    )
    SQLiteStore(database_url).save_work_item(item)

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="continue",
            work_item_id=item.id,
            database_url=database_url,
            save=True,
        ),
        manager_loop=True,
        max_manager_steps=3,
    )

    assert outcome.result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert outcome.result.status == WorkItemStatus.NEEDS_APPROVAL
    assert outcome.checkpoint_required is True
    assert "run_outreach_composer" in outcome.node_path
    assert outcome.node_path[-1] == "approval_checkpoint"


def test_langgraph_checkpoint_approval_decision_is_stored_before_resume(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    research = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="research NeuroFlow",
            database_url=database_url,
            save=True,
        )
    ).result
    company_ref = research.artifact_refs[0]
    item = approve_artifact_context(
        research.work_item,
        company_ref.artifact_type,
        company_ref.artifact_id,
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
    )
    item = set_next_action(
        item,
        WorkItemNextAction(
            action="draft_outreach",
            agent=WorkItemRoute.OUTREACH_COMPOSER,
        ),
    )
    store = SQLiteStore(database_url)
    store.save_work_item(item)

    checkpoint = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="continue",
            work_item_id=item.id,
            database_url=database_url,
            save=True,
        ),
        manager_loop=True,
        max_manager_steps=3,
    )
    draft_ref = next(
        artifact
        for artifact in checkpoint.result.work_item.artifact_refs
        if artifact.artifact_type == "outreach_draft"
    )
    draft_gate = next(
        gate
        for gate in checkpoint.result.work_item.approval_gates
        if gate.scope == "external_use"
    )
    checkpoint_events = store.list_work_item_events(checkpoint.result.work_item.id)
    checkpoint_event = next(
        event
        for event in checkpoint_events
        if event.event_type == "langgraph_orchestration"
        and event.metadata.get("checkpoint_required") is True
    )

    assert checkpoint.result.status == WorkItemStatus.NEEDS_APPROVAL
    assert checkpoint.node_path[-1] == "approval_checkpoint"
    assert checkpoint_event.metadata["checkpoint_payload"]["schema"] == (
        "keystone.langgraph.approval_checkpoint.v1"
    )
    assert checkpoint_event.metadata["checkpoint_payload"]["work_item_id"] == item.id
    assert checkpoint_event.metadata["checkpoint_payload"]["approval_gates"][0][
        "state"
    ] in {
        ApprovalState.APPROVED_FOR_DRAFTING.value,
        ApprovalState.PENDING.value,
    }
    assert checkpoint_event.metadata["checkpoint_payload"]["send_enabled"] is False
    assert checkpoint_event.metadata["checkpoint_payload"]["external_writes_enabled"] is False

    approved = apply_slack_approval_to_work_item_gate(
        checkpoint.result.work_item,
        draft_gate.approval_id,
        ApprovalQueueStatus.APPROVED,
        actor="test-operator",
        notes="Approve external-use gate for local graph resume test only.",
        store=store,
    )
    stored_after_approval = store.get_work_item(item.id)
    assert approved.changed is True
    assert stored_after_approval is not None
    assert any(
        gate.approval_id == draft_gate.approval_id
        and gate.state == ApprovalState.APPROVED_FOR_EXTERNAL_USE.value
        for gate in stored_after_approval.approval_gates
    )
    assert any(
        event.event_type == "approval_gate_updated"
        and event.metadata["approval_id"] == draft_gate.approval_id
        and event.metadata["new_state"] == ApprovalState.APPROVED_FOR_EXTERNAL_USE.value
        for event in store.list_work_item_events(item.id)
    )
    approval_report = render_work_item_graph_report(
        stored_after_approval,
        store.list_work_item_events(item.id),
    )
    assert "Latest Approval Decision:" in approval_report
    assert draft_gate.approval_id in approval_report
    assert "pending -> approved_for_external_use" in approval_report
    assert "outreach_draft" in approval_report

    resumed = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="continue",
            work_item_id=item.id,
            database_url=database_url,
            save=True,
        ),
        manager_loop=True,
        max_manager_steps=2,
    )

    assert resumed.result.route == WorkItemRoute.ORCHESTRATOR
    assert resumed.result.status == WorkItemStatus.DONE
    assert resumed.checkpoint_required is False
    assert resumed.node_path == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
    ]
    assert "No new research, drafting, external write, or send action was run" in (
        resumed.result.human_summary
    )
    assert any(
        artifact.artifact_type == "outreach_draft"
        and artifact.artifact_id == draft_ref.artifact_id
        and artifact.approval_state == ApprovalState.APPROVED_FOR_EXTERNAL_USE.value
        for artifact in resumed.result.work_item.artifact_refs
    )
    assert all(
        artifact.metadata.get("send_enabled") is not True
        for artifact in resumed.result.work_item.artifact_refs
    )
    assert all(
        artifact.metadata.get("external_writes_enabled") is not True
        for artifact in resumed.result.work_item.artifact_refs
    )


def test_backend_selected_checkpoint_approval_resume_reports_saved_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
    research = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="research NeuroFlow",
            database_url=database_url,
            save=True,
        )
    ).result
    company_ref = research.artifact_refs[0]
    item = approve_artifact_context(
        research.work_item,
        company_ref.artifact_type,
        company_ref.artifact_id,
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
    )
    item = set_next_action(
        item,
        WorkItemNextAction(
            action="draft_outreach",
            agent=WorkItemRoute.OUTREACH_COMPOSER,
        ),
    )
    store = SQLiteStore(database_url)
    store.save_work_item(item)

    checkpoint = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text="continue",
            work_item_id=item.id,
            database_url=database_url,
            save=True,
        ),
        max_steps=3,
    )
    draft_gate = next(
        gate for gate in checkpoint.work_item.approval_gates if gate.scope == "external_use"
    )
    checkpoint_events = store.list_work_item_events(item.id)
    checkpoint_event = next(
        event
        for event in checkpoint_events
        if event.event_type == "langgraph_orchestration"
        and event.metadata.get("checkpoint_required") is True
    )

    assert checkpoint.route == WorkItemRoute.OUTREACH_COMPOSER
    assert checkpoint.status == WorkItemStatus.NEEDS_APPROVAL
    assert checkpoint_event.metadata["node_path"][-1] == "approval_checkpoint"
    assert checkpoint_event.metadata["checkpoint_payload"]["send_enabled"] is False
    assert checkpoint_event.metadata["checkpoint_payload"]["external_writes_enabled"] is False

    apply_slack_approval_to_work_item_gate(
        checkpoint.work_item,
        draft_gate.approval_id,
        ApprovalQueueStatus.APPROVED,
        actor="test-operator",
        notes="Approve external-use gate for backend-selected graph resume test only.",
        store=store,
    )

    resumed = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text="continue",
            work_item_id=item.id,
            database_url=database_url,
            save=True,
        ),
        max_steps=2,
    )
    resume_events = store.list_work_item_events(item.id)
    resume_graph_event = next(
        event
        for event in reversed(resume_events)
        if event.event_type == "langgraph_orchestration"
        and event.metadata.get("checkpoint_required") is False
    )

    assert resumed.route == WorkItemRoute.ORCHESTRATOR
    assert resumed.status == WorkItemStatus.DONE
    assert resume_graph_event.metadata["node_path"] == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
    ]
    assert "No new research, drafting, external write, or send action was run" in (
        resumed.human_summary
    )
    assert any(
        artifact.artifact_type == "outreach_draft"
        and artifact.approval_state == ApprovalState.APPROVED_FOR_EXTERNAL_USE.value
        for artifact in resumed.work_item.artifact_refs
    )
    assert all(
        artifact.metadata.get("send_enabled") is not True
        for artifact in resumed.work_item.artifact_refs
    )
    assert all(
        artifact.metadata.get("external_writes_enabled") is not True
        for artifact in resumed.work_item.artifact_refs
    )


def test_langgraph_manager_loop_live_outreach_uses_single_specialist_sdk_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    research = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="research NeuroFlow",
            database_url=database_url,
            save=True,
        )
    ).result
    company_ref = research.artifact_refs[0]
    item = approve_artifact_context(
        research.work_item,
        company_ref.artifact_type,
        company_ref.artifact_id,
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
    )
    item = set_next_action(
        item,
        WorkItemNextAction(
            action="draft_outreach",
            agent=WorkItemRoute.OUTREACH_COMPOSER,
        ),
    )
    SQLiteStore(database_url).save_work_item(item)
    captured: dict[str, object] = {"sdk_calls": 0}

    def fake_run_retrieved_sdk_synthesis(**kwargs):
        captured["sdk_calls"] = int(captured["sdk_calls"]) + 1
        captured["agent_instructions"] = str(kwargs["agent"].instructions)
        context = kwargs["retrieve"]()
        typed_input = kwargs["normalize"](context)
        captured["approved_context"] = typed_input.approved_context

        class Outcome:
            final_output = {
                "company_name": "NeuroFlow",
                "email_subject": "Comparing notes",
                "email_body": "Hi,\n\nOpen to compare notes?\n\nSincerely,\nJordan",
                "linkedin_note": "Open to compare notes?",
                "personalization_rationale": "Used only approved WorkItem context.",
                "source_ids_used": ["keystone_profile"],
            }

        return Outcome()

    def fake_compose_outreach_draft_llm_constrained(*, approved_context, **_kwargs):
        return workflow_runner.compose_outreach_draft_fixture(
            company_profile=approved_context.company_profile,
            opportunity_record=approved_context.opportunity_record,
            outreach_goal=approved_context.objective,
        ).model_copy(update={"drafting_mode": "llm_constrained"})

    def fail_user_response_synthesis(*_args, **_kwargs):
        raise AssertionError("manager-loop graph should not run generic final SDK synthesis")

    monkeypatch.setattr(
        workflow_runner,
        "run_retrieved_sdk_synthesis",
        fake_run_retrieved_sdk_synthesis,
    )
    monkeypatch.setattr(
        workflow_runner,
        "compose_outreach_draft_llm_constrained",
        fake_compose_outreach_draft_llm_constrained,
    )
    monkeypatch.setattr(
        workflow_runner,
        "synthesize_user_facing_work_item_response_sdk_result",
        fail_user_response_synthesis,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="continue",
            work_item_id=item.id,
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
        ),
        manager_loop=True,
        max_manager_steps=3,
    )

    assert captured["sdk_calls"] == 1
    assert "<!-- outreach_composer.md -->" in str(captured["agent_instructions"])
    assert "<!-- tools.md -->" in str(captured["agent_instructions"])
    assert "<!-- action_boundary_enforcement/SKILL.md -->" in str(
        captured["agent_instructions"]
    )
    assert "<!-- context_permission_gating/SKILL.md -->" in str(captured["agent_instructions"])
    assert "Approved source-backed WorkItem outreach context" in str(
        captured["approved_context"]
    )
    assert outcome.result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert outcome.result.status == WorkItemStatus.NEEDS_APPROVAL
    assert outcome.checkpoint_required is True
    assert (
        "Outreach Composer live SDK draft created; no send or live Gmail side effect occurred."
        in outcome.result.audit_notes
    )
    assert "Live user-facing response synthesis executed." in outcome.result.audit_notes


def test_langgraph_checkpoint_routing_for_approval_gated_result() -> None:
    result = WorkflowRunResult(
        work_item=WorkItem(kind=WorkItemKind.OUTREACH, title="Draft outreach"),
        route=WorkItemRoute.OUTREACH_COMPOSER,
        status=WorkItemStatus.NEEDS_APPROVAL,
        advanced=True,
        next_action=WorkItemNextAction(
            action="review_outreach_draft",
            agent=WorkItemRoute.OUTREACH_COMPOSER,
            description="Review the draft approval item before any external use.",
            requires_approval=True,
        ),
    )

    assert next_langgraph_node_for_result(result) == "approval_checkpoint"


def test_langgraph_runtime_can_be_required_only_when_installed() -> None:
    if langgraph_available():
        assert build_work_item_langgraph() is not None
    else:
        with pytest.raises(LangGraphUnavailableError):
            build_work_item_langgraph()


def test_langgraph_improvements_preserve_sdk_first_contract() -> None:
    improvements = " ".join(langgraph_functionality_improvements())

    assert "WorkItem" in improvements
    assert "SQLite" in improvements
    assert "SDK specialist agents" in improvements
    assert "Python safety gates" in improvements


def test_langgraph_env_aliases_enable_optional_work_item_runtime() -> None:
    assert work_item_langgraph_enabled({}) is False
    assert work_item_langgraph_enabled({"KEYSTONE_WORKITEM_LANGGRAPH": "true"}) is True
    assert work_item_langgraph_enabled({"KNI_BUSINESS_AGENTS_LANGGRAPH": "1"}) is True
    assert (
        work_item_langgraph_enabled(
            {
                "KNI_BUSINESS_AGENTS_LANGGRAPH": "false",
                "KEYSTONE_WORKITEM_LANGGRAPH": "true",
            }
        )
        is True
    )
    assert work_item_langgraph_enabled({"KNI_BUSINESS_AGENTS_LANGGRAPH": "off"}) is False


@pytest.mark.parametrize(
    ("case_name", "workflow_request", "manager_loop", "expected"),
    [
        (
            "simple specialist stays simple",
            WorkflowRunRequest(request_text="research NeuroFlow"),
            True,
            False,
        ),
        (
            "existing work item resumes through graph",
            WorkflowRunRequest(request_text="continue", work_item_id="wi_existing"),
            True,
            True,
        ),
        (
            "research to opportunity handoff",
            WorkflowRunRequest(request_text="research NeuroFlow and find matching opportunities"),
            True,
            True,
        ),
        (
            "gmail triage to research to draft checkpoint",
            WorkflowRunRequest(
                request_text=(
                    "triage this Gmail thread, research the company, and prepare a draft reply"
                )
            ),
            True,
            True,
        ),
        (
            "natural gmail triage research thread-local draft uses graph",
            WorkflowRunRequest(
                request_text=(
                    "@KNI gmail triage this sanitized inbound email from Mindful Care, "
                    "research Mindful Care, and prepare a draft-only Slack-thread "
                    "sample outreach for review. Email: From: Jordan Lee, Operations "
                    "at Mindful Care. Subject: Follow-up on measurement support. "
                    "Body: Hi Jordan, our team is reviewing measurement-based care "
                    "workflows and may need advisory help on evaluation design."
                )
            ),
            True,
            True,
        ),
        (
            "zotero context before research",
            WorkflowRunRequest(
                request_text=(
                    "use Zotero context agent to build evidence, then research the company"
                )
            ),
            True,
            True,
        ),
        (
            "rss context before opportunity",
            WorkflowRunRequest(
                request_text=(
                    "use RSS context agent announcement history before Opportunity Scout"
                )
            ),
            True,
            True,
        ),
        (
            "preprints context before artifact planning",
            WorkflowRunRequest(
                request_text=(
                    "use preprints context agent evidence for an internal artifact plan"
                )
            ),
            True,
            True,
        ),
        (
            "workspace artifact approval plan",
            WorkflowRunRequest(
                request_text=(
                    "use Google Workspace context agent to plan an artifact and approval gate"
                )
            ),
            True,
            True,
        ),
        (
            "chief coordinates context and specialists",
            WorkflowRunRequest(
                request_text="chief of staff coordinate context agents and selected specialists"
            ),
            True,
            True,
        ),
        (
            "chief planning-only workflow ask stays simple",
            WorkflowRunRequest(
                request_text=(
                    "@KNI chief of staff plan the safest workflow to review NeuroFlow, "
                    "state the route, handoff order, blockers, and evidence needed."
                ),
                manual_request_plan={"requested_agent": WorkItemRoute.CHIEF_OF_STAFF.value},
            ),
            True,
            False,
        ),
        (
            "chief planning-only ask with draft wording stays simple",
            WorkflowRunRequest(
                request_text=(
                    "@KNI chief of staff plan the safest workflow to research NeuroFlow, "
                    "assess the opportunity, and prepare draft-only outreach. State the "
                    "route and approval blockers; do not run the specialists yet."
                ),
                manual_request_plan={"requested_agent": WorkItemRoute.CHIEF_OF_STAFF.value},
            ),
            True,
            False,
        ),
        (
            "chief execution ask with run wording uses graph",
            WorkflowRunRequest(
                request_text=(
                    "@KNI chief of staff run the workflow to research NeuroFlow, assess "
                    "the opportunity, and prepare draft-only outreach for review."
                ),
                manual_request_plan={"requested_agent": WorkItemRoute.CHIEF_OF_STAFF.value},
            ),
            True,
            True,
        ),
        (
            "natural chief ask with announcements and saved papers uses graph",
            WorkflowRunRequest(
                request_text=(
                    "@KNI chief of staff NeuroFlow has recent announcements and saved "
                    "papers around payer partnership and outcomes-evidence signals. "
                    "Assess whether this is a real opportunity and what evidence is "
                    "missing before outreach."
                )
            ),
            True,
            True,
        ),
        (
            "natural chief research opportunity sample outreach uses graph",
            WorkflowRunRequest(
                request_text=(
                    "@KNI chief of staff NeuroFlow has payer partnership and "
                    "outcomes-evidence signals. Do research, assess whether this is "
                    "a KNI advisory opportunity, and include a draft-only Slack-thread "
                    "sample outreach for review if evidence supports pursuing it."
                )
            ),
            True,
            True,
        ),
        (
            "natural chief research opportunity sample outreach is not company specific",
            WorkflowRunRequest(
                request_text=(
                    "@KNI chief of staff Example Health has payer partnership and "
                    "implementation evidence signals. Do research, assess whether this is "
                    "a KNI advisory opportunity, and include a draft-only Slack-thread "
                    "sample outreach for review if evidence supports pursuing it."
                )
            ),
            True,
            True,
        ),
        (
            "context-only read stays simple",
            WorkflowRunRequest(request_text="use Zotero context agent to summarize the collection"),
            True,
            False,
        ),
        (
            "negated outreach draft safety language stays simple",
            WorkflowRunRequest(
                request_text="research NeuroFlow. Do not draft outreach or send email."
            ),
            True,
            False,
        ),
        (
            "deterministic status ask stays simple",
            WorkflowRunRequest(request_text="show WorkItem status"),
            True,
            False,
        ),
        (
            "single-step approval boundary uses graph outside manager loop",
            WorkflowRunRequest(request_text="prepare draft-only outreach for approved context"),
            False,
            True,
        ),
        (
            "single-step plain research stays simple outside manager loop",
            WorkflowRunRequest(request_text="research NeuroFlow"),
            False,
            False,
        ),
    ],
)
def test_backend_selects_langgraph_only_for_graph_worthy_work_item_flows(
    case_name: str,
    workflow_request: WorkflowRunRequest,
    manager_loop: bool,
    expected: bool,
) -> None:
    assert (
        should_use_langgraph_for_work_item(workflow_request, manager_loop=manager_loop)
        is expected
    ), case_name


def test_negated_capabilities_do_not_make_request_graph_worthy() -> None:
    request = WorkflowRunRequest(
        request_text=(
            "CoS, return three bullets from supplied facts. Do not run a workflow, "
            "research, assess opportunities, draft outreach, or write provider records."
        )
    )

    assert should_use_langgraph_for_work_item(request, manager_loop=True) is False


@pytest.mark.parametrize(
    ("request_text", "requires_durable_state", "workflow", "expected"),
    [
        (
            "Give me the answer now; the background mentions workflow, handoff, and approval.",
            False,
            [],
            False,
        ),
        (
            "Handle this review and keep it available for later.",
            True,
            [],
            True,
        ),
        (
            "Take care of the supplied review.",
            True,
            [
                WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                WorkItemRoute.OPPORTUNITY_SCOUT.value,
            ],
            True,
        ),
    ],
)
def test_live_semantic_plan_owns_graph_selection_across_natural_phrasings(
    request_text: str,
    requires_durable_state: bool,
    workflow: list[str],
    expected: bool,
) -> None:
    request = WorkflowRunRequest(
        request_text=request_text,
        manual_request_plan={
            "source": "llm",
            "target_agent": "chief_of_staff",
            "intent": "route_request",
            "task_objective": "route_or_continue",
            "expected_artifact_type": "none",
            "requires_durable_state": requires_durable_state,
            "workflow": workflow,
        },
    )

    assert should_use_langgraph_for_work_item(request, manager_loop=True) is expected
    assert should_use_langgraph_for_work_item(request, manager_loop=False) is expected


def test_canonical_source_alias_also_owns_graph_selection() -> None:
    request = WorkflowRunRequest(
        request_text=(
            "The background mentions workflow, approval, research, opportunity, and draft. "
            "Return the supplied-facts answer only."
        ),
        manual_request_plan={
            "source": "canonical",
            "target_agent": "chief_of_staff",
            "intent": "route_request",
            "task_objective": "route_or_continue",
            "requires_durable_state": False,
            "workflow": [],
        },
    )

    assert should_use_langgraph_for_work_item(request, manager_loop=True) is False


def test_langgraph_work_item_thread_id_is_stable() -> None:
    assert work_item_graph_thread_id("wi_123") == "work-item:wi_123"
    assert work_item_graph_thread_id("") == ""
