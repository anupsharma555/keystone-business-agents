"""Preserve bounded provider tool receipts across SDK retries.

An SDK turn can complete a provider mutation and then fail while validating the
model's structured final answer.  The model retry must not erase proof of the
already-completed tool call or repeat the same mutation.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from typing import Any

from keystone_agents.receipts.mutations import (
    mutation_tool_names,
    receipt_reports_possible_write,
)
from keystone_agents.receipts.normalization import normalize_tool_output_receipt

_TOOL_RECEIPT_JOURNAL: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "keystone_tool_receipt_journal",
    default=None,
)
_TOOL_RECEIPT_SINK: ContextVar[Callable[[Mapping[str, Any]], Any] | None] = ContextVar(
    "keystone_tool_receipt_sink",
    default=None,
)
def reset_tool_receipt_journal(
    initial_receipts: list[dict[str, Any]] | None = None,
    *,
    receipt_sink: Callable[[Mapping[str, Any]], Any] | None = None,
) -> None:
    """Start one isolated receipt journal for the current SDK execution."""

    _TOOL_RECEIPT_JOURNAL.set([dict(item) for item in (initial_receipts or [])])
    _TOOL_RECEIPT_SINK.set(receipt_sink)


def tool_receipt_journal() -> list[dict[str, Any]]:
    """Return a detached snapshot of receipts captured in the current execution."""

    return [dict(item) for item in (_TOOL_RECEIPT_JOURNAL.get() or [])]


def record_tool_output(tool_name: str, output: Any) -> None:
    """Record one bounded structured provider result without message bodies."""

    receipt = normalize_tool_output_receipt(tool_name, output)
    if receipt is None:
        return
    journal = _TOOL_RECEIPT_JOURNAL.get()
    if journal is None:
        return
    journal.append(receipt)
    receipt_sink = _TOOL_RECEIPT_SINK.get()
    if receipt_sink is not None and receipt_reports_possible_write(receipt):
        receipt_sink(receipt)


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


__all__ = [
    "instrument_agent_tools",
    "mutation_tool_names",
    "record_tool_output",
    "receipt_reports_possible_write",
    "reset_tool_receipt_journal",
    "retry_receipt_context",
    "tool_receipt_journal",
]
