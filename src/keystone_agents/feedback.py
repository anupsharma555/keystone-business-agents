"""Local human feedback capture for Keystone agent outputs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from keystone_agents.memory import feedback_memory_item
from keystone_agents.schemas.approval import ApprovalQueueItem
from keystone_agents.schemas.feedback import (
    OUTREACH_REVIEW_FEEDBACK_TAGS,
    SUGGESTED_FEEDBACK_TAGS,
    FeedbackRecord,
    FeedbackResponseResult,
    OperatorFeedbackCaptureFields,
    OperatorFeedbackRequest,
)
from keystone_agents.storage.sqlite_store import SQLiteStore


def save_feedback(
    *,
    object_type: str,
    object_id: str | int,
    approval_id: str | None = None,
    source_agent: str = "",
    review_stage: str = "approval_review",
    rating: str,
    tags: list[str] | None = None,
    risk_flags: list[str] | None = None,
    notes: str = "",
    database_url: str | None = None,
) -> FeedbackRecord:
    """Save one feedback record to local SQLite storage."""

    store = SQLiteStore(database_url)
    row_id = store.save_feedback(
        object_type=object_type,
        object_id=object_id,
        approval_id=approval_id,
        source_agent=source_agent,
        review_stage=review_stage,
        rating=rating,
        tags=tags or [],
        risk_flags=risk_flags or [],
        notes=notes,
    )
    row = next(record for record in store.list_feedback() if record["id"] == row_id)
    return FeedbackRecord.model_validate(row)


def build_operator_feedback_request(
    *,
    object_type: str,
    object_id: str | int,
    source_agent: str,
    review_stage: str = "approval_review",
    approval_question: str = "Should this artifact be approved, revised, or rejected?",
    suggested_tags: list[str] | None = None,
) -> OperatorFeedbackRequest:
    """Build a dry-run-safe prompt asking the operator for lightweight feedback."""

    object_id_text = str(object_id).strip()
    tag_defaults = (
        list(OUTREACH_REVIEW_FEEDBACK_TAGS)
        if object_type == "outreach_draft"
        else list(SUGGESTED_FEEDBACK_TAGS)
    )
    request = OperatorFeedbackRequest(
        object_type=object_type,
        object_id=object_id_text,
        source_agent=source_agent,
        review_stage=review_stage,
        approval_question=approval_question,
        suggested_tags=suggested_tags or tag_defaults,
        capture_fields=OperatorFeedbackCaptureFields(
            object_type=object_type,
            object_id=object_id_text,
            rating="good|okay|poor",
            tags=suggested_tags or tag_defaults,
            notes="optional short operator comments",
        ),
    )
    return request


def list_feedback(
    *,
    object_type: str | None = None,
    rating: str | None = None,
    tag: str | None = None,
    approval_id: str | None = None,
    database_url: str | None = None,
) -> list[FeedbackRecord]:
    """List local feedback records with optional filters."""

    store = SQLiteStore(database_url)
    return [
        FeedbackRecord.model_validate(record)
        for record in store.list_feedback(
            object_type=object_type,
            rating=rating,
            tag=tag,
            approval_id=approval_id,
        )
    ]


def respond_feedback_request(
    *,
    approval_id: str,
    rating: str,
    tags: list[str] | None = None,
    notes: str = "",
    reviewer: str = "",
    save_memory: bool = True,
    database_url: str | None = None,
) -> FeedbackResponseResult:
    """Answer an approval-linked operator feedback request and save reusable memory."""

    store = SQLiteStore(database_url)
    item = store.get_approval_item(approval_id)
    if item is None:
        raise KeyError(f"Approval queue item not found: {approval_id}")

    request = _linked_feedback_request(item)
    feedback = save_feedback(
        object_type=request.object_type,
        object_id=request.object_id,
        approval_id=item.id,
        source_agent=request.source_agent or item.source_agent,
        review_stage=request.review_stage,
        rating=rating,
        tags=tags or [],
        risk_flags=item.risk_flags,
        notes=notes,
        database_url=database_url,
    )
    memory_id = (
        store.save_memory_item(feedback_memory_item(feedback, approval_item=item))
        if save_memory
        else None
    )
    refreshed_item = _link_feedback_to_approval_item(
        store=store,
        item=item,
        feedback=feedback,
        reviewer=reviewer,
        memory_id=memory_id,
    )
    return FeedbackResponseResult(
        approval_id=refreshed_item.id,
        approval_status=refreshed_item.approval_status.value,
        feedback=feedback,
        memory_id=memory_id,
        object_key=feedback_memory_item(feedback, approval_item=refreshed_item).object_key,
    )


def summarize_feedback_by_tag(
    *,
    object_type: str | None = None,
    rating: str | None = None,
    database_url: str | None = None,
) -> dict[str, int]:
    """Count feedback tags for prompt and eval improvement review."""

    return SQLiteStore(database_url).summarize_feedback_by_tag(
        object_type=object_type,
        rating=rating,
    )


def export_feedback_to_markdown(
    *,
    object_type: str | None = None,
    rating: str | None = None,
    tag: str | None = None,
    approval_id: str | None = None,
    database_url: str | None = None,
) -> str:
    """Render feedback records as reviewable markdown."""

    records = list_feedback(
        object_type=object_type,
        rating=rating,
        tag=tag,
        approval_id=approval_id,
        database_url=database_url,
    )
    summary = _summarize_records(records)
    lines = [
        "# Keystone Feedback Export",
        "",
        f"- Records: {len(records)}",
    ]
    if object_type:
        lines.append(f"- Object type filter: {object_type}")
    if rating:
        lines.append(f"- Rating filter: {rating}")
    if tag:
        lines.append(f"- Tag filter: {tag}")
    if approval_id:
        lines.append(f"- Approval ID filter: {approval_id}")

    lines.extend(["", "## Tag Summary", ""])
    if summary:
        lines.extend(f"- {tag_name}: {count}" for tag_name, count in summary.items())
    else:
        lines.append("- none")

    lines.extend(["", "## Feedback", ""])
    if not records:
        lines.append("No feedback records found.")
        return "\n".join(lines)

    for record in records:
        lines.extend(_record_markdown(record))
    return "\n".join(lines)


def _summarize_records(records: list[FeedbackRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        for tag in record.tags:
            counts[tag] = counts.get(tag, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _record_markdown(record: FeedbackRecord) -> list[str]:
    tags = ", ".join(record.tags) if record.tags else "none"
    risk_flags = ", ".join(record.risk_flags) if record.risk_flags else "none"
    notes = record.notes or "none"
    return [
        f"### Feedback {record.id}",
        "",
        f"- Object: {record.object_type} `{record.object_id}`",
        f"- Approval ID: {record.approval_id or 'none'}",
        f"- Source agent: {record.source_agent or 'none'}",
        f"- Review stage: {record.review_stage}",
        f"- Rating: {record.rating}",
        f"- Tags: {tags}",
        f"- Risk flags: {risk_flags}",
        f"- Created: {record.created_at}",
        "",
        "Notes:",
        "",
        _escape_markdown_notes(notes),
        "",
    ]


def _escape_markdown_notes(value: Any) -> str:
    return str(value).replace("\u2014", "-").strip()


def _linked_feedback_request(item: ApprovalQueueItem) -> OperatorFeedbackRequest:
    request = item.metadata.get("operator_feedback_request")
    if not isinstance(request, dict):
        raise ValueError(
            f"Approval queue item {item.id} does not include an operator_feedback_request."
        )
    linked = OperatorFeedbackRequest.model_validate(request)
    object_id = linked.object_id or item.object_id or item.id
    return linked.model_copy(update={"object_id": object_id})


def _link_feedback_to_approval_item(
    *,
    store: SQLiteStore,
    item: ApprovalQueueItem,
    feedback: FeedbackRecord,
    reviewer: str,
    memory_id: int | None,
) -> ApprovalQueueItem:
    metadata = dict(item.metadata)
    existing_ids = metadata.get("operator_feedback_response_ids", [])
    response_ids = (
        [int(value) for value in existing_ids if str(value).strip()]
        if isinstance(existing_ids, list)
        else []
    )
    if feedback.id is not None and feedback.id not in response_ids:
        response_ids.append(feedback.id)
    metadata.update(
        {
            "operator_feedback_response_ids": response_ids,
            "operator_feedback_response_count": len(response_ids),
            "latest_feedback_id": feedback.id,
            "latest_feedback_rating": feedback.rating,
            "latest_feedback_tags": feedback.tags,
            "latest_feedback_at": feedback.created_at,
            "latest_feedback_memory_id": memory_id,
            "latest_feedback_reviewer": reviewer.strip(),
        }
    )
    updated = item.model_copy(
        update={
            "metadata": metadata,
            "updated_at": datetime.now(UTC).replace(microsecond=0),
        }
    )
    store.save_approval_item(updated)
    refreshed = store.get_approval_item(item.id)
    if refreshed is None:
        raise KeyError(f"Approval queue item not found after feedback link update: {item.id}")
    return refreshed
