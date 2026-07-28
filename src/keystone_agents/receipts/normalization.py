"""Normalize bounded provider receipts without exposing provider payloads."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from keystone_agents.schemas.provider_recovery import ProviderMutationReceipt

RECOVERY_SAFE_RECEIPT_KEYS = {
    "status",
    "operation",
    "action",
    "operation_type",
    "provider",
    "provider_system",
    "calendar_id",
    "event_id",
    "record_id",
    "draft_id",
    "document_id",
    "file_id",
    "folder_id",
    "spreadsheet_id",
    "item_key",
    "collection_key",
    "message_id",
    "thread_id",
    "html_link",
    "provider_link",
    "provider_write",
    "verification",
    "dry_run",
    "live",
    "tool_name",
}
JOURNAL_SAFE_RECEIPT_KEYS = {
    "schema_name",
    "status",
    "operation",
    "action",
    "operation_type",
    "provider",
    "provider_system",
    "provider_read",
    "resource",
    "plan_fingerprint",
    "capability_profile_fingerprint",
    "snapshot_fingerprint",
    "completeness",
    "item_count",
    "page_count",
    "bytes_read",
    "continuation_available",
    "attempt_count",
    "retry_count",
    "elapsed_ms",
    "deadline_exceeded",
    "cache_hit",
    "identity_fingerprints",
    "error_codes",
    "calendar_id",
    "event_id",
    "event_reference",
    "record_id",
    "draft_id",
    "document_id",
    "file_id",
    "folder_id",
    "spreadsheet_id",
    "item_key",
    "collection_key",
    "message_id",
    "thread_id",
    "match_count",
    "title",
    "subject",
    "start",
    "end",
    "start_date",
    "start_time",
    "end_time",
    "all_day",
    "timezone",
    "html_link",
    "provider_link",
    "provider_write",
    "events",
    "verification",
    "send_enabled",
    "dry_run",
    "live",
    "error",
}
OBJECT_ID_KEYS = (
    "event_id",
    "record_id",
    "draft_id",
    "document_id",
    "file_id",
    "folder_id",
    "spreadsheet_id",
    "item_key",
    "collection_key",
    "message_id",
    "thread_id",
)
RECOVERY_IDENTITY_KEYS_BY_TOOL_OPERATION = {
    ("zotero_write_test_item", "create"): ("item_key",),
    ("zotero_write_test_item", "update"): ("item_key",),
    ("zotero_delete_test_item", "delete_test_item"): ("item_key",),
    ("zotero_write_test_collection", "create"): ("collection_key",),
    ("zotero_write_test_collection", "update"): ("collection_key",),
    ("zotero_delete_test_collection", "delete_test_collection"): (
        "collection_key",
    ),
    ("google_sheet_create", "create_sheet"): ("spreadsheet_id",),
    ("google_drive_create_folder", "create_folder"): ("folder_id",),
}
RECOVERY_IDENTITY_GUARDED_TOOLS = frozenset(
    tool_name for tool_name, _operation in RECOVERY_IDENTITY_KEYS_BY_TOOL_OPERATION
)


def bounded_value(value: Any, *, depth: int = 0) -> Any:
    """Return a depth-, size-, and type-bounded receipt value."""

    if depth >= 4:
        return "[bounded]"
    if isinstance(value, Mapping):
        return {
            str(key): bounded_value(item, depth=depth + 1)
            for key, item in list(value.items())[:30]
        }
    if isinstance(value, list | tuple):
        return [bounded_value(item, depth=depth + 1) for item in list(value)[:25]]
    if isinstance(value, str):
        return value[:2000]
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)[:500]


def payload_digest(payload: Mapping[str, Any]) -> str:
    """Return the stable digest used by recovery checkpoints."""

    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def normalize_tool_output_receipt(
    tool_name: str,
    output: Any,
) -> dict[str, Any] | None:
    """Return the established bounded journal receipt for one tool result."""

    parsed = output
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            return None
    if hasattr(parsed, "model_dump"):
        parsed = parsed.model_dump(mode="json")
    if not isinstance(parsed, Mapping):
        return None
    bounded = {
        key: bounded_value(parsed[key])
        for key in JOURNAL_SAFE_RECEIPT_KEYS
        if key in parsed
    }
    if not bounded:
        return None
    bounded["tool_name"] = tool_name
    return bounded


def normalize_provider_mutation_receipt(
    raw_receipt: Mapping[str, Any],
) -> ProviderMutationReceipt:
    """Return the existing typed recovery receipt from a bounded provider result."""

    payload = {
        str(key): bounded_value(value)
        for key, value in raw_receipt.items()
        if str(key) in RECOVERY_SAFE_RECEIPT_KEYS
    }
    tool_name = str(payload.get("tool_name") or "").strip()
    operation = str(
        payload.get("operation")
        or payload.get("action")
        or payload.get("operation_type")
        or ""
    ).strip()
    provider = str(
        payload.get("provider") or payload.get("provider_system") or ""
    ).strip()
    identity_keys = RECOVERY_IDENTITY_KEYS_BY_TOOL_OPERATION.get(
        (tool_name, operation),
        () if tool_name in RECOVERY_IDENTITY_GUARDED_TOOLS else OBJECT_ID_KEYS,
    )
    object_id = next(
        (
            str(payload.get(key) or "").strip()
            for key in identity_keys
            if payload.get(key)
        ),
        "",
    )
    provider_link = str(
        payload.get("provider_link") or payload.get("html_link") or ""
    ).strip()
    verification = payload.get("verification")
    verification_passed = (
        verification.get("passed")
        if isinstance(verification, Mapping)
        and isinstance(verification.get("passed"), bool)
        else None
    )
    return ProviderMutationReceipt(
        receipt_id=payload_digest(payload),
        tool_name=tool_name,
        operation=operation,
        provider=provider,
        object_id=object_id,
        provider_link=provider_link,
        verification_passed=verification_passed,
        payload=payload,
    )


__all__ = [
    "JOURNAL_SAFE_RECEIPT_KEYS",
    "OBJECT_ID_KEYS",
    "RECOVERY_IDENTITY_GUARDED_TOOLS",
    "RECOVERY_IDENTITY_KEYS_BY_TOOL_OPERATION",
    "RECOVERY_SAFE_RECEIPT_KEYS",
    "bounded_value",
    "normalize_provider_mutation_receipt",
    "normalize_tool_output_receipt",
    "payload_digest",
]
