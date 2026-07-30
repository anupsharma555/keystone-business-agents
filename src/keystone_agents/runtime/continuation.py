"""Project verified provider receipts into bounded continuation identities."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from keystone_agents.schemas.execution_request import ContinuationObjectReference


def attach_verified_continuation_objects(
    payload: dict[str, Any],
    receipts: list[Mapping[str, Any]],
) -> tuple[ContinuationObjectReference, ...]:
    """Attach exact identities only from provider-verified internal receipts."""

    continuation_objects = verified_object_references_from_receipts(receipts)
    if continuation_objects:
        payload["continuation_objects"] = [
            reference.model_dump(mode="json") for reference in continuation_objects
        ]
    return continuation_objects


def verified_object_references_from_receipts(
    receipts: list[Mapping[str, Any]],
) -> tuple[ContinuationObjectReference, ...]:
    """Return unique provider-neutral references from verified receipts."""

    references: list[ContinuationObjectReference] = []
    for receipt in receipts:
        if not _receipt_verification_passed(receipt):
            continue
        reference = _verified_object_reference_from_receipt(receipt)
        if reference is not None and reference not in references:
            references.append(reference)
    return tuple(references[:8])


def _receipt_verification_passed(receipt: Mapping[str, Any]) -> bool:
    verification = receipt.get("verification")
    if isinstance(verification, Mapping):
        return verification.get("passed") is True
    if (
        receipt.get("provider_read") is True
        and str(receipt.get("status") or "").strip().lower() == "success"
        and any(
            str(receipt.get(key) or "").strip()
            for key in (
                "selected_item_key",
                "item_key",
                "event_id",
                "document_id",
                "record_id",
                "draft_id",
                "message_id",
                "thread_id",
            )
        )
    ):
        return True
    return bool(
        receipt.get("provider_verification") == "passed"
        and receipt.get("content_verified") is True
    )


def _verified_object_reference_from_receipt(
    receipt: Mapping[str, Any],
) -> ContinuationObjectReference | None:
    operation = str(
        receipt.get("operation")
        or receipt.get("action")
        or receipt.get("operation_type")
        or receipt.get("status")
        or ""
    ).strip().lower()
    verification = receipt.get("verification")
    verification_mapping = (
        verification if isinstance(verification, Mapping) else {}
    )
    deleted = bool(
        re.search(r"(?:^|_)(?:delete|deleted|trash|trashed|remove)(?:_|$)", operation)
        or receipt.get("trashed") is True
        or any(
            verification_mapping.get(key) is True
            for key in (
                "draft_absent_after_cleanup",
                "record_absent_after_cleanup",
                "note_absent_after_cleanup",
                "item_absent_after_cleanup",
                "document_trashed_after_cleanup",
                "record_absent_after",
                "item_absent_after",
            )
        )
    )

    event_id = str(receipt.get("event_id") or "").strip()
    if event_id:
        return ContinuationObjectReference(
            provider_system="google_calendar",
            object_type="calendar_event",
            object_id=event_id,
            display_name=str(
                receipt.get("title") or receipt.get("summary") or ""
            ).strip(),
            effective_date=str(
                receipt.get("display_start_date")
                or receipt.get("start_date")
                or ""
            ).strip(),
            lifecycle_state="deleted" if deleted else "active",
            verification_status="verified",
            provider_scope=_bounded_provider_scope(
                {
                    **receipt,
                    "start_time": (
                        receipt.get("display_start_time")
                        or receipt.get("start_time")
                    ),
                    "end_time": (
                        receipt.get("display_end_time") or receipt.get("end_time")
                    ),
                },
                ("calendar_id", "start_time", "end_time"),
            ),
        )

    document_id = str(receipt.get("document_id") or "").strip()
    if document_id:
        return ContinuationObjectReference(
            provider_system="google_drive",
            object_type="google_document",
            object_id=document_id,
            display_name=str(receipt.get("title") or "").strip(),
            lifecycle_state="deleted" if deleted else "active",
            verification_status="verified",
            provider_scope=_bounded_provider_scope(
                receipt,
                ("folder_path", "google_account"),
            ),
        )

    record_id = str(receipt.get("record_id") or "").strip()
    table = str(receipt.get("table") or "").strip()
    if record_id and table:
        return ContinuationObjectReference(
            provider_system="airtable",
            object_type="airtable_record",
            object_id=record_id,
            display_name=str(
                receipt.get("display_name")
                or receipt.get("title")
                or receipt.get("name")
                or ""
            ).strip(),
            lifecycle_state="deleted" if deleted else "active",
            verification_status="verified",
            provider_scope=_bounded_provider_scope(
                receipt,
                ("base_alias", "table"),
            ),
        )

    item_key = str(
        receipt.get("item_key") or receipt.get("selected_item_key") or ""
    ).strip()
    if item_key:
        is_note = bool(
            str(receipt.get("parent_item_key") or "").strip()
            or str(receipt.get("required_marker") or "").strip()
            or re.search(r"(?:^|_)(?:note|test_note)(?:_|$)", operation)
        )
        return ContinuationObjectReference(
            provider_system="zotero",
            object_type="zotero_note" if is_note else "zotero_item",
            object_id=item_key,
            display_name=str(
                receipt.get("selected_item_title")
                or receipt.get("title")
                or receipt.get("required_marker")
                or ""
            ).strip(),
            lifecycle_state="deleted" if deleted else "active",
            verification_status="verified",
            provider_scope=_bounded_provider_scope(
                receipt,
                ("library_id", "library_type", "parent_item_key"),
            ),
        )

    draft_id = str(receipt.get("draft_id") or "").strip()
    message_id = str(receipt.get("message_id") or "").strip()
    thread_id = str(receipt.get("thread_id") or "").strip()
    if draft_id or message_id or thread_id:
        sent = bool(
            receipt.get("sent") is True
            or re.search(r"(?:^|_)(?:send|sent)(?:_|$)", operation)
        )
        object_type = (
            "gmail_message"
            if sent and message_id
            else "gmail_thread"
            if sent and thread_id
            else "gmail_draft"
            if draft_id
            else "gmail_message"
            if message_id
            else "gmail_thread"
        )
        object_id = (
            message_id or thread_id
            if sent
            else draft_id or message_id or thread_id
        )
        provider_scope = _bounded_provider_scope(receipt, ("gmail_account",))
        if thread_id and object_type != "gmail_thread":
            provider_scope["thread_id"] = thread_id
        if message_id and object_type == "gmail_draft":
            provider_scope["message_id"] = message_id
        if draft_id and sent:
            provider_scope["draft_id"] = draft_id
        return ContinuationObjectReference(
            provider_system="gmail",
            object_type=object_type,
            object_id=object_id,
            display_name=str(
                receipt.get("subject") or receipt.get("title") or ""
            ).strip(),
            lifecycle_state="deleted" if deleted else "active",
            verification_status="verified",
            provider_scope=provider_scope,
        )
    return None


def _bounded_provider_scope(
    receipt: Mapping[str, Any],
    keys: tuple[str, ...],
) -> dict[str, str]:
    return {
        key: str(receipt.get(key) or "").strip()
        for key in keys
        if str(receipt.get(key) or "").strip()
    }


__all__ = [
    "attach_verified_continuation_objects",
    "verified_object_references_from_receipts",
]
