"""Classify provider receipt operations through one fail-closed boundary."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_MUTATION_OPERATION = re.compile(
    r"(?:^|_)(?:append|archive|attach|create|delete|label|lifecycle|link|"
    r"modify|move|post|publish|reconcile|remove|rename|replace|save|send|share|"
    r"trash|update|upload|write)(?:_|$)",
    re.IGNORECASE,
)

# Preserve existing exact context-agent classifications whose names do not
# necessarily contain a mutation verb.
_EXACT_MUTATION_OPERATIONS = {
    "apply_gmail_labels",
    "extract_slide_copy",
    "mark_important",
    "mark_not_important",
    "mark_read",
    "mark_unread",
    "restore",
    "star",
    "unarchive",
    "unstar",
}


def operation_is_mutation(value: object) -> bool:
    """Return whether one normalized operation name represents a mutation."""

    operation = "_".join(str(value or "").strip().lower().replace("-", "_").split())
    return bool(
        operation
        and (
            operation in _EXACT_MUTATION_OPERATIONS
            or _MUTATION_OPERATION.search(operation)
        )
    )


def receipt_reports_possible_write(receipt: Mapping[str, Any]) -> bool:
    """Return whether a bounded receipt indicates a mutation may have occurred."""

    status = str(receipt.get("status") or "").strip().lower().replace("_", "-")
    if receipt.get("dry_run") is True or status == "dry-run":
        return False
    if receipt.get("provider_write") is True:
        return True
    mutation_operation = any(
        operation_is_mutation(receipt.get(key))
        for key in ("operation", "action", "operation_type", "tool_name")
    )
    if mutation_operation:
        return True
    explicit_read = bool(
        receipt.get("provider_read") is True
        or str(receipt.get("operation") or "").strip().lower()
        in {"read", "search", "query", "list", "get", "inspect", "retrieve", "verify"}
    )
    if explicit_read and receipt.get("provider_write") is not True:
        return False
    return bool(str(receipt.get("approval_reference") or "").strip())


def mutation_tool_names(receipts: Sequence[Mapping[str, Any]]) -> set[str]:
    """Identify tools that must not be called again during answer repair."""

    return {
        str(receipt.get("tool_name") or "").strip()
        for receipt in receipts
        if receipt_reports_possible_write(receipt)
        and str(receipt.get("tool_name") or "").strip()
    }


__all__ = [
    "mutation_tool_names",
    "operation_is_mutation",
    "receipt_reports_possible_write",
]
