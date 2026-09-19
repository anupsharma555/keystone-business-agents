"""Read-only automation inventory tools for the Chief of Staff agent."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from keystone_agents.automation_inventory import (
    build_automation_inventory_report,
    ensure_default_automation_inventory,
)
from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.receipts.inspection import inspect_work_item_receipts
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
    """Inspect active, non-archived WorkItems and their oldest activity safely."""

    active_statuses = {"new", "in_progress", "needs_context", "needs_approval", "blocked"}
    store = _store(database_url)
    bounded_limit = max(1, min(int(limit), 50))
    placeholders = ", ".join("?" for _ in active_statuses)
    with store.managed_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT
                w.work_item_json,
                w.updated_at_utc,
                COALESCE(
                    MAX(NULLIF(e.created_at_utc, '')),
                    NULLIF(w.updated_at_utc, '')
                ) AS last_activity_at_utc
            FROM work_items AS w
            LEFT JOIN work_item_events AS e ON e.work_item_id = w.id
            WHERE w.archived = 0 AND w.status IN ({placeholders})
            GROUP BY w.id
            ORDER BY last_activity_at_utc ASC, w.id ASC
            """,
            tuple(sorted(active_statuses)),
        ).fetchall()
    items_with_activity: list[tuple[dict[str, Any], str]] = []
    for row in rows:
        item = json.loads(str(row["work_item_json"] or "{}"))
        last_activity_at = str(
            row["last_activity_at_utc"] or row["updated_at_utc"] or item.get("updated_at") or ""
        )
        items_with_activity.append((item, last_activity_at))

    oldest: dict[str, Any] | None = None
    if items_with_activity:
        item, last_activity_at = items_with_activity[0]
        age_seconds = _age_seconds(last_activity_at)
        oldest = {
            "id": str(item.get("id") or ""),
            "status": str(item.get("status") or ""),
            "current_route": str(item.get("current_route") or ""),
            "last_agent": str(item.get("last_agent") or ""),
            "last_activity_at_utc": last_activity_at,
            "age_seconds": age_seconds,
            "age_days": round(age_seconds / 86400, 3) if age_seconds is not None else None,
            "blockers": [
                blocker
                for blocker in item.get("blockers", [])
                if not bool(blocker.get("resolved"))
            ],
            "next_action": item.get("next_action"),
        }
    return json.dumps(
        {
            "active_work_item_count": len(items_with_activity),
            "oldest_active_work_item": oldest,
            "active_work_items": [
                {**item, "last_activity_at_utc": last_activity_at}
                for item, last_activity_at in items_with_activity[:bounded_limit]
            ],
            "returned_item_count": min(len(items_with_activity), bounded_limit),
            "inventory_complete": True,
            "inventory_scope": "all active non-archived WorkItems in local SQLite",
            "send_enabled": False,
        },
        ensure_ascii=True,
        sort_keys=True,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def inspect_active_work_item_execution_summary(
    database_url: str | None = None,
    limit: int = 5,
) -> str:
    """Compare oldest verified active WorkItems using one compact read-only view.

    Use this for questions that rank or compare active, non-archived WorkItems by
    their verified execution history. The result is ordered by the most recent
    canonical artifact timestamp, oldest first, and includes the last verified
    stage, blockers, approval state, and exact safe resume disposition. Use the
    single-WorkItem receipt inspection tool instead when an exact WorkItem id is
    already known.
    """

    store = _store(database_url)
    bounded_limit = max(1, min(int(limit), 20))
    active_statuses = {"new", "in_progress", "needs_context", "needs_approval", "blocked"}
    placeholders = ", ".join("?" for _ in active_statuses)
    with store.managed_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT
                w.id,
                MAX(NULLIF(a.created_at_utc, '')) AS last_verified_at_utc
            FROM work_items AS w
            INNER JOIN work_item_artifacts AS a ON a.work_item_id = w.id
            WHERE w.archived = 0 AND w.status IN ({placeholders})
            GROUP BY w.id
            ORDER BY last_verified_at_utc ASC, w.id ASC
            """,
            tuple(sorted(active_statuses)),
        ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        inspection = inspect_work_item_receipts(str(row["id"]), store=store)
        if not inspection.stage_receipts:
            continue
        last_verified_at = str(row["last_verified_at_utc"] or "")
        latest_stage = _latest_verified_stage(inspection.stage_receipts)
        age_seconds = _age_seconds(last_verified_at)
        approval_queue_ids = _pending_approval_ids_for_work_item(
            store,
            work_item_id=inspection.work_item_id,
            artifact_ids=[
                artifact_id
                for receipt in inspection.stage_receipts
                for artifact_id in receipt.artifact_ids
            ],
        )
        items.append(
            {
                "id": inspection.work_item_id,
                "status": inspection.work_item_status,
                "current_route": inspection.current_route,
                "last_agent": inspection.last_agent,
                "last_verified_at_utc": last_verified_at,
                "verification_age_seconds": age_seconds,
                "verification_age_days": (
                    round(age_seconds / 86400, 3) if age_seconds is not None else None
                ),
                "last_verified_stage": latest_stage,
                "open_blocker_codes": inspection.open_blocker_codes,
                "unresolved_approval_scopes": inspection.unresolved_approval_scopes,
                "approval_queued": bool(approval_queue_ids),
                "approval_queue_ids": approval_queue_ids,
                "resume_point": inspection.resume_point.model_dump(mode="json"),
            }
        )
        if len(items) >= bounded_limit:
            break

    return json.dumps(
        {
            "schema_name": "keystone.active_work_item_execution_summary.v1",
            "active_work_item_count": _active_work_item_count(store, active_statuses),
            "verified_active_work_item_count": len(rows),
            "returned_item_count": len(items),
            "ordering": "last_verified_at_utc_ascending",
            "active_work_items": items,
            "inventory_scope": (
                "all active non-archived WorkItems with canonical artifact evidence"
            ),
            "canonical_state": "sqlite",
            "read_only": True,
            "provider_calls_performed": 0,
            "external_write_performed": False,
            "send_enabled": False,
        },
        ensure_ascii=True,
        sort_keys=True,
    )


def _latest_verified_stage(receipts: list[Any]) -> dict[str, Any]:
    with_timestamps = [receipt for receipt in receipts if receipt.event_created_at]
    selected = (
        max(with_timestamps, key=lambda receipt: receipt.event_created_at)
        if with_timestamps
        else receipts[-1]
    )
    return {
        "route": selected.route,
        "stage": selected.stage,
        "status": selected.status,
        "artifact_types": selected.artifact_types,
        "verification_basis": selected.verification_basis,
    }


def _pending_approval_ids_for_work_item(
    store: SQLiteStore,
    *,
    work_item_id: str,
    artifact_ids: list[str],
) -> list[str]:
    object_ids = list(dict.fromkeys(value for value in artifact_ids if value))[:50]
    object_placeholders = ", ".join("?" for _ in object_ids)
    object_clause = f"OR object_id IN ({object_placeholders})" if object_ids else ""
    with store.managed_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT id
            FROM approval_queue
            WHERE approval_status = 'pending'
              AND (
                    json_extract(metadata_json, '$.work_item_id') = ?
                    {object_clause}
              )
            ORDER BY created_at_utc ASC, id ASC
            LIMIT 20
            """,
            (work_item_id, *object_ids),
        ).fetchall()
    return [str(row["id"]) for row in rows]


def _active_work_item_count(store: SQLiteStore, active_statuses: set[str]) -> int:
    placeholders = ", ".join("?" for _ in active_statuses)
    with store.managed_connection() as connection:
        row = connection.execute(
            f"""
            SELECT COUNT(*) AS item_count
            FROM work_items
            WHERE archived = 0 AND status IN ({placeholders})
            """,
            tuple(sorted(active_statuses)),
        ).fetchone()
    return int(row["item_count"] if row is not None else 0)


def _age_seconds(timestamp: str) -> int | None:
    """Return a stable non-negative UTC age for an ISO timestamp."""

    normalized = str(timestamp or "").strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - parsed.astimezone(UTC)
    return max(0, int(delta.total_seconds()))
