from __future__ import annotations

import json
from pathlib import Path

from keystone_agents.receipts.inspection import inspect_work_item_receipts
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.operational_receipts import WorkItemReceiptInspection
from keystone_agents.schemas.signal_lifecycle import (
    SIGNAL_LIFECYCLE_ARTIFACT_TYPE,
    SignalLifecycleCheckpoint,
    SignalLifecycleStage,
    SignalLifecycleStatus,
    SignalSourceKind,
)
from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemApprovalGate,
    WorkItemArtifactRef,
    WorkItemEvent,
    WorkItemKind,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemStatus,
)
from keystone_agents.storage.sqlite_store import SQLiteStore


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'receipt-inspection.db'}"


def _verified_write_receipt() -> dict[str, object]:
    return {
        "tool_name": "airtable_create_record",
        "operation": "create_record",
        "status": "success",
        "provider": "airtable",
        "record_id": "rec_verified_001",
        "provider_write": True,
        "verification": {"passed": True, "status": "verified_present"},
    }


def test_inspection_reports_verified_receipts_and_exact_resume_without_writes(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    artifact = WorkItemArtifactRef(
        artifact_type="company_profile",
        artifact_id="profile-1",
        source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        title="Fixture profile",
        metadata={
            "tool_receipts": [
                _verified_write_receipt(),
                {
                    "tool_name": "gmail_search_messages",
                    "operation": "search_messages",
                    "status": "success",
                    "provider": "gmail",
                    "provider_read": True,
                    "verified": True,
                },
                {
                    **_verified_write_receipt(),
                    "tool_name": "airtable_update_record",
                    "verification": {"passed": False},
                },
            ]
        },
    )
    item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        status=WorkItemStatus.IN_PROGRESS,
        title="Research then draft",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        last_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        artifact_refs=[artifact],
        next_action=WorkItemNextAction(
            action="continue_planned_workflow",
            agent=WorkItemRoute.OUTREACH_COMPOSER,
            description="Draft from the verified research artifact.",
        ),
    )
    store.save_work_item(item)
    store.save_work_item_artifact(item.id, artifact)
    store.save_work_item_event(
        item.id,
        WorkItemEvent(
            event_type="manager_loop_completed",
            metadata={
                "steps": [
                    {
                        "step": 1,
                        "route": WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                        "status": WorkItemStatus.IN_PROGRESS.value,
                        "advanced": True,
                        "artifact_types": ["company_profile"],
                    }
                ]
            },
        ),
    )
    counts_before = {
        "work_items": store.count("work_items"),
        "events": store.count("work_item_events"),
        "artifacts": store.count("work_item_artifacts"),
    }

    payload = json.loads(
        inspect_work_item_receipts(item.id, store=store).model_dump_json()
    )
    result = WorkItemReceiptInspection.model_validate(payload)

    assert result.resume_point.disposition == "resume"
    assert result.resume_point.exact is True
    assert result.resume_point.stage == "continue_planned_workflow"
    assert result.resume_point.agent == WorkItemRoute.OUTREACH_COMPOSER.value
    assert result.resume_point.do_not_repeat_tool_names == [
        "airtable_create_record"
    ]
    assert result.resume_point.do_not_recreate_object_ids == ["rec_verified_001"]
    assert {(receipt.receipt_kind, receipt.tool_name) for receipt in result.tool_receipts} == {
        ("write", "airtable_create_record"),
        ("read", "gmail_search_messages"),
    }
    assert result.ignored_unverified_receipt_count == 1
    assert result.stage_receipts[0].verification_basis == (
        "manager_loop_audit_step_and_canonical_work_item_artifact"
    )
    assert result.provider_calls_performed == 0
    assert result.external_write_performed is False
    assert {
        "work_items": store.count("work_items"),
        "events": store.count("work_item_events"),
        "artifacts": store.count("work_item_artifacts"),
    } == counts_before


def test_pending_approval_prevents_resume_even_with_verified_write_receipt(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    item = WorkItem(
        kind=WorkItemKind.OUTREACH,
        status=WorkItemStatus.NEEDS_APPROVAL,
        title="Review draft",
        current_route=WorkItemRoute.OUTREACH_COMPOSER,
        approval_gates=[
            WorkItemApprovalGate(scope="external_use", state="pending", required=True)
        ],
        next_action=WorkItemNextAction(
            action="request_external_use_approval",
            agent=WorkItemRoute.OUTREACH_COMPOSER,
            requires_approval=True,
            description="Wait for scoped external-use approval.",
        ),
    )
    store.save_work_item(item)

    result = inspect_work_item_receipts(item.id, store=store)

    assert result.resume_point.disposition == "await_approval"
    assert result.resume_point.exact is True
    assert result.resume_point.requires_approval is True
    assert result.unresolved_approval_scopes == ["external_use"]


def test_rejected_approval_is_blocked_not_waiting(tmp_path: Path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    item = WorkItem(
        kind=WorkItemKind.OUTREACH,
        status=WorkItemStatus.BLOCKED,
        title="Rejected draft",
        current_route=WorkItemRoute.OUTREACH_COMPOSER,
        approval_gates=[
            WorkItemApprovalGate(
                scope="external_use",
                state="rejected",
                required=True,
            )
        ],
        next_action=WorkItemNextAction(
            action="resolve_rejected_approval",
            agent=WorkItemRoute.OUTREACH_COMPOSER,
            requires_approval=True,
            description="Create a new reviewed artifact or archive this WorkItem.",
        ),
    )
    store.save_work_item(item)

    result = inspect_work_item_receipts(item.id, store=store)

    assert result.resume_point.disposition == "blocked"
    assert result.resume_point.exact is True
    assert result.resume_point.basis == [
        "unresolved_approval:external_use:rejected"
    ]


def test_nonterminal_work_item_without_next_action_fails_closed(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        status=WorkItemStatus.IN_PROGRESS,
        title="Incomplete state",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
    )
    store.save_work_item(item)

    result = inspect_work_item_receipts(item.id, store=store)

    assert result.resume_point.disposition == "indeterminate"
    assert result.resume_point.exact is False
    assert result.resume_point.basis == ["missing_canonical_next_action"]


def test_terminal_work_item_never_requires_resume(tmp_path: Path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        status=WorkItemStatus.DONE,
        title="Completed research",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        next_action=WorkItemNextAction(
            action="optional_deepening",
            agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        ),
    )
    store.save_work_item(item)

    result = inspect_work_item_receipts(item.id, store=store)

    assert result.resume_point.disposition == "terminal"
    assert result.resume_point.required is False
    assert result.resume_point.stage == ""


def test_chief_and_orchestrator_expose_receipt_inspection_as_core_read() -> None:
    from keystone_agents.agents.chief_of_staff import build_chief_of_staff_agent
    from keystone_agents.agents.orchestrator import build_orchestrator_agent

    durable_plan = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="continue_work_item",
        requires_durable_state=True,
    )
    chief = build_chief_of_staff_agent(
        request_text="Resume the saved WorkItem safely.",
        manual_request_plan=durable_plan,
    )
    orchestrator = build_orchestrator_agent(tool_tier="core_read")

    assert "inspect_work_item_execution_receipts" in {
        getattr(tool, "name", "") for tool in chief.tools
    }
    assert "inspect_work_item_execution_receipts" in {
        getattr(tool, "name", "") for tool in orchestrator.tools
    }
    receipt_tool = next(
        tool
        for tool in orchestrator.tools
        if getattr(tool, "name", "") == "inspect_work_item_execution_receipts"
    )
    assert set(receipt_tool.params_json_schema["properties"]) == {"work_item_id"}


def test_stage_receipt_requires_artifact_from_the_exact_reported_route(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    artifact = WorkItemArtifactRef(
        artifact_type="company_profile",
        artifact_id="profile-wrong-owner",
        source_agent=WorkItemRoute.OUTREACH_COMPOSER.value,
    )
    item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        status=WorkItemStatus.IN_PROGRESS,
        title="Wrong provenance",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        artifact_refs=[artifact],
        next_action=WorkItemNextAction(
            action="repair_artifact_provenance",
            agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        ),
    )
    store.save_work_item(item)
    store.save_work_item_artifact(item.id, artifact)
    store.save_work_item_event(
        item.id,
        WorkItemEvent(
            event_type="manager_loop_completed",
            metadata={
                "steps": [
                    {
                        "route": WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                        "status": "in_progress",
                        "advanced": True,
                        "artifact_types": ["company_profile"],
                    }
                ]
            },
        ),
    )

    result = inspect_work_item_receipts(item.id, store=store)

    assert not any(
        receipt.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
        and receipt.verification_basis.startswith("manager_loop")
        for receipt in result.stage_receipts
    )
    assert any(
        receipt.route == WorkItemRoute.OUTREACH_COMPOSER.value
        and receipt.verification_basis == "canonical_work_item_artifact"
        for receipt in result.stage_receipts
    )


def test_lifecycle_checkpoint_exposes_exact_resume_and_completed_stage_receipts(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    item = WorkItem(
        id="wi_receipt_signal",
        kind=WorkItemKind.RESEARCH_BRIEF,
        status=WorkItemStatus.IN_PROGRESS,
        title="Resume signal",
        current_route=WorkItemRoute.RSS_CONTEXT_AGENT,
    )
    checkpoint = SignalLifecycleCheckpoint(
        work_item_id=item.id,
        source_kind=SignalSourceKind.RSS,
        trigger_id="sig_receipt",
        trigger_ref_digest="a" * 64,
        dedupe_scope_digest="b" * 64,
        status=SignalLifecycleStatus.PARTIAL_FAILURE,
        completed_stages=[SignalLifecycleStage.CONTEXT_RETRIEVED],
        next_stage=SignalLifecycleStage.HANDOFF_PREPARED,
        failed_stage=SignalLifecycleStage.HANDOFF_PREPARED.value,
        failure_code="SyntheticFailure",
        safe_next_action="Retry the same WorkItem at handoff_prepared.",
        dry_run=False,
    )
    lifecycle_artifact = WorkItemArtifactRef(
        artifact_type=SIGNAL_LIFECYCLE_ARTIFACT_TYPE,
        artifact_id=checkpoint.trigger_id,
        source_agent=WorkItemRoute.RSS_CONTEXT_AGENT.value,
        approval_state="internal_checkpoint",
        metadata={"checkpoint": checkpoint.model_dump(mode="json")},
    )
    context_artifact = WorkItemArtifactRef(
        artifact_type="rss_context",
        artifact_id="rss-context-1",
        source_agent=WorkItemRoute.RSS_CONTEXT_AGENT.value,
        metadata={"signal_lifecycle_trigger_id": checkpoint.trigger_id},
    )
    item = item.model_copy(update={"artifact_refs": [lifecycle_artifact, context_artifact]})
    store.save_work_item(item)
    store.save_work_item_artifact(item.id, lifecycle_artifact)
    store.save_work_item_artifact(item.id, context_artifact)

    result = inspect_work_item_receipts(item.id, store=store)

    assert result.resume_point.disposition == "resume"
    assert result.resume_point.exact is True
    assert result.resume_point.stage == "handoff_prepared"
    assert result.resume_point.do_not_repeat_stages == ["context_retrieved"]
    assert any(
        receipt.stage == "context_retrieved"
        and receipt.route == WorkItemRoute.RSS_CONTEXT_AGENT.value
        and receipt.verification_basis
        == "typed_signal_lifecycle_checkpoint_and_route_artifact"
        for receipt in result.stage_receipts
    )
