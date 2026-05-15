"""Read-only automation inventory tools for the Chief of Staff agent."""

from __future__ import annotations

import json
from typing import Any

from keystone_agents.automation_inventory import (
    build_automation_inventory_report,
    ensure_default_automation_inventory,
)
from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.sdk import function_tool
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env


def _store(database_url: str | None = None) -> SQLiteStore:
    store = SQLiteStore(database_url or database_url_from_env())
    ensure_default_automation_inventory(store)
    return store


@function_tool(**keystone_tool_guardrail_kwargs())
def list_automation_specs(database_url: str | None = None, status: str = "all") -> str:
    """List configured Keystone automations."""

    specs = _store(database_url).list_automation_specs(status=status, limit=100)
    return json.dumps(
        {
            "automation_specs": [spec.model_dump(mode="json") for spec in specs],
            "canonical_state": "sqlite",
            "send_enabled": False,
        },
        ensure_ascii=True,
        sort_keys=True,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def list_recent_automation_runs(
    database_url: str | None = None,
    automation_id: str | None = None,
    status: str = "all",
    limit: int = 20,
) -> str:
    """List recent Keystone automation runs."""

    runs = _store(database_url).list_automation_runs(
        automation_id=automation_id,
        status=status,
        limit=limit,
    )
    return json.dumps(
        {
            "automation_runs": [run.model_dump(mode="json") for run in runs],
            "canonical_state": "sqlite",
            "send_enabled": False,
        },
        ensure_ascii=True,
        sort_keys=True,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def list_channel_automation_bindings(
    database_url: str | None = None,
    channel: str | None = None,
) -> str:
    """List Slack/review destinations tied to automations."""

    bindings = _store(database_url).list_automation_channel_bindings(channel=channel, limit=200)
    return json.dumps(
        {
            "channel_bindings": [binding.model_dump(mode="json") for binding in bindings],
            "canonical_state": "sqlite",
            "send_enabled": False,
        },
        ensure_ascii=True,
        sort_keys=True,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def summarize_automation_health(
    database_url: str | None = None,
    channels_json: str = "[]",
    limit: int = 20,
) -> str:
    """Build a Chief of Staff automation inventory report."""

    channels: list[str] = []
    try:
        parsed: Any = json.loads(channels_json or "[]")
        if isinstance(parsed, list):
            channels = [str(channel) for channel in parsed if str(channel).strip()]
    except json.JSONDecodeError:
        channels = [channels_json]
    report = build_automation_inventory_report(
        database_url=database_url,
        channels=channels,
        limit=limit,
    )
    return report.model_dump_json()


@function_tool(**keystone_tool_guardrail_kwargs())
def list_pending_automation_approvals(database_url: str | None = None) -> str:
    """List pending approval queue items relevant to automation review."""

    from keystone_agents.schemas.approval import ApprovalQueueStatus

    items = _store(database_url).list_approval_items(status=ApprovalQueueStatus.PENDING)
    return json.dumps(
        {
            "pending_approval_count": len(items),
            "approval_items": [
                {
                    "id": item.id,
                    "object_type": item.object_type.value,
                    "title": item.title,
                    "source_agent": item.source_agent,
                    "approval_status": item.approval_status.value,
                    "metadata": item.metadata,
                }
                for item in items
            ],
            "send_enabled": False,
        },
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def inspect_active_work_items(database_url: str | None = None, limit: int = 20) -> str:
    """Inspect active WorkItems that may be linked to automations."""

    active_statuses = {"new", "in_progress", "needs_context", "needs_approval", "blocked"}
    items = [
        item
        for item in _store(database_url).list_work_items(status="all", limit=limit)
        if item.status.value in active_statuses
    ]
    return json.dumps(
        {
            "active_work_items": [item.model_dump(mode="json") for item in items],
            "send_enabled": False,
        },
        ensure_ascii=True,
        sort_keys=True,
    )
