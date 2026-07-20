"""Preserve bounded provider tool receipts across SDK retries.

An SDK turn can complete a provider mutation and then fail while validating the
model's structured final answer.  The model retry must not erase proof of the
already-completed tool call or repeat the same mutation.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

_TOOL_RECEIPT_JOURNAL: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "keystone_tool_receipt_journal",
    default=None,
)
_WRITE_OPERATION = re.compile(
    r"(?:^|_)(?:create|update|delete|write|upload|send|post|trash|modify|label)(?:_|$)",
    re.IGNORECASE,
)
_SAFE_RECEIPT_KEYS = {
    "status",
    "operation",
    "action",
    "operation_type",
    "provider",
    "provider_system",
    "calendar_id",
    "event_id",
    "event_reference",
    "record_id",
    "draft_id",
    "document_id",
    "file_id",
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
    "events",
    "verification",
    "send_enabled",
    "dry_run",
    "live",
    "error",
}


def reset_tool_receipt_journal() -> None:
    """Start one isolated receipt journal for the current SDK execution."""

    _TOOL_RECEIPT_JOURNAL.set([])


def tool_receipt_journal() -> list[dict[str, Any]]:
    """Return a detached snapshot of receipts captured in the current execution."""

    return [dict(item) for item in (_TOOL_RECEIPT_JOURNAL.get() or [])]


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return "[bounded]"
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_value(item, depth=depth + 1)
            for key, item in list(value.items())[:30]
        }
    if isinstance(value, list | tuple):
        return [_bounded_value(item, depth=depth + 1) for item in list(value)[:25]]
    if isinstance(value, str):
        return value[:2000]
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)[:500]


def _receipt_from_tool_output(tool_name: str, output: Any) -> dict[str, Any] | None:
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
        key: _bounded_value(parsed[key])
        for key in _SAFE_RECEIPT_KEYS
        if key in parsed
    }
    if not bounded:
        return None
    bounded["tool_name"] = tool_name
    return bounded


def record_tool_output(tool_name: str, output: Any) -> None:
    """Record one bounded structured provider result without message bodies."""

    receipt = _receipt_from_tool_output(tool_name, output)
    if receipt is None:
        return
    journal = _TOOL_RECEIPT_JOURNAL.get()
    if journal is None:
        return
    journal.append(receipt)


def instrument_agent_tools(agent: Any) -> None:
    """Wrap SDK function tools so outputs survive final-output validation errors."""

    for tool in list(getattr(agent, "tools", []) or []):
        original = getattr(tool, "on_invoke_tool", None)
        name = str(getattr(tool, "name", "") or "").strip()
        if not name or not callable(original):
            continue
        if bool(getattr(tool, "_keystone_receipt_journal_wrapped", False)):
            continue

        async def invoke_and_record(
            context: Any,
            tool_input: str,
            *,
            _original: Any = original,
            _name: str = name,
        ) -> Any:
            result = _original(context, tool_input)
            if inspect.isawaitable(result):
                result = await result
            record_tool_output(_name, result)
            return result

        tool.on_invoke_tool = invoke_and_record
        tool._keystone_receipt_journal_wrapped = True


def receipt_reports_possible_write(receipt: Mapping[str, Any]) -> bool:
    """Return whether a receipt indicates a live mutation may have occurred."""

    if receipt.get("dry_run") is True or str(receipt.get("status") or "") == "dry-run":
        return False
    operation_evidence = " ".join(
        str(receipt.get(key) or "")
        for key in ("operation", "action", "operation_type", "tool_name")
    )
    return bool(_WRITE_OPERATION.search(operation_evidence))


def mutation_tool_names(receipts: list[dict[str, Any]]) -> set[str]:
    """Identify tools that must not be called again during answer repair."""

    return {
        str(receipt.get("tool_name") or "")
        for receipt in receipts
        if receipt_reports_possible_write(receipt)
        and str(receipt.get("tool_name") or "")
    }


def retry_receipt_context(receipts: list[dict[str, Any]]) -> str:
    """Build a compact recovery instruction from already-observed provider truth."""

    compact = json.dumps(receipts[-12:], ensure_ascii=True, sort_keys=True, default=str)
    return (
        "\n\nSDK structured-output recovery context:\n"
        "The prior attempt already produced the provider tool receipts below. "
        "Treat them as authoritative. Do not repeat any mutation represented by these "
        "receipts. You may use a read-only tool if another provider check is needed. "
        "Return the requested typed final answer from the original request and these "
        f"receipts.\nProvider receipts: {compact[:12000]}"
    )
