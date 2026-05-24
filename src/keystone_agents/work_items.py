"""Deterministic WorkItem state helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from keystone_agents.schemas.approval import (
    ApprovalQueueStatus,
    ApprovalState,
    approved_state_for_scope,
    normalize_approval_queue_status,
    normalize_approval_state,
)
from keystone_agents.schemas.context_pack import (
    ContextPack,
    ContextPackReadinessGate,
    GmailContextPack,
    MemoryContextRef,
    OpportunityContextPack,
    OutreachContactContext,
    OutreachContextPack,
    ResearchContextPack,
)
from keystone_agents.schemas.memory import normalize_memory_key
from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemApprovalGate,
    WorkItemArtifactRef,
    WorkItemBlocker,
    WorkItemEvent,
    WorkItemFact,
    WorkItemKind,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemStatus,
    WorkItemTarget,
    utc_now_iso,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.zotero_research import (
    extract_zotero_article_query,
    extract_zotero_collection_hint,
    looks_like_zotero_article_request,
    looks_like_zotero_collection_request,
)


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    blockers: tuple[WorkItemBlocker, ...] = ()
    next_action: WorkItemNextAction | None = None


@dataclass(frozen=True)
class ApprovalGateUpdateResult:
    work_item: WorkItem
    approval_id: str
    gate_scope: str
    previous_state: str
    new_state: str
    changed: bool
    event_recorded: bool


def create_or_load_work_item(
    *,
    store: SQLiteStore | None,
    request_text: str,
    work_item_id: str | None = None,
    route: WorkItemRoute | str | None = None,
) -> WorkItem:
    """Load an existing WorkItem or create a new case shell."""

    if work_item_id and store is not None:
        existing = store.get_work_item(work_item_id)
        if existing is not None:
            return existing
    resolved_route = _coerce_route(route)
    target = _target_for_request(request_text, resolved_route)
    return WorkItem(
        kind=_kind_for_target(resolved_route, target),
        status=WorkItemStatus.NEW,
        title=_title_for_request(request_text, resolved_route),
        request_text=request_text,
        target=target,
        current_route=resolved_route,
    )


def summarize_work_item_for_agent(work_item: WorkItem, *, max_artifacts: int = 5) -> dict[str, Any]:
    """Return bounded structured state for specialist prompts or logs."""

    return {
        "id": work_item.id,
        "kind": work_item.kind.value,
        "status": work_item.status.value,
        "title": work_item.title,
        "target": work_item.target.model_dump(mode="json"),
        "current_route": work_item.current_route.value,
        "facts": [fact.model_dump(mode="json") for fact in work_item.facts[:12]],
        "sources": [source.model_dump(mode="json") for source in work_item.sources[:12]],
        "artifact_refs": [
            ref.model_dump(mode="json") for ref in work_item.artifact_refs[:max_artifacts]
        ],
        "open_blockers": [
            blocker.model_dump(mode="json")
            for blocker in work_item.blockers
            if not blocker.resolved
        ],
        "next_action": (
            work_item.next_action.model_dump(mode="json") if work_item.next_action else None
        ),
    }


def attach_artifact(work_item: WorkItem, artifact: WorkItemArtifactRef) -> WorkItem:
    refs = [
        ref
        for ref in work_item.artifact_refs
        if not (
            ref.artifact_type == artifact.artifact_type and ref.artifact_id == artifact.artifact_id
        )
    ]
    return work_item.model_copy(update={"artifact_refs": [*refs, artifact]}).touch()


def attach_existing_artifact(
    work_item: WorkItem,
    artifact_type: str,
    artifact_id: str | int,
    *,
    title: str = "",
    summary: str = "",
    source_agent: str = "",
    approval_state: str = ApprovalState.PENDING.value,
    selected: bool = False,
) -> WorkItem:
    """Attach an existing stored artifact row by reference."""

    artifact = WorkItemArtifactRef(
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        source_agent=source_agent,
        approval_state=approval_state,
        title=title,
        summary=summary,
        selected=selected,
    )
    return attach_artifact(work_item, artifact)


def parse_artifact_spec(spec: str) -> tuple[str, str]:
    """Parse artifact specs like company_profile:12 or opportunity:7."""

    cleaned = str(spec or "").strip()
    if ":" not in cleaned:
        raise ValueError("artifact must use <artifact_type>:<artifact_id> format")
    artifact_type, artifact_id = cleaned.split(":", maxsplit=1)
    artifact_type = artifact_type.strip()
    artifact_id = artifact_id.strip()
    if not artifact_type or not artifact_id:
        raise ValueError("artifact type and id are required")
    return artifact_type, artifact_id


def find_artifact(
    work_item: WorkItem,
    artifact_type: str,
    artifact_id: str | int,
) -> WorkItemArtifactRef | None:
    wanted_id = str(artifact_id)
    for artifact in work_item.artifact_refs:
        if artifact.artifact_type == artifact_type and artifact.artifact_id == wanted_id:
            return artifact
    return None


def selected_artifacts(
    work_item: WorkItem,
    artifact_type: str | None = None,
) -> list[WorkItemArtifactRef]:
    artifacts = [
        artifact
        for artifact in work_item.artifact_refs
        if artifact.selected and (artifact_type is None or artifact.artifact_type == artifact_type)
    ]
    if artifacts or artifact_type is None:
        return artifacts
    fallback = [
        artifact for artifact in work_item.artifact_refs if artifact.artifact_type == artifact_type
    ]
    return fallback[:1]


def select_artifact(
    work_item: WorkItem,
    artifact_type: str,
    artifact_id: str | int,
) -> WorkItem:
    """Mark one artifact selected, unselecting other artifacts of the same type."""

    wanted_id = str(artifact_id)
    found = False
    artifacts: list[WorkItemArtifactRef] = []
    for artifact in work_item.artifact_refs:
        selected = artifact.artifact_type == artifact_type and artifact.artifact_id == wanted_id
        found = found or selected
        artifacts.append(
            artifact.model_copy(
                update={
                    "selected": (
                        selected if artifact.artifact_type == artifact_type else artifact.selected
                    ),
                    "metadata": {**artifact.metadata, "selected": selected}
                    if artifact.artifact_type == artifact_type
                    else artifact.metadata,
                }
            )
        )
    if not found:
        raise ValueError(f"Artifact not attached to WorkItem: {artifact_type}:{wanted_id}")
    return work_item.model_copy(update={"artifact_refs": artifacts}).touch()


def approve_artifact_context(
    work_item: WorkItem,
    artifact_type: str,
    artifact_id: str | int,
    *,
    approval_state: ApprovalState | str = ApprovalState.APPROVED_FOR_DRAFTING,
    approval_id: str = "",
) -> WorkItem:
    """Approve one artifact for a deterministic WorkItem context gate."""

    state = normalize_approval_state(approval_state).value
    wanted_id = str(artifact_id)
    found = False
    artifacts: list[WorkItemArtifactRef] = []
    for artifact in work_item.artifact_refs:
        if artifact.artifact_type == artifact_type and artifact.artifact_id == wanted_id:
            found = True
            artifacts.append(
                artifact.model_copy(
                    update={
                        "approval_state": state,
                        "selected": True,
                        "metadata": {**artifact.metadata, "selected": True},
                    }
                )
            )
        elif artifact.artifact_type == artifact_type:
            artifacts.append(
                artifact.model_copy(
                    update={
                        "selected": False,
                        "metadata": {**artifact.metadata, "selected": False},
                    }
                )
            )
        else:
            artifacts.append(artifact)
    if not found:
        raise ValueError(f"Artifact not attached to WorkItem: {artifact_type}:{wanted_id}")

    gates = [
        gate
        for gate in work_item.approval_gates
        if not (
            gate.scope == "drafting_context" and gate.approval_id == f"{artifact_type}:{wanted_id}"
        )
    ]
    gates.append(
        WorkItemApprovalGate(
            scope="drafting_context",
            state=state,
            required=True,
            rationale=(
                f"Artifact {artifact_type}:{wanted_id} approved for WorkItem drafting context."
            ),
            approval_id=approval_id or f"{artifact_type}:{wanted_id}",
        )
    )
    updated = work_item.model_copy(
        update={"artifact_refs": artifacts, "approval_gates": gates}
    ).touch()
    return resolve_blocker(updated, "outreach_requires_approved_context")


def apply_slack_approval_to_work_item_gate(
    work_item: WorkItem,
    approval_id: str,
    status: ApprovalQueueStatus | str,
    *,
    actor: str,
    notes: str = "",
    slack_context: dict[str, Any] | None = None,
    store: SQLiteStore | None = None,
) -> ApprovalGateUpdateResult:
    """Apply one Slack approval decision to the exact WorkItem gate it references."""

    cleaned_approval_id = str(approval_id or "").strip()
    if not cleaned_approval_id:
        raise ValueError("approval_id is required to update a WorkItem approval gate")
    resolved_status = normalize_approval_queue_status(status)
    matching_gate = next(
        (gate for gate in work_item.approval_gates if gate.approval_id == cleaned_approval_id),
        None,
    )
    if matching_gate is None:
        raise ValueError(f"stale WorkItem approval id: {cleaned_approval_id}")

    previous_state = matching_gate.state
    new_state = _gate_state_for_queue_status(matching_gate, resolved_status)
    changed = previous_state != new_state
    if not changed:
        return ApprovalGateUpdateResult(
            work_item=work_item,
            approval_id=cleaned_approval_id,
            gate_scope=matching_gate.scope,
            previous_state=previous_state,
            new_state=new_state,
            changed=False,
            event_recorded=False,
        )

    updated_gates = [
        gate.model_copy(
            update={
                "state": new_state,
                "rationale": _gate_rationale(
                    gate,
                    resolved_status,
                    actor=actor,
                    notes=notes,
                ),
            }
        )
        if gate.approval_id == cleaned_approval_id
        else gate
        for gate in work_item.approval_gates
    ]
    updated = work_item.model_copy(update={"approval_gates": updated_gates}).touch()
    blocker_code = _gate_blocker_code(cleaned_approval_id)
    if resolved_status == ApprovalQueueStatus.APPROVED:
        updated = resolve_blocker(updated, blocker_code)
        updated = set_next_action(
            updated,
            _approved_gate_next_action(matching_gate, approval_id=cleaned_approval_id),
        )
    else:
        updated = add_blocker(
            updated,
            WorkItemBlocker(
                code=blocker_code,
                message=_blocked_gate_message(matching_gate, resolved_status, actor=actor),
            ),
        )
        updated = set_next_action(
            updated,
            _blocked_gate_next_action(matching_gate, resolved_status),
        )

    updated = updated.model_copy(update={"status": derive_case_status(updated)}).touch()
    record_event(
        updated,
        event_type="approval_gate_updated",
        actor=actor or "slack",
        summary=(
            f"Slack {resolved_status.value} action changed "
            f"{matching_gate.scope} approval gate from {previous_state} to {new_state}."
        ),
        metadata={
            "approval_id": cleaned_approval_id,
            "gate_scope": matching_gate.scope,
            "previous_state": previous_state,
            "new_state": new_state,
            "approval_queue_status": resolved_status.value,
            "notes": notes,
            "slack": slack_context or {},
        },
        store=store,
    )
    if store is not None:
        store.save_work_item(updated)
    return ApprovalGateUpdateResult(
        work_item=updated,
        approval_id=cleaned_approval_id,
        gate_scope=matching_gate.scope,
        previous_state=previous_state,
        new_state=new_state,
        changed=True,
        event_recorded=True,
    )


def record_event(
    work_item: WorkItem,
    *,
    event_type: str,
    summary: str,
    actor: str = "system",
    metadata: dict[str, Any] | None = None,
    store: SQLiteStore | None = None,
) -> WorkItemEvent:
    event = WorkItemEvent(
        event_type=event_type,
        actor=actor,
        summary=summary,
        metadata=metadata or {},
    )
    if store is not None:
        store.save_work_item_event(work_item.id, event)
    return event


def set_next_action(work_item: WorkItem, next_action: WorkItemNextAction | None) -> WorkItem:
    return work_item.model_copy(update={"next_action": next_action}).touch()


def add_blocker(work_item: WorkItem, blocker: WorkItemBlocker) -> WorkItem:
    blockers = [
        existing
        for existing in work_item.blockers
        if not (existing.code == blocker.code and not existing.resolved)
    ]
    return work_item.model_copy(update={"blockers": [*blockers, blocker]}).touch()


def resolve_blocker(work_item: WorkItem, code: str) -> WorkItem:
    resolved: list[WorkItemBlocker] = []
    for blocker in work_item.blockers:
        if blocker.code == code and not blocker.resolved:
            resolved.append(
                blocker.model_copy(update={"resolved": True, "resolved_at": utc_now_iso()})
            )
        else:
            resolved.append(blocker)
    return work_item.model_copy(update={"blockers": resolved}).touch()


def _gate_state_for_queue_status(
    gate: WorkItemApprovalGate,
    status: ApprovalQueueStatus,
) -> str:
    if status == ApprovalQueueStatus.APPROVED:
        return approved_state_for_scope(_approval_scope_for_gate(gate.scope)).value
    if status == ApprovalQueueStatus.REJECTED:
        return ApprovalState.REJECTED.value
    if status == ApprovalQueueStatus.REVISE:
        return "revision_requested"
    if status == ApprovalQueueStatus.EXPIRED:
        return ApprovalState.EXPIRED.value
    return ApprovalState.PENDING.value


def _approval_scope_for_gate(scope: str) -> str:
    if scope == "drafting_context":
        return "drafting"
    return scope


def _gate_rationale(
    gate: WorkItemApprovalGate,
    status: ApprovalQueueStatus,
    *,
    actor: str,
    notes: str,
) -> str:
    actor_text = actor or "Slack reviewer"
    if status == ApprovalQueueStatus.APPROVED:
        return (
            f"{actor_text} approved this {gate.scope} gate from Slack. "
            "No send, publish, schedule, post, or live draft side effect was executed."
        )
    if status == ApprovalQueueStatus.REVISE:
        suffix = f" Feedback: {notes}" if notes else ""
        return f"{actor_text} requested revision for this {gate.scope} gate from Slack.{suffix}"
    if status == ApprovalQueueStatus.REJECTED:
        suffix = f" Feedback: {notes}" if notes else ""
        return f"{actor_text} rejected this {gate.scope} gate from Slack.{suffix}"
    return f"{actor_text} updated this {gate.scope} gate from Slack."


def _gate_blocker_code(approval_id: str) -> str:
    suffix = re.sub(r"[^a-zA-Z0-9_]+", "_", approval_id).strip("_") or "unknown"
    return f"approval_gate_blocked_{suffix}"


def _blocked_gate_message(
    gate: WorkItemApprovalGate,
    status: ApprovalQueueStatus,
    *,
    actor: str,
) -> str:
    actor_text = actor or "Slack reviewer"
    if status == ApprovalQueueStatus.REVISE:
        return (
            f"{actor_text} requested revisions for the {gate.scope} approval gate. "
            "Revise the artifact and resubmit a new approval request before continuing."
        )
    if status == ApprovalQueueStatus.REJECTED:
        return (
            f"{actor_text} rejected the {gate.scope} approval gate. "
            "Create a new reviewed artifact or close the WorkItem before continuing."
        )
    return f"The {gate.scope} approval gate is blocked pending human review."


def _blocked_gate_next_action(
    gate: WorkItemApprovalGate,
    status: ApprovalQueueStatus,
) -> WorkItemNextAction:
    if status == ApprovalQueueStatus.REVISE:
        return WorkItemNextAction(
            action="revise_approval_artifact",
            description=(
                f"Revise the artifact for the {gate.scope} gate, then request approval again."
            ),
            requires_approval=True,
        )
    return WorkItemNextAction(
        action="resolve_rejected_approval",
        description=(
            f"The {gate.scope} gate was rejected. Create a new approval request or archive "
            "the WorkItem."
        ),
        requires_approval=True,
    )


def _approved_gate_next_action(
    gate: WorkItemApprovalGate,
    *,
    approval_id: str,
) -> WorkItemNextAction:
    if gate.scope == "external_use":
        return WorkItemNextAction(
            action="external_use_approval_recorded",
            description=(
                "External-use approval was recorded for this specific gate only. "
                "No email send, Slack post, publication, scheduling action, or live draft "
                "creation was performed."
            ),
            command_hint=f"approval gate {approval_id} approved",
        )
    if gate.scope == "drafting":
        return WorkItemNextAction(
            action="continue_after_drafting_approval",
            description="Drafting-context approval was recorded for this specific gate.",
            command_hint=f"approval gate {approval_id} approved",
        )
    return WorkItemNextAction(
        action=f"{gate.scope}_approval_recorded",
        description=f"Approval was recorded for the {gate.scope} gate only.",
        command_hint=f"approval gate {approval_id} approved",
    )


def derive_case_status(work_item: WorkItem) -> WorkItemStatus:
    if work_item.status == WorkItemStatus.ARCHIVED:
        return WorkItemStatus.ARCHIVED
    if any(not blocker.resolved for blocker in work_item.blockers):
        return WorkItemStatus.BLOCKED
    if any(gate.required and gate.state == "pending" for gate in work_item.approval_gates):
        return WorkItemStatus.NEEDS_APPROVAL
    if work_item.next_action is not None:
        return WorkItemStatus.IN_PROGRESS
    if work_item.artifact_refs:
        return WorkItemStatus.DONE
    return WorkItemStatus.NEEDS_CONTEXT


def build_research_context(work_item: WorkItem) -> dict[str, Any]:
    return _context_pack_payload(build_research_context_pack(work_item))


def build_opportunity_context(work_item: WorkItem) -> dict[str, Any]:
    return _context_pack_payload(build_opportunity_context_pack(work_item))


def build_outreach_context(work_item: WorkItem) -> dict[str, Any]:
    return _context_pack_payload(build_outreach_context_pack(work_item))


def build_gmail_context(work_item: WorkItem) -> dict[str, Any]:
    return _context_pack_payload(build_gmail_context_pack(work_item))


def build_context_pack_for_route(
    work_item: WorkItem,
    route: WorkItemRoute | str,
    *,
    store: SQLiteStore | None = None,
) -> ContextPack:
    """Build the typed WorkItem-derived context pack for a specialist route."""

    resolved = _coerce_route(route)
    if resolved == WorkItemRoute.BUSINESS_RESEARCH_ANALYST:
        return hydrate_context_pack_memory(build_research_context_pack(work_item), work_item, store)
    if resolved == WorkItemRoute.OPPORTUNITY_SCOUT:
        return hydrate_context_pack_memory(build_opportunity_context_pack(work_item), work_item, store)
    if resolved == WorkItemRoute.OUTREACH_COMPOSER:
        return hydrate_context_pack_memory(build_outreach_context_pack(work_item), work_item, store)
    if resolved == WorkItemRoute.GMAIL_TRIAGE:
        return hydrate_context_pack_memory(build_gmail_context_pack(work_item), work_item, store)
    return hydrate_context_pack_memory(build_research_context_pack(work_item), work_item, store)


def build_research_context_pack(work_item: WorkItem) -> ResearchContextPack:
    ready = research_ready(work_item)
    gates = [_gate("research_target_readiness", ready)]
    source_summary = _source_bundle_summary(work_item.sources, work_item.artifact_refs)
    return ResearchContextPack(
        work_item_id=work_item.id,
        current_status=work_item.status,
        target=work_item.target,
        request_text=work_item.request_text,
        approved_facts=_approved_facts(work_item),
        source_refs=work_item.sources[:12],
        retrieved_sources=work_item.sources[:12],
        selected_artifacts=selected_artifacts(work_item),
        blockers=_open_blockers(work_item),
        approval_gates=work_item.approval_gates,
        allowed_next_action=ready.next_action or work_item.next_action,
        readiness_gates=gates,
        ready=_required_gates_ready(gates),
        can_synthesize=_required_gates_ready(gates),
        missing_requirements=_missing_requirements_from_gates(gates),
        limitation_notes=_limitation_notes_from_gates(gates),
        summary=summarize_work_item_for_agent(work_item),
        research_goal=work_item.request_text or work_item.target.name or work_item.target.url,
        source_bundle_summary=source_summary,
        missing_evidence=(
            [] if source_summary else ["No source refs or stored research artifacts yet."]
        ),
    )


def build_opportunity_context_pack(work_item: WorkItem) -> OpportunityContextPack:
    objective_ready = opportunity_objective_clarity(work_item)
    source_ready = source_sufficiency(work_item)
    gates = [
        _gate("opportunity_objective_clarity", objective_ready),
        _gate("source_sufficiency", source_ready, required=False),
    ]
    candidates = selected_artifacts(work_item, "opportunity")
    return OpportunityContextPack(
        work_item_id=work_item.id,
        current_status=work_item.status,
        target=work_item.target,
        request_text=work_item.request_text,
        approved_facts=_approved_facts(work_item),
        source_refs=work_item.sources[:12],
        retrieved_sources=work_item.sources[:12],
        selected_artifacts=selected_artifacts(work_item),
        blockers=_open_blockers(work_item),
        approval_gates=work_item.approval_gates,
        allowed_next_action=objective_ready.next_action or work_item.next_action,
        readiness_gates=gates,
        ready=_required_gates_ready(gates),
        can_synthesize=_required_gates_ready(gates),
        missing_requirements=_missing_requirements_from_gates(gates),
        limitation_notes=_limitation_notes_from_gates(gates),
        summary=summarize_work_item_for_agent(work_item),
        objective=work_item.request_text or work_item.target.name,
        constraints=_metadata_text_list(work_item.target.metadata, "constraints"),
        entity_types=_metadata_text_list(work_item.target.metadata, "entity_types"),
        retrieved_candidates=[
            ref for ref in work_item.artifact_refs if ref.artifact_type == "opportunity"
        ][:12],
        review_candidates=candidates[:5],
        source_sufficiency="sufficient" if source_ready.ready else "missing",
        approval_state=_highest_approval_state([*candidates, *selected_artifacts(work_item)]),
    )


def build_outreach_context_pack(work_item: WorkItem) -> OutreachContextPack:
    recipient_ready = outreach_recipient_readiness(work_item)
    channel_ready = outreach_contact_channel_readiness(work_item)
    claims_ready = approved_claims_readiness(work_item)
    sources_ready = source_sufficiency(work_item)
    research_ready_result = research_sufficiency(work_item)
    external_ready = external_use_ready(work_item)
    gates = [
        _gate("outreach_recipient_readiness", recipient_ready),
        _gate(
            "outreach_contact_channel_readiness",
            channel_ready,
            required=False,
            details=_outreach_contact(
                work_item,
                *_selected_outreach_artifacts(work_item),
            ).model_dump(mode="json"),
        ),
        _gate("approved_claims_readiness", claims_ready),
        _gate("source_sufficiency", sources_ready),
        _gate("research_sufficiency", research_ready_result),
        _gate("external_use_readiness", external_ready, required=False),
    ]
    company_ref = _first_or_none(selected_artifacts(work_item, "company_profile"))
    opportunity_ref = _first_or_none(selected_artifacts(work_item, "opportunity"))
    allowed_claims = _approved_facts(work_item, for_drafting=True)
    return OutreachContextPack(
        work_item_id=work_item.id,
        current_status=work_item.status,
        target=work_item.target,
        request_text=work_item.request_text,
        approved_facts=_approved_facts(work_item),
        source_refs=work_item.sources[:12],
        retrieved_sources=work_item.sources[:12],
        selected_artifacts=selected_artifacts(work_item),
        blockers=_open_blockers(work_item),
        approval_gates=work_item.approval_gates,
        allowed_next_action=_first_next_action(gates) or work_item.next_action,
        readiness_gates=gates,
        ready=_required_gates_ready(gates),
        can_synthesize=_required_gates_ready(gates),
        missing_requirements=_missing_requirements_from_gates(gates),
        limitation_notes=_limitation_notes_from_gates(gates),
        summary=summarize_work_item_for_agent(work_item),
        selected_company_artifact=company_ref,
        selected_opportunity_artifact=opportunity_ref,
        contact=_outreach_contact(work_item, company_ref, opportunity_ref),
        channel=_outreach_channel(work_item, company_ref, opportunity_ref),
        allowed_claims=allowed_claims,
        blocked_claims=[
            fact for fact in work_item.facts if fact not in allowed_claims and fact.approval_state
        ][:12],
        approval_state=_highest_approval_state(
            [ref for ref in [company_ref, opportunity_ref] if ref]
        ),
    )


def build_gmail_context_pack(work_item: WorkItem) -> GmailContextPack:
    thread_ready = gmail_thread_readiness(work_item)
    external_ready = external_use_ready(work_item)
    gates = [
        _gate("gmail_thread_readiness", thread_ready),
        _gate("external_use_readiness", external_ready, required=False),
    ]
    metadata = work_item.target.metadata
    thread_id = str(metadata.get("thread_id") or work_item.target.external_id or "")
    message_id = str(metadata.get("message_id") or "")
    return GmailContextPack(
        work_item_id=work_item.id,
        current_status=work_item.status,
        target=work_item.target,
        request_text=work_item.request_text,
        approved_facts=_approved_facts(work_item),
        source_refs=work_item.sources[:12],
        retrieved_sources=work_item.sources[:12],
        selected_artifacts=selected_artifacts(work_item),
        blockers=_open_blockers(work_item),
        approval_gates=work_item.approval_gates,
        allowed_next_action=thread_ready.next_action or work_item.next_action,
        readiness_gates=gates,
        ready=_required_gates_ready(gates),
        can_synthesize=_required_gates_ready(gates),
        missing_requirements=_missing_requirements_from_gates(gates),
        limitation_notes=_limitation_notes_from_gates(gates),
        summary=summarize_work_item_for_agent(work_item),
        thread_id=thread_id,
        message_id=message_id,
        selected_thread_ids=_metadata_text_list(metadata, "selected_thread_ids") or [thread_id]
        if thread_id
        else [],
        selected_message_ids=_metadata_text_list(metadata, "selected_message_ids") or [message_id]
        if message_id
        else [],
        thread_summary=str(metadata.get("thread_summary") or work_item.title or ""),
        prior_reply_context=str(metadata.get("prior_reply_context") or ""),
        sender=str(metadata.get("sender") or work_item.target.email or ""),
        reply_objective=str(metadata.get("reply_objective") or work_item.request_text or ""),
        risk_flags=_metadata_text_list(metadata, "risk_flags"),
        approval_state=_highest_gate_state(work_item),
    )


def hydrate_context_pack_memory(
    pack: ContextPack,
    work_item: WorkItem,
    store: SQLiteStore | None,
) -> ContextPack:
    """Attach bounded approved prompt-safe memory without changing readiness gates."""

    if store is None:
        return pack
    query = _memory_query(work_item)
    object_keys = _memory_object_keys(work_item)
    relevant = _retrieve_memory_refs(
        store,
        query=query,
        object_keys=object_keys,
        memory_types=_route_memory_types(pack.route),
        limit=5,
    )
    updates: dict[str, Any] = {"relevant_memory_refs": relevant}
    if isinstance(pack, ResearchContextPack):
        updates.update(
            {
                "approved_company_facts": _retrieve_memory_refs(
                    store,
                    query=query,
                    object_keys=object_keys,
                    memory_types=["company_profile_snapshot", "company_fact"],
                    limit=5,
                ),
                "retrieval_performance_notes": _retrieve_memory_refs(
                    store,
                    query=query or "company research retrieval",
                    object_keys=object_keys,
                    memory_types=["retrieval_tool_performance"],
                    limit=3,
                ),
            }
        )
    elif isinstance(pack, OpportunityContextPack):
        updates.update(
            {
                "prior_opportunity_refs": _retrieve_memory_refs(
                    store,
                    query=query,
                    object_keys=object_keys,
                    memory_types=["opportunity_signal", "opportunity_outcome", "workflow_dedup"],
                    limit=5,
                ),
                "retrieval_performance_notes": _retrieve_memory_refs(
                    store,
                    query=query or "opportunity retrieval",
                    object_keys=object_keys,
                    memory_types=["retrieval_tool_performance"],
                    limit=3,
                ),
            }
        )
    elif isinstance(pack, OutreachContextPack):
        updates.update(
            {
                "approved_company_facts": _retrieve_memory_refs(
                    store,
                    query=query,
                    object_keys=object_keys,
                    memory_types=["company_profile_snapshot", "company_fact"],
                    limit=5,
                ),
                "prior_opportunity_refs": _retrieve_memory_refs(
                    store,
                    query=query,
                    object_keys=object_keys,
                    memory_types=["opportunity_signal", "opportunity_outcome", "workflow_dedup"],
                    limit=5,
                ),
                "outreach_style_examples": _retrieve_memory_refs(
                    store,
                    query=query or "outreach style",
                    object_keys=object_keys,
                    memory_types=["email_style_preference", "outreach_example", "human_feedback"],
                    limit=3,
                ),
                "approval_history_refs": _retrieve_memory_refs(
                    store,
                    query=query,
                    object_keys=object_keys,
                    memory_types=["approval_decision"],
                    limit=3,
                ),
            }
        )
    elif isinstance(pack, GmailContextPack):
        updates.update(
            {
                "outreach_style_examples": _retrieve_memory_refs(
                    store,
                    query=query or "reply style",
                    object_keys=object_keys,
                    memory_types=["email_style_preference", "outreach_example", "human_feedback"],
                    limit=3,
                ),
                "approval_history_refs": _retrieve_memory_refs(
                    store,
                    query=query,
                    object_keys=object_keys,
                    memory_types=["approval_decision"],
                    limit=3,
                ),
            }
        )
    return pack.model_copy(update=updates)


def research_ready(work_item: WorkItem) -> ReadinessResult:
    if work_item.target.name or work_item.target.url:
        return ReadinessResult(ready=True)
    return ReadinessResult(
        ready=False,
        blockers=(
            WorkItemBlocker(
                code="missing_research_target",
                message="A company, domain, URL, or research target is required.",
            ),
        ),
        next_action=WorkItemNextAction(
            action="provide_research_target",
            agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            description="Provide the company or research target to investigate.",
        ),
    )


def opportunity_ready(work_item: WorkItem) -> ReadinessResult:
    return opportunity_objective_clarity(work_item)


def opportunity_objective_clarity(work_item: WorkItem) -> ReadinessResult:
    if work_item.request_text.strip() or work_item.target.name:
        return ReadinessResult(ready=True)
    return ReadinessResult(
        ready=False,
        blockers=(
            WorkItemBlocker(
                code="missing_opportunity_objective",
                message="An opportunity scouting objective is required.",
            ),
        ),
        next_action=WorkItemNextAction(
            action="provide_opportunity_objective",
            agent=WorkItemRoute.OPPORTUNITY_SCOUT,
            description="Provide the market, role, grant, company, or opportunity target.",
        ),
    )


def drafting_ready(work_item: WorkItem) -> ReadinessResult:
    checks = [
        outreach_recipient_readiness(work_item),
        approved_claims_readiness(work_item),
        source_sufficiency(work_item),
        research_sufficiency(work_item),
    ]
    if all(check.ready for check in checks):
        return ReadinessResult(ready=True)
    blockers: list[WorkItemBlocker] = []
    for check in checks:
        if not check.ready:
            blockers.extend(check.blockers)
    if blockers:
        return ReadinessResult(
            ready=False,
            blockers=(
                WorkItemBlocker(
                    code="outreach_requires_approved_context",
                    message=(
                        "Outreach drafting requires a selected source-backed company "
                        "profile approved for drafting context, plus a recipient or "
                        "target organization."
                    ),
                ),
            ),
            next_action=next((check.next_action for check in checks if check.next_action), None),
        )
    return ReadinessResult(
        ready=False,
        blockers=(
            WorkItemBlocker(
                code="outreach_requires_approved_context",
                message=(
                    "Outreach drafting requires a selected company profile approved for "
                    "drafting context, plus a recipient or target organization."
                ),
            ),
        ),
        next_action=WorkItemNextAction(
            action="approve_research_context",
            agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            description=(
                "Select and approve a source-backed company profile before drafting outreach."
            ),
            requires_approval=True,
        ),
    )


def external_use_ready(work_item: WorkItem) -> ReadinessResult:
    if any(gate.state == "approved_for_external_use" for gate in work_item.approval_gates):
        return ReadinessResult(ready=True)
    return ReadinessResult(
        ready=False,
        blockers=(
            WorkItemBlocker(
                code="external_use_requires_approval",
                message="External use requires explicit human approval.",
            ),
        ),
        next_action=WorkItemNextAction(
            action="request_external_use_approval",
            description="Request human approval before external use.",
            requires_approval=True,
        ),
    )


def gmail_draft_ready(work_item: WorkItem) -> ReadinessResult:
    return gmail_thread_readiness(work_item)


def outreach_recipient_readiness(work_item: WorkItem) -> ReadinessResult:
    contact = _outreach_contact(work_item, *_selected_outreach_artifacts(work_item))
    if contact.email or contact.name or contact.organization:
        return ReadinessResult(ready=True)
    return ReadinessResult(
        ready=False,
        blockers=(
            WorkItemBlocker(
                code="outreach_recipient_required",
                message="Outreach drafting requires a recipient person or target organization.",
            ),
        ),
        next_action=WorkItemNextAction(
            action="provide_outreach_recipient",
            agent=WorkItemRoute.OUTREACH_COMPOSER,
            description=(
                "Provide a recipient, contact, or target organization before drafting outreach."
            ),
        ),
    )


def outreach_contact_channel_readiness(work_item: WorkItem) -> ReadinessResult:
    """Return whether outreach has a source-backed direct contact channel."""

    contact = _outreach_contact(work_item, *_selected_outreach_artifacts(work_item))
    if contact.email or contact.linkedin_url:
        return ReadinessResult(ready=True)
    return ReadinessResult(
        ready=False,
        blockers=(
            WorkItemBlocker(
                code="outreach_contact_channel_missing",
                message="A source-backed email address or LinkedIn URL is not available yet.",
                severity="warning",
            ),
        ),
        next_action=WorkItemNextAction(
            action="find_outreach_contact_channel",
            agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            description=(
                "Find a source-backed contact email or LinkedIn profile before saving "
                "a Gmail draft or preparing LinkedIn outreach."
            ),
        ),
    )


def approved_claims_readiness(work_item: WorkItem) -> ReadinessResult:
    selected_refs = selected_artifacts(work_item)
    if _approved_facts(work_item, for_drafting=True) or any(
        _artifact_allows_drafting(ref.approval_state) for ref in selected_refs
    ):
        return ReadinessResult(ready=True)
    return ReadinessResult(
        ready=False,
        blockers=(
            WorkItemBlocker(
                code="approved_claims_required",
                message=(
                    "Drafting requires approved facts or selected artifacts approved for drafting."
                ),
            ),
        ),
        next_action=WorkItemNextAction(
            action="approve_claims_for_drafting",
            agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            description="Approve source-backed claims before they can be used in outreach.",
            requires_approval=True,
        ),
    )


def source_sufficiency(work_item: WorkItem) -> ReadinessResult:
    if work_item.sources or any(
        _artifact_has_source_context(ref) for ref in selected_artifacts(work_item)
    ):
        return ReadinessResult(ready=True)
    return ReadinessResult(
        ready=False,
        blockers=(
            WorkItemBlocker(
                code="source_sufficiency_required",
                message="A source ref, retrieval marker, or source-backed artifact is required.",
            ),
        ),
        next_action=WorkItemNextAction(
            action="add_source_backed_context",
            agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            description="Add or select source-backed research context before continuing.",
        ),
    )


def research_sufficiency(work_item: WorkItem) -> ReadinessResult:
    selected_company = selected_artifacts(work_item, "company_profile")
    if selected_company and source_sufficiency(work_item).ready:
        return ReadinessResult(ready=True)
    return ReadinessResult(
        ready=False,
        blockers=(
            WorkItemBlocker(
                code="research_sufficiency_required",
                message="Outreach requires a selected source-backed company profile.",
            ),
        ),
        next_action=WorkItemNextAction(
            action="select_company_research",
            agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            description="Select a source-backed company profile before drafting.",
        ),
    )


def gmail_thread_readiness(work_item: WorkItem) -> ReadinessResult:
    metadata = work_item.target.metadata
    if work_item.kind == WorkItemKind.GMAIL_THREAD and (
        work_item.target.external_id or metadata.get("thread_id") or metadata.get("message_id")
    ):
        return ReadinessResult(ready=True)
    return ReadinessResult(
        ready=False,
        blockers=(
            WorkItemBlocker(
                code="gmail_thread_required",
                message="A Gmail thread identifier is required before Gmail drafting.",
            ),
        ),
    )


def infer_route_for_continue(work_item: WorkItem) -> WorkItemRoute | None:
    if work_item.next_action and work_item.next_action.agent:
        return work_item.next_action.agent
    if selected_artifacts(work_item, "company_profile") and drafting_ready(work_item).ready:
        return WorkItemRoute.OUTREACH_COMPOSER
    return None


def normalize_target_text(text: str, route: WorkItemRoute) -> str:
    """Extract a compact target/topic phrase from a natural-language request."""

    cleaned = " ".join(str(text or "").strip().strip('"').strip("'").split())
    cleaned = re.sub(r"^@?KNI\s+", "", cleaned, flags=re.I)
    if route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST:
        cleaned = re.sub(
            r"^(business\s+research\s+analyst|business\s+agent\s+analyst|account\s+researcher|company\s+research(?:\s+agent)?)\s+",
            "",
            cleaned,
            flags=re.I,
        )
        cleaned = re.sub(
            r"^(please\s+)?(research|analyze|profile|investigate|look\s+into)\s+",
            "",
            cleaned,
            flags=re.I,
        )
    elif route == WorkItemRoute.OPPORTUNITY_SCOUT:
        cleaned = re.sub(
            r"^(opportunity\s+scout|scout\s+agent|scout)\s+",
            "",
            cleaned,
            flags=re.I,
        )
        cleaned = re.sub(
            r"^(please\s+)?(find|identify|scout|search\s+for|look\s+for)\s+",
            "",
            cleaned,
            flags=re.I,
        )
    cleaned = re.sub(r"\s+(please|thanks)$", "", cleaned, flags=re.I)
    return cleaned[:180].strip()


def _context_for_agent(work_item: WorkItem, route: WorkItemRoute) -> dict[str, Any]:
    context = summarize_work_item_for_agent(work_item)
    context["agent"] = route.value
    context["approved_facts"] = [
        fact.model_dump(mode="json")
        for fact in work_item.facts
        if fact.approval_state.startswith("approved")
    ][:12]
    context["source_refs"] = [source.model_dump(mode="json") for source in work_item.sources[:12]]
    context["selected_artifacts"] = [
        artifact.model_dump(mode="json") for artifact in selected_artifacts(work_item)
    ]
    return context


def _context_pack_payload(pack: ContextPack) -> dict[str, Any]:
    payload = pack.model_dump(mode="json")
    payload["agent"] = pack.route.value
    return payload


def _retrieve_memory_refs(
    store: SQLiteStore,
    *,
    query: str,
    object_keys: list[str],
    memory_types: list[str],
    limit: int,
) -> list[MemoryContextRef]:
    found: list[Any] = []
    seen: set[str] = set()

    def add_records(records: list[Any]) -> None:
        for record in records:
            key = _memory_identity(record)
            if key in seen:
                continue
            seen.add(key)
            found.append(record)
            if len(found) >= limit:
                return

    for object_key in object_keys:
        if len(found) >= limit:
            break
        add_records(
            store.retrieve_memory(
                query=query,
                object_key=object_key,
                memory_types=memory_types,
                limit=limit,
                approved_only=True,
                safe_for_prompt=True,
            )
        )
    if len(found) < limit:
        add_records(
            store.retrieve_memory(
                query=query,
                memory_types=memory_types,
                limit=limit,
                approved_only=True,
                safe_for_prompt=True,
            )
        )
    return [_memory_context_ref(record) for record in found[:limit]]


def _memory_context_ref(item: Any) -> MemoryContextRef:
    return MemoryContextRef(
        id=getattr(item, "id", None),
        memory_type=str(getattr(item, "memory_type", "") or ""),
        object_type=str(getattr(item, "object_type", "") or ""),
        object_id=str(getattr(item, "object_id", "") or ""),
        object_key=str(getattr(item, "object_key", "") or ""),
        title=_bounded_memory_text(getattr(item, "title", ""), max_chars=120),
        summary=_bounded_memory_text(getattr(item, "summary", ""), max_chars=320),
        content_summary=_memory_content_summary(getattr(item, "content", {})),
        source_ids=[str(source_id) for source_id in getattr(item, "source_ids", [])[:8]],
        approval_state=str(getattr(getattr(item, "approval_state", ""), "value", getattr(item, "approval_state", ""))),
        confidence=float(getattr(item, "confidence", 0.0) or 0.0),
        created_at=str(getattr(item, "created_at", "") or ""),
    )


def _memory_content_summary(content: Any) -> dict[str, Any]:
    if not isinstance(content, dict):
        return {}
    allowed_keys = (
        "company_name",
        "entity_name",
        "entity_kind",
        "canonical_entity_key",
        "claim_text",
        "claim_type",
        "opportunity_type",
        "priority_score",
        "recommended_next_step",
        "decision",
        "scope",
        "outcome_status",
        "provider",
        "provider_name",
        "success_rate",
        "lessons",
        "preference",
        "style_summary",
        "rationale",
    )
    summary: dict[str, Any] = {}
    for key in allowed_keys:
        if key not in content:
            continue
        value = content[key]
        if isinstance(value, str):
            summary[key] = _bounded_memory_text(value, max_chars=240)
        elif isinstance(value, int | float | bool):
            summary[key] = value
        elif isinstance(value, list):
            summary[key] = [
                _bounded_memory_text(item, max_chars=160)
                for item in value[:5]
                if str(item).strip()
            ]
        elif isinstance(value, dict):
            summary[key] = {
                str(inner_key): _bounded_memory_text(inner_value, max_chars=160)
                for inner_key, inner_value in list(value.items())[:5]
                if str(inner_value).strip()
            }
    return summary


def _memory_query(work_item: WorkItem) -> str:
    parts = [
        work_item.target.name,
        work_item.target.email,
        work_item.target.url,
        work_item.target.external_id,
        work_item.request_text,
        work_item.title,
        *_metadata_query_values(work_item.target.metadata),
        *(artifact.title for artifact in selected_artifacts(work_item)),
    ]
    return " ".join(part for part in (" ".join(str(value or "").split()) for value in parts) if part)[:600]


def _memory_object_keys(work_item: WorkItem) -> list[str]:
    values = [
        work_item.target.name,
        work_item.target.email,
        work_item.target.url,
        work_item.target.external_id,
        work_item.title,
        *(_metadata_query_values(work_item.target.metadata)),
        *(artifact.title for artifact in selected_artifacts(work_item)),
    ]
    keys = [normalize_memory_key(str(value or "")) for value in values if str(value or "").strip()]
    return list(dict.fromkeys(key for key in keys if key))[:12]


def _metadata_query_values(metadata: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in (
        "thread_id",
        "message_id",
        "sender",
        "company_name",
        "organization",
        "recipient",
        "contact_name",
        "opportunity_type",
        "canonical_entity_key",
    ):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return values


def _route_memory_types(route: WorkItemRoute) -> list[str]:
    if route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST:
        return [
            "company_profile_snapshot",
            "company_fact",
            "workflow_dedup",
            "retrieval_tool_performance",
        ]
    if route == WorkItemRoute.OPPORTUNITY_SCOUT:
        return [
            "opportunity_signal",
            "opportunity_outcome",
            "company_profile_snapshot",
            "workflow_dedup",
            "retrieval_tool_performance",
        ]
    if route == WorkItemRoute.OUTREACH_COMPOSER:
        return [
            "company_profile_snapshot",
            "company_fact",
            "opportunity_signal",
            "email_style_preference",
            "outreach_example",
            "human_feedback",
            "approval_decision",
        ]
    if route == WorkItemRoute.GMAIL_TRIAGE:
        return ["email_style_preference", "outreach_example", "human_feedback", "approval_decision"]
    return ["workflow_dedup"]


def _memory_identity(item: Any) -> str:
    item_id = getattr(item, "id", None)
    if item_id:
        return f"id:{item_id}"
    return "|".join(
        [
            str(getattr(item, "memory_type", "") or ""),
            str(getattr(item, "object_key", "") or ""),
            str(getattr(item, "title", "") or ""),
        ]
    )


def _bounded_memory_text(value: Any, *, max_chars: int) -> str:
    text = " ".join(str(value or "").replace("\r\n", "\n").replace("\r", "\n").split())
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."


def _gate(
    name: str,
    readiness: ReadinessResult,
    *,
    required: bool = True,
    details: dict[str, Any] | None = None,
) -> ContextPackReadinessGate:
    return ContextPackReadinessGate(
        name=name,
        ready=readiness.ready,
        required=required,
        blockers=list(readiness.blockers),
        next_action=readiness.next_action,
        details=details or {},
    )


def _required_gates_ready(gates: Iterable[ContextPackReadinessGate]) -> bool:
    return all(gate.ready for gate in gates if gate.required)


def _missing_requirements_from_gates(gates: Iterable[ContextPackReadinessGate]) -> list[str]:
    return _gate_messages(gate for gate in gates if gate.required and not gate.ready)


def _limitation_notes_from_gates(gates: Iterable[ContextPackReadinessGate]) -> list[str]:
    return _gate_messages(gate for gate in gates if not gate.required and not gate.ready)


def _gate_messages(gates: Iterable[ContextPackReadinessGate]) -> list[str]:
    messages: list[str] = []
    for gate in gates:
        for blocker in gate.blockers:
            message = blocker.message.strip()
            if message:
                messages.append(message)
    return list(dict.fromkeys(messages))


def _first_next_action(gates: Iterable[ContextPackReadinessGate]) -> WorkItemNextAction | None:
    for gate in gates:
        if gate.required and not gate.ready and gate.next_action is not None:
            return gate.next_action
    return None


def _open_blockers(work_item: WorkItem) -> list[WorkItemBlocker]:
    return [blocker for blocker in work_item.blockers if not blocker.resolved]


def _approved_facts(work_item: WorkItem, *, for_drafting: bool = False) -> list[WorkItemFact]:
    allowed = {
        ApprovalState.APPROVED_FOR_RESEARCH,
        ApprovalState.APPROVED_FOR_DRAFTING,
        ApprovalState.APPROVED_FOR_EXTERNAL_USE,
        ApprovalState.APPROVED_FOR_SEND,
    }
    if for_drafting:
        allowed = {
            ApprovalState.APPROVED_FOR_DRAFTING,
            ApprovalState.APPROVED_FOR_EXTERNAL_USE,
            ApprovalState.APPROVED_FOR_SEND,
        }
    facts = []
    for fact in work_item.facts:
        try:
            state = normalize_approval_state(fact.approval_state)
        except ValueError:
            continue
        if state in allowed:
            facts.append(fact)
    return facts[:12]


def _source_bundle_summary(
    sources: Iterable[WorkItemSourceRef],
    artifacts: Iterable[WorkItemArtifactRef],
) -> str:
    source_count = len(list(sources))
    source_backed_artifact_count = sum(
        1 for artifact in artifacts if _artifact_has_source_context(artifact)
    )
    parts = []
    if source_count:
        parts.append(f"{source_count} source refs")
    if source_backed_artifact_count:
        parts.append(f"{source_backed_artifact_count} source-backed artifact refs")
    return ", ".join(parts)


def _selected_outreach_artifacts(
    work_item: WorkItem,
) -> tuple[WorkItemArtifactRef | None, WorkItemArtifactRef | None]:
    return (
        _first_or_none(selected_artifacts(work_item, "company_profile")),
        _first_or_none(selected_artifacts(work_item, "opportunity")),
    )


def _first_or_none(values: list[Any]) -> Any | None:
    return values[0] if values else None


def _metadata_text_list(metadata: dict[str, Any], key: str) -> list[str]:
    value = metadata.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _outreach_contact(
    work_item: WorkItem,
    company_ref: WorkItemArtifactRef | None,
    opportunity_ref: WorkItemArtifactRef | None,
) -> OutreachContactContext:
    metadata_sources = [
        work_item.target.metadata,
        company_ref.metadata if company_ref else {},
        opportunity_ref.metadata if opportunity_ref else {},
    ]
    name = _first_metadata_value(metadata_sources, "contact_name", "recipient_name", "name")
    email = _first_metadata_value(metadata_sources, "contact_email", "recipient_email", "email")
    linkedin_url = _first_metadata_value(metadata_sources, "linkedin_url", "contact_linkedin_url")
    organization = (
        work_item.target.name
        or _first_metadata_value(metadata_sources, "organization", "company", "company_name")
        or (company_ref.title if company_ref else "")
    )
    channel = _outreach_channel(work_item, company_ref, opportunity_ref)
    source = "work_item.target"
    if name or email or linkedin_url:
        source = "metadata"
    elif company_ref:
        source = "selected_company_artifact"
    source_ids = _contact_source_ids(metadata_sources, company_ref, opportunity_ref)
    readiness_level, missing = _contact_readiness_level(
        name=name,
        organization=organization,
        email=work_item.target.email or email,
        linkedin_url=linkedin_url,
    )
    return OutreachContactContext(
        name=name,
        organization=organization,
        email=work_item.target.email or email,
        linkedin_url=linkedin_url,
        channel=channel,
        source=source,
        source_ids=source_ids,
        readiness_level=readiness_level,
        missing=missing,
    )


def _outreach_channel(
    work_item: WorkItem,
    company_ref: WorkItemArtifactRef | None,
    opportunity_ref: WorkItemArtifactRef | None,
) -> str:
    metadata_sources = [
        work_item.target.metadata,
        company_ref.metadata if company_ref else {},
        opportunity_ref.metadata if opportunity_ref else {},
    ]
    return _first_metadata_value(metadata_sources, "channel", "outreach_channel") or (
        "email" if work_item.target.email else "unspecified"
    )


def _first_metadata_value(sources: Iterable[dict[str, Any]], *keys: str) -> str:
    for metadata in sources:
        for key in keys:
            value = metadata.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return ""


def _contact_source_ids(
    metadata_sources: Iterable[dict[str, Any]],
    company_ref: WorkItemArtifactRef | None,
    opportunity_ref: WorkItemArtifactRef | None,
) -> list[str]:
    source_ids: list[str] = []
    for metadata in metadata_sources:
        source_ids.extend(_metadata_text_list(metadata, "contact_source_ids"))
        source_ids.extend(_metadata_text_list(metadata, "source_ids"))
    for artifact in (company_ref, opportunity_ref):
        if artifact and artifact.metadata.get("source_id"):
            source_ids.append(str(artifact.metadata["source_id"]))
    return list(dict.fromkeys(source_id for source_id in source_ids if source_id))


def _contact_readiness_level(
    *,
    name: str,
    organization: str,
    email: str,
    linkedin_url: str,
) -> tuple[str, list[str]]:
    missing: list[str] = []
    if not organization:
        missing.append("target organization")
    if not name:
        missing.append("contact name")
    if not email:
        missing.append("contact email")
    if not linkedin_url:
        missing.append("LinkedIn URL")
    if email and name:
        return "email_ready", missing
    if linkedin_url and name:
        return "linkedin_ready", missing
    if email or linkedin_url:
        return "channel_ready", missing
    if name:
        return "person_identified", missing
    if organization:
        return "organization_only", missing
    return "missing", missing


def _artifact_has_source_context(artifact: WorkItemArtifactRef) -> bool:
    metadata = artifact.metadata
    if any(metadata.get(key) for key in ("source_count", "source_refs", "sources", "source_url")):
        return True
    if artifact.artifact_type in {"company_profile", "opportunity"} and (
        artifact.source_agent
        in {
            WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
            WorkItemRoute.OPPORTUNITY_SCOUT.value,
        }
    ):
        return bool(artifact.title or artifact.summary)
    return False


def _highest_approval_state(artifacts: Iterable[WorkItemArtifactRef]) -> str:
    ranking = {
        ApprovalState.PENDING: 0,
        ApprovalState.APPROVED_FOR_RESEARCH: 1,
        ApprovalState.APPROVED_FOR_DRAFTING: 2,
        ApprovalState.APPROVED_FOR_EXTERNAL_USE: 3,
        ApprovalState.APPROVED_FOR_SEND: 4,
        ApprovalState.REJECTED: -1,
        ApprovalState.EXPIRED: -1,
    }
    best_state = ApprovalState.PENDING
    best_rank = ranking[best_state]
    for artifact in artifacts:
        try:
            state = normalize_approval_state(artifact.approval_state)
        except ValueError:
            continue
        rank = ranking[state]
        if rank > best_rank:
            best_state = state
            best_rank = rank
    return best_state.value


def _highest_gate_state(work_item: WorkItem) -> str:
    return _highest_approval_state(
        [
            WorkItemArtifactRef(
                artifact_type="approval_gate",
                artifact_id=gate.approval_id or gate.scope,
                approval_state=gate.state,
            )
            for gate in work_item.approval_gates
        ]
    )


def _artifact_allows_drafting(state: str) -> bool:
    return normalize_approval_state(state) in {
        ApprovalState.APPROVED_FOR_DRAFTING,
        ApprovalState.APPROVED_FOR_EXTERNAL_USE,
        ApprovalState.APPROVED_FOR_SEND,
    }


def _coerce_route(route: WorkItemRoute | str | None) -> WorkItemRoute:
    if isinstance(route, WorkItemRoute):
        return route
    if route:
        try:
            return WorkItemRoute(str(route))
        except ValueError:
            return WorkItemRoute.CLARIFICATION
    return WorkItemRoute.ORCHESTRATOR


def _kind_for_route(route: WorkItemRoute) -> WorkItemKind:
    if route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST:
        return WorkItemKind.COMPANY_RESEARCH
    if route == WorkItemRoute.OPPORTUNITY_SCOUT:
        return WorkItemKind.OPPORTUNITY
    if route == WorkItemRoute.OUTREACH_COMPOSER:
        return WorkItemKind.OUTREACH
    if route == WorkItemRoute.GMAIL_TRIAGE:
        return WorkItemKind.GMAIL_THREAD
    if route == WorkItemRoute.CHIEF_OF_STAFF:
        return WorkItemKind.WEEKLY_SCAN
    return WorkItemKind.WEEKLY_SCAN


def _kind_for_target(route: WorkItemRoute, target: WorkItemTarget) -> WorkItemKind:
    if route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST and target.object_type in {
        "zotero_collection",
        "zotero_article",
        "article_collection",
        "topic",
        "institute",
        "conference",
        "lab",
        "person",
    }:
        return WorkItemKind.RESEARCH_BRIEF
    return _kind_for_route(route)


def _target_for_request(text: str, route: WorkItemRoute) -> WorkItemTarget:
    target = normalize_target_text(text, route)
    object_type = (
        "topic"
        if route == WorkItemRoute.OPPORTUNITY_SCOUT
        else "slack_channel"
        if route == WorkItemRoute.CHIEF_OF_STAFF
        else "company"
    )
    if (
        route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
        and looks_like_zotero_article_request(text)
    ):
        target = extract_zotero_article_query(text) or target
        object_type = "zotero_article"
    elif (
        route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
        and looks_like_zotero_collection_request(text)
    ):
        target = extract_zotero_collection_hint(text) or target
        object_type = "zotero_collection"
    url_match = re.search(r"https?://\S+", text or "")
    return WorkItemTarget(
        name=target if not url_match else target.replace(url_match.group(0), "").strip(),
        url=url_match.group(0).rstrip(".,)") if url_match else "",
        object_type=object_type,
    )


def _title_for_request(text: str, route: WorkItemRoute) -> str:
    target = normalize_target_text(text, route)
    if route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST:
        if looks_like_zotero_article_request(text):
            target = extract_zotero_article_query(text) or target
            return f"Research brief: {target or 'Untitled target'}"
        if looks_like_zotero_collection_request(text):
            target = extract_zotero_collection_hint(text) or target
            return f"Research brief: {target or 'Untitled target'}"
        return f"Research: {target or 'Untitled target'}"
    if route == WorkItemRoute.OPPORTUNITY_SCOUT:
        return f"Opportunity scan: {target or 'Untitled objective'}"
    if route == WorkItemRoute.OUTREACH_COMPOSER:
        return f"Outreach: {target or 'Untitled draft'}"
    if route == WorkItemRoute.GMAIL_TRIAGE:
        return "Gmail triage"
    if route == WorkItemRoute.CHIEF_OF_STAFF:
        return f"Chief of Staff: {target or 'operations request'}"
    return target or "WorkItem"


def source_refs_from_sources(sources: Iterable[Any], *, limit: int = 8) -> list[Any]:
    return list(sources)[:limit]
