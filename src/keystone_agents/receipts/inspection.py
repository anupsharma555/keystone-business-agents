"""Deterministic WorkItem receipt inspection and fail-closed resume decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from keystone_agents.receipts.mutations import receipt_reports_possible_write
from keystone_agents.receipts.normalization import (
    OBJECT_ID_KEYS,
    normalize_provider_mutation_receipt,
)
from keystone_agents.schemas.operational_receipts import (
    OperationalStageReceipt,
    OperationalToolReceipt,
    WorkItemReceiptInspection,
    WorkItemResumePoint,
)
from keystone_agents.schemas.signal_lifecycle import (
    SIGNAL_LIFECYCLE_ARTIFACT_TYPE,
    SignalLifecycleCheckpoint,
    SignalLifecycleStatus,
)
from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemArtifactRef,
    WorkItemEvent,
    WorkItemStatus,
)

_VERIFIED_READ_STATUSES = frozenset(
    {"completed", "empty", "ok", "success", "verified", "verified_present"}
)
_APPROVED_GATE_STATES = frozenset(
    {
        "approved_for_research",
        "approved_for_drafting",
        "approved_for_external_use",
        "approved_for_send",
    }
)
_RECEIPT_KEYS = ("tool_receipts", "preacquired_context_receipts", "receipts")


class WorkItemReceiptReader(Protocol):
    """Minimum storage contract for operational receipt inspection."""

    def get_work_item(self, work_item_id: str) -> WorkItem | None: ...

    def list_work_item_events(self, work_item_id: str) -> list[WorkItemEvent]: ...

    def list_work_item_artifacts(
        self, work_item_id: str
    ) -> list[WorkItemArtifactRef]: ...


def inspect_work_item_receipts(
    work_item_id: str,
    *,
    store: WorkItemReceiptReader,
) -> WorkItemReceiptInspection:
    """Inspect durable receipts without advancing or mutating the WorkItem."""

    cleaned_id = str(work_item_id or "").strip()
    if not cleaned_id:
        raise ValueError("work_item_id is required for receipt inspection")
    work_item = store.get_work_item(cleaned_id)
    if work_item is None:
        return WorkItemReceiptInspection(
            inspection_status="not_found",
            work_item_id=cleaned_id,
            resume_point=WorkItemResumePoint(
                disposition="indeterminate",
                exact=False,
                required=False,
                description="No canonical WorkItem exists for the supplied id.",
                basis=["work_item_not_found"],
            ),
        )

    events = store.list_work_item_events(cleaned_id)
    artifacts = _merged_artifacts(
        work_item.artifact_refs,
        store.list_work_item_artifacts(cleaned_id),
    )
    lifecycle_checkpoints = _signal_lifecycle_checkpoints(artifacts)
    stage_receipts = _stage_receipts(work_item.id, events, artifacts)
    tool_receipts, ignored_count = _tool_receipts(events, artifacts)
    resume_point = _resume_point(
        work_item,
        tool_receipts=tool_receipts,
        lifecycle_checkpoints=lifecycle_checkpoints,
    )
    open_blockers = [blocker for blocker in work_item.blockers if not blocker.resolved]
    unresolved_gates = [
        gate
        for gate in work_item.approval_gates
        if gate.required and str(gate.state or "").strip() not in _APPROVED_GATE_STATES
    ]
    return WorkItemReceiptInspection(
        work_item_id=work_item.id,
        work_item_status=work_item.status.value,
        current_route=work_item.current_route.value,
        last_agent=work_item.last_agent,
        stage_receipts=stage_receipts,
        tool_receipts=tool_receipts,
        ignored_unverified_receipt_count=ignored_count,
        open_blocker_codes=[blocker.code for blocker in open_blockers],
        unresolved_approval_scopes=[gate.scope for gate in unresolved_gates],
        resume_point=resume_point,
    )


def _merged_artifacts(
    *groups: Sequence[WorkItemArtifactRef],
) -> list[WorkItemArtifactRef]:
    merged: list[WorkItemArtifactRef] = []
    seen: set[tuple[str, str]] = set()
    for artifact in (item for group in groups for item in group):
        key = (artifact.artifact_type, artifact.artifact_id)
        if key in seen:
            continue
        seen.add(key)
        merged.append(artifact)
    return merged


def _stage_receipts(
    work_item_id: str,
    events: Sequence[WorkItemEvent],
    artifacts: Sequence[WorkItemArtifactRef],
) -> list[OperationalStageReceipt]:
    artifact_ids_by_route: dict[str, list[str]] = {}
    artifact_types_by_route: dict[str, list[str]] = {}
    for artifact in artifacts:
        if artifact.artifact_type == SIGNAL_LIFECYCLE_ARTIFACT_TYPE:
            continue
        route = str(artifact.source_agent or "").strip()
        if not route:
            continue
        artifact_ids_by_route.setdefault(route, []).append(artifact.artifact_id)
        artifact_types_by_route.setdefault(route, []).append(artifact.artifact_type)

    receipts: list[OperationalStageReceipt] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for event in events:
        if event.event_type != "manager_loop_completed":
            continue
        steps = event.metadata.get("steps") if isinstance(event.metadata, Mapping) else None
        if not isinstance(steps, list):
            continue
        for raw_step in steps:
            if not isinstance(raw_step, Mapping) or raw_step.get("advanced") is not True:
                continue
            route = str(raw_step.get("route") or "").strip()
            raw_types = raw_step.get("artifact_types")
            step_types = [
                str(value).strip()
                for value in raw_types
                if str(value or "").strip()
            ] if isinstance(raw_types, list) else []
            route_artifact_types = set(artifact_types_by_route.get(route, []))
            if (
                not route
                or not step_types
                or not set(step_types).issubset(route_artifact_types)
            ):
                continue
            key = (route, tuple(sorted(step_types)))
            if key in seen:
                continue
            seen.add(key)
            receipts.append(
                OperationalStageReceipt(
                    route=route,
                    status=str(raw_step.get("status") or "").strip(),
                    artifact_types=list(dict.fromkeys(step_types)),
                    artifact_ids=artifact_ids_by_route.get(route, [])[:20],
                    event_type=event.event_type,
                    event_created_at=event.created_at,
                    verification_basis=(
                        "manager_loop_audit_step_and_canonical_work_item_artifact"
                    ),
                )
            )

    for artifact in artifacts:
        checkpoint = _signal_lifecycle_checkpoint(artifact)
        if checkpoint is None:
            continue
        route = f"{checkpoint.source_kind.value}_context_agent"
        if artifact.source_agent != route or checkpoint.work_item_id != work_item_id:
            continue
        expected_context_type = f"{checkpoint.source_kind.value}_context"
        context_artifact = next(
            (
                candidate
                for candidate in artifacts
                if candidate.artifact_type == expected_context_type
                and candidate.source_agent == route
                and str(candidate.metadata.get("signal_lifecycle_trigger_id") or "")
                == checkpoint.trigger_id
            ),
            None,
        )
        if context_artifact is None:
            continue
        for stage in checkpoint.completed_stages:
            key = (route, (f"{artifact.artifact_type}:{stage.value}",))
            if key in seen:
                continue
            seen.add(key)
            receipts.append(
                OperationalStageReceipt(
                    route=route,
                    stage=stage.value,
                    status="completed",
                    artifact_types=[artifact.artifact_type, context_artifact.artifact_type],
                    artifact_ids=[artifact.artifact_id, context_artifact.artifact_id],
                    verification_basis=(
                        "typed_signal_lifecycle_checkpoint_and_route_artifact"
                    ),
                )
            )

    for route, route_types in artifact_types_by_route.items():
        key = (route, tuple(sorted(set(route_types))))
        if key in seen:
            continue
        seen.add(key)
        receipts.append(
            OperationalStageReceipt(
                route=route,
                status="artifact_persisted",
                artifact_types=list(dict.fromkeys(route_types))[:20],
                artifact_ids=list(dict.fromkeys(artifact_ids_by_route[route]))[:20],
                verification_basis="canonical_work_item_artifact",
            )
        )
    return receipts[:30]


def _signal_lifecycle_checkpoint(
    artifact: WorkItemArtifactRef,
) -> SignalLifecycleCheckpoint | None:
    if artifact.artifact_type != SIGNAL_LIFECYCLE_ARTIFACT_TYPE:
        return None
    payload = artifact.metadata.get("checkpoint")
    if not isinstance(payload, Mapping):
        return None
    try:
        return SignalLifecycleCheckpoint.model_validate(payload)
    except ValueError:
        return None


def _signal_lifecycle_checkpoints(
    artifacts: Sequence[WorkItemArtifactRef],
) -> list[SignalLifecycleCheckpoint]:
    return [
        checkpoint
        for artifact in artifacts
        if (checkpoint := _signal_lifecycle_checkpoint(artifact)) is not None
    ]


def _tool_receipts(
    events: Sequence[WorkItemEvent],
    artifacts: Sequence[WorkItemArtifactRef],
) -> tuple[list[OperationalToolReceipt], int]:
    candidates: list[Mapping[str, Any]] = []
    for artifact in artifacts:
        candidates.extend(_receipt_candidates(artifact.metadata))
    for event in events:
        metadata = event.metadata if isinstance(event.metadata, Mapping) else {}
        candidates.extend(_receipt_candidates(metadata))
        artifact_payload = metadata.get("artifact")
        if isinstance(artifact_payload, Mapping):
            nested_metadata = artifact_payload.get("metadata")
            if isinstance(nested_metadata, Mapping):
                candidates.extend(_receipt_candidates(nested_metadata))

    verified: list[OperationalToolReceipt] = []
    ignored = 0
    seen: set[tuple[str, str, str, str]] = set()
    for candidate in candidates:
        receipt = _verified_tool_receipt(candidate)
        if receipt is None:
            ignored += 1
            continue
        key = (receipt.tool_name, receipt.operation, receipt.object_id, receipt.receipt_kind)
        if key in seen:
            continue
        seen.add(key)
        verified.append(receipt)
    return verified[:40], ignored


def _receipt_candidates(metadata: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    candidates: list[Mapping[str, Any]] = []
    for key in _RECEIPT_KEYS:
        values = metadata.get(key)
        if not isinstance(values, list):
            continue
        candidates.extend(value for value in values[:50] if isinstance(value, Mapping))
    return candidates


def _verified_tool_receipt(
    receipt: Mapping[str, Any],
) -> OperationalToolReceipt | None:
    tool_name = str(receipt.get("tool_name") or receipt.get("source") or "").strip()
    operation = str(
        receipt.get("operation")
        or receipt.get("action")
        or receipt.get("operation_type")
        or ""
    ).strip()
    if not tool_name or not operation:
        return None
    provider = str(receipt.get("provider") or receipt.get("provider_system") or "").strip()
    status = str(receipt.get("status") or "").strip().lower()
    verification = receipt.get("verification")
    explicit_verified = bool(
        receipt.get("verified") is True
        or (
            isinstance(verification, Mapping)
            and verification.get("passed") is True
        )
    )
    if receipt_reports_possible_write(receipt):
        try:
            normalized = normalize_provider_mutation_receipt(
                {**dict(receipt), "tool_name": tool_name, "operation": operation}
            )
        except (TypeError, ValueError):
            return None
        if normalized.verification_passed is not True or not normalized.object_id:
            return None
        return OperationalToolReceipt(
            tool_name=tool_name,
            operation=operation,
            provider=provider or normalized.provider,
            status=status,
            receipt_kind="write",
            object_id=normalized.object_id,
            verification_basis="provider_mutation_read_back",
        )
    if not (
        receipt.get("provider_read") is True
        and explicit_verified
        and status in _VERIFIED_READ_STATUSES
    ):
        return None
    object_id = next(
        (
            str(receipt.get(key) or "").strip()
            for key in OBJECT_ID_KEYS
            if str(receipt.get(key) or "").strip()
        ),
        "",
    )
    return OperationalToolReceipt(
        tool_name=tool_name,
        operation=operation,
        provider=provider,
        status=status,
        receipt_kind="read",
        object_id=object_id,
        verification_basis="explicit_verified_provider_read",
    )


def _resume_point(
    work_item: WorkItem,
    *,
    tool_receipts: Sequence[OperationalToolReceipt],
    lifecycle_checkpoints: Sequence[SignalLifecycleCheckpoint],
) -> WorkItemResumePoint:
    verified_writes = [
        receipt for receipt in tool_receipts if receipt.receipt_kind == "write"
    ]
    owned_lifecycle = next(
        (
            checkpoint
            for checkpoint in reversed(lifecycle_checkpoints)
            if checkpoint.work_item_id == work_item.id
            and f"{checkpoint.source_kind.value}_context_agent"
            == work_item.current_route.value
        ),
        None,
    )
    common = {
        "do_not_repeat_tool_names": list(
            dict.fromkeys(receipt.tool_name for receipt in verified_writes)
        ),
        "do_not_recreate_object_ids": list(
            dict.fromkeys(
                receipt.object_id for receipt in verified_writes if receipt.object_id
            )
        ),
        "do_not_repeat_stages": (
            [stage.value for stage in owned_lifecycle.completed_stages]
            if owned_lifecycle is not None
            else []
        ),
    }
    if work_item.status in {WorkItemStatus.DONE, WorkItemStatus.ARCHIVED}:
        return WorkItemResumePoint(
            disposition="terminal",
            exact=True,
            required=False,
            description="The canonical WorkItem is terminal; no resume is required.",
            basis=[f"work_item_status:{work_item.status.value}"],
            **common,
        )

    open_blockers = [blocker for blocker in work_item.blockers if not blocker.resolved]
    unresolved_gates = [
        gate
        for gate in work_item.approval_gates
        if gate.required and str(gate.state or "").strip() not in _APPROVED_GATE_STATES
    ]
    pending_gates = [
        gate for gate in unresolved_gates if str(gate.state or "").strip() == "pending"
    ]
    denied_gates = [gate for gate in unresolved_gates if gate not in pending_gates]
    next_action = work_item.next_action
    if open_blockers or denied_gates:
        return WorkItemResumePoint(
            disposition="blocked",
            exact=next_action is not None,
            required=True,
            stage=next_action.action if next_action is not None else "resolve_blockers",
            agent=(
                next_action.agent.value
                if next_action is not None and next_action.agent is not None
                else work_item.current_route.value
            ),
            description=(
                next_action.description
                if next_action is not None
                else "Resolve the persisted blockers before resuming execution."
            ),
            command_hint=next_action.command_hint if next_action is not None else "",
            requires_approval=(
                next_action.requires_approval if next_action is not None else False
            ),
            basis=[
                *[f"open_blocker:{blocker.code}" for blocker in open_blockers],
                *[
                    f"unresolved_approval:{gate.scope}:{gate.state}"
                    for gate in denied_gates
                ],
            ],
            **common,
        )
    if pending_gates or (next_action is not None and next_action.requires_approval):
        return WorkItemResumePoint(
            disposition="await_approval",
            exact=next_action is not None,
            required=True,
            stage=next_action.action if next_action is not None else "await_approval",
            agent=(
                next_action.agent.value
                if next_action is not None and next_action.agent is not None
                else work_item.current_route.value
            ),
            description=(
                next_action.description
                if next_action is not None
                else "Resolve the persisted approval gate before resuming execution."
            ),
            command_hint=next_action.command_hint if next_action is not None else "",
            requires_approval=True,
            basis=[
                *[f"pending_approval:{gate.scope}" for gate in pending_gates],
                *(
                    ["next_action_requires_approval"]
                    if next_action is not None and next_action.requires_approval
                    else []
                ),
            ],
            **common,
        )
    if owned_lifecycle is not None:
        if owned_lifecycle.status in {
            SignalLifecycleStatus.DUPLICATE_TRIGGER,
            SignalLifecycleStatus.NO_ACTION,
        }:
            return WorkItemResumePoint(
                disposition="terminal",
                exact=True,
                required=False,
                agent=work_item.current_route.value,
                description=owned_lifecycle.safe_next_action,
                basis=[f"signal_lifecycle_status:{owned_lifecycle.status.value}"],
                **common,
            )
        if owned_lifecycle.status == SignalLifecycleStatus.COMPLETED:
            return WorkItemResumePoint(
                disposition="resume",
                exact=True,
                required=True,
                stage="finalize_work_item",
                agent=work_item.current_route.value,
                description=(
                    "Finalize canonical WorkItem state without repeating completed signal "
                    "lifecycle stages."
                ),
                basis=["completed_signal_lifecycle_checkpoint"],
                **common,
            )
        if owned_lifecycle.next_stage is not None:
            return WorkItemResumePoint(
                disposition="resume",
                exact=True,
                required=True,
                stage=owned_lifecycle.next_stage.value,
                agent=work_item.current_route.value,
                description=(
                    owned_lifecycle.safe_next_action
                    or f"Resume at {owned_lifecycle.next_stage.value}."
                ),
                basis=[
                    "typed_signal_lifecycle_checkpoint",
                    f"signal_lifecycle_status:{owned_lifecycle.status.value}",
                ],
                **common,
            )
    if next_action is not None:
        return WorkItemResumePoint(
            disposition="resume",
            exact=True,
            required=True,
            stage=next_action.action,
            agent=(
                next_action.agent.value
                if next_action.agent is not None
                else work_item.current_route.value
            ),
            description=next_action.description,
            command_hint=next_action.command_hint,
            requires_approval=False,
            basis=["canonical_work_item_next_action"],
            **common,
        )
    return WorkItemResumePoint(
        disposition="indeterminate",
        exact=False,
        required=True,
        agent=work_item.current_route.value,
        description=(
            "Canonical state has no next_action; inspect or repair the WorkItem before "
            "executing another stage."
        ),
        basis=["missing_canonical_next_action"],
        **common,
    )


__all__ = ["WorkItemReceiptReader", "inspect_work_item_receipts"]
