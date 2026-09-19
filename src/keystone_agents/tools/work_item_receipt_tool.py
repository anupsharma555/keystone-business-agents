"""Agent-visible read-only inspection of WorkItem stage and tool receipts."""

from __future__ import annotations

from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.receipts.inspection import inspect_work_item_receipts
from keystone_agents.sdk import function_tool
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env


def inspect_work_item_execution_receipts_impl(
    work_item_id: str,
    *,
    database_url: str | None = None,
) -> str:
    """Return verified receipt evidence and the canonical safe resume point."""

    store = SQLiteStore(database_url or database_url_from_env())
    return inspect_work_item_receipts(work_item_id, store=store).model_dump_json()


@function_tool(**keystone_tool_guardrail_kwargs())
def inspect_work_item_execution_receipts(
    work_item_id: str,
) -> str:
    """Inspect one WorkItem's verified receipts and exact safe resume point.

    This is a local, read-only operational diagnostic. It performs no provider
    calls, does not advance the WorkItem, and does not execute the returned next
    action. Supply the exact persisted WorkItem id.
    """

    return inspect_work_item_execution_receipts_impl(work_item_id)


__all__ = [
    "inspect_work_item_execution_receipts",
    "inspect_work_item_execution_receipts_impl",
]
