from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import keystone_agents.workflow_runner as workflow_runner
from keystone_agents.langgraph_workflow import run_work_item_langgraph
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.schemas.announcement_feed import AnnouncementFeedItem
from keystone_agents.schemas.approval import ApprovalState
from keystone_agents.schemas.chief_of_staff import (
    ChiefContextHandoff,
    ChiefDurableHandoff,
    ChiefOfStaffResult,
    ChiefOfStaffRouteRecommendation,
)
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkflowRunResult,
    WorkItem,
    WorkItemArtifactRef,
    WorkItemFact,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemStatus,
)
from keystone_agents.storage.sqlite_store import SQLiteStore

FIXTURE_PATH = Path("tests/fixtures/slack_chief_preprints_zotero_graph_replay.json")
OPPORTUNITY_FIXTURE_PATH = Path(
    "tests/fixtures/slack_opportunity_airtable_outreach_replay.json"
)


def test_natural_chief_preprints_zotero_replay_uses_real_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    database_path = tmp_path / "chief-context-graph.db"
    database_url = f"sqlite:///{database_path}"
    store = SQLiteStore(database_url)
    preprint = fixture["preprint_fixture"]
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title=preprint["title"],
            url=preprint["url"],
            source=preprint["source"],
            feed=preprint["feed"],
            doi=preprint["doi"],
            tags=preprint["tags"],
            selected=True,
            selection_reason=preprint["selection_reason"],
            summary=preprint["summary"],
        )
    )

    context_agents = fixture["expected"]["context_agents"]

    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Stage the existing read-only paper context, then have Business "
                    "Research compare established and preliminary evidence."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="research-direction-review",
                    target_channel="current thread",
                    command_text="Run the selected durable specialist with context.",
                ),
                durable_handoff=ChiefDurableHandoff(
                    agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                    rationale="Business Research owns the evidence-maturity comparison.",
                ),
                context_handoffs=[
                    ChiefContextHandoff(
                        agent=agent,
                        before_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                        rationale="Stage read-only context before synthesis.",
                    )
                    for agent in context_agents
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
            request_text=fixture["current_request"],
            database_url=database_url,
            save=True,
            live_sdk=True,
            live_search=False,
            manual_request_plan=fixture["manual_request_plan"],
        ),
        manager_loop=True,
        max_manager_steps=4,
    )

    expected = fixture["expected"]
    events = store.list_work_item_events(outcome.result.work_item.id)
    artifacts = outcome.result.work_item.artifact_refs
    artifact_types = {artifact.artifact_type for artifact in artifacts}
    source_providers = {source.provider for source in outcome.result.work_item.sources}
    staged_context_agents = [
        event.actor
        for event in events
        if event.event_type == "context_evidence_staged"
        and event.actor in context_agents
    ]

    assert outcome.result.route.value == expected["route"], {
        "route": outcome.result.route.value,
        "status": outcome.result.status.value,
        "node_path": " -> ".join(outcome.node_path),
        "artifact_types": sorted(artifact_types),
        "chief_context_handoffs": next(
            (
                artifact.metadata.get("context_handoffs")
                for artifact in artifacts
                if artifact.artifact_type == "chief_of_staff_plan"
            ),
            None,
        ),
    }
    assert outcome.result.status.value == expected["status"]
    assert outcome.result.work_item.request_text == fixture["current_request"]
    assert outcome.node_path == expected["node_path"], {
        "node_path": outcome.node_path,
        "chief_context_handoffs": next(
            (
                artifact.metadata.get("context_handoffs")
                for artifact in artifacts
                if artifact.artifact_type == "chief_of_staff_plan"
            ),
            None,
        ),
    }
    assert set(expected["artifact_types"]) <= artifact_types
    assert set(context_agents) <= source_providers
    assert staged_context_agents == context_agents
    assert all(
        artifact.metadata.get("external_write_performed") is not True
        and artifact.metadata.get("send_enabled") is not True
        for artifact in artifacts
    )

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tool_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM approval_queue").fetchone()[0] == 0


def test_quick_opportunity_airtable_outreach_replay_uses_full_review_only_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = json.loads(OPPORTUNITY_FIXTURE_PATH.read_text(encoding="utf-8"))
    database_path = tmp_path / "opportunity-airtable-outreach.db"
    database_url = f"sqlite:///{database_path}"
    request_text = fixture["current_request"]

    def fake_advance_opportunity(
        work_item: WorkItem,
        *,
        request: WorkflowRunRequest,
        store: SQLiteStore | None,
    ) -> WorkflowRunResult:
        del request
        source = WorkItemSourceRef(
            source_id="fixture:affectai:evidence-review",
            title="AffectAI evidence-review note",
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
                    update={"name": "AffectAI", "object_type": "company"}
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
                        artifact_id="fixture-affectai-opportunity",
                        source_agent=WorkItemRoute.OPPORTUNITY_SCOUT.value,
                        approval_state=ApprovalState.APPROVED_FOR_DRAFTING.value,
                        selected=True,
                        title="AffectAI evidence-review opportunity",
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
            human_summary="Selected the supplied opportunity for bounded review.",
        )

    monkeypatch.setattr(
        workflow_runner,
        "_advance_opportunity",
        fake_advance_opportunity,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text=request_text,
            database_url=database_url,
            save=True,
            live_sdk=False,
            live_search=False,
            requested_route=WorkItemRoute.OPPORTUNITY_SCOUT,
            manual_request_plan=fixture["manual_request_plan"],
        ),
        manager_loop=True,
        max_manager_steps=6,
    )

    expected = fixture["expected"]
    artifact_types = {
        artifact.artifact_type for artifact in outcome.result.work_item.artifact_refs
    }
    assert outcome.result.work_item.request_text == request_text
    assert outcome.result.route.value == expected["route"], {
        "route": outcome.result.route.value,
        "status": outcome.result.status.value,
        "node_path": outcome.node_path,
        "blockers": [blocker.code for blocker in outcome.result.blockers],
        "artifact_types": sorted(artifact_types),
    }
    assert outcome.result.status.value == expected["status"]
    assert set(expected["artifact_types"]) <= artifact_types
    node_positions = [outcome.node_path.index(node) for node in expected["node_order"]]
    assert node_positions == sorted(node_positions)
    assert outcome.checkpoint_required is True
    assert outcome.node_path[-1] == "approval_checkpoint"
    assert {
        "manager_loop_outreach_not_drafted",
        "manager_loop_current_opportunity_evidence_missing",
    }.isdisjoint({blocker.code for blocker in outcome.result.blockers})
    airtable_plan = next(
        artifact
        for artifact in outcome.result.work_item.artifact_refs
        if artifact.artifact_type == "airtable_write_plan"
    )
    assert airtable_plan.metadata["mode"] == "plan_only_no_provider_write"
    assert airtable_plan.metadata["provider_write_authorized"] is False
    assert all(
        artifact.metadata.get("external_write_performed") is not True
        and artifact.metadata.get("external_writes_enabled") is not True
        and artifact.metadata.get("send_enabled") is not True
        for artifact in outcome.result.work_item.artifact_refs
    )
    assert outcome.result.work_item.target.name == "AffectAI"

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tool_events").fetchone()[0] == 0
