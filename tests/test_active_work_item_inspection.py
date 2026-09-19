from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemApprovalGate,
    WorkItemArtifactRef,
    WorkItemBlocker,
    WorkItemKind,
    WorkItemRoute,
    WorkItemStatus,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.automation_inventory_tool import (
    inspect_active_work_item_execution_summary,
    inspect_active_work_items,
)


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'work-items.db'}"


def test_active_work_item_inspection_reports_oldest_across_full_inventory(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    now = datetime.now(UTC)
    for index in range(25):
        timestamp = (now - timedelta(days=30 - index)).isoformat()
        store.save_work_item(
            WorkItem(
                id=f"wi_{index:02d}",
                kind=WorkItemKind.RESEARCH_BRIEF,
                status=WorkItemStatus.IN_PROGRESS,
                title=f"Item {index}",
                updated_at=timestamp,
                )
            )
        with store.managed_connection() as connection:
            connection.execute(
                "UPDATE work_items SET updated_at_utc = ? WHERE id = ?",
                (timestamp, f"wi_{index:02d}"),
            )

    payload = json.loads(inspect_active_work_items(database_url=database_url, limit=5))

    assert payload["active_work_item_count"] == 25
    assert payload["returned_item_count"] == 5
    assert payload["oldest_active_work_item"]["id"] == "wi_00"
    assert payload["oldest_active_work_item"]["age_days"] >= 29
    assert payload["inventory_complete"] is True
    assert payload["send_enabled"] is False


def test_active_work_item_inspection_uses_newest_event_as_activity(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    old = (datetime.now(UTC) - timedelta(days=20)).isoformat()
    recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    store.save_work_item(
        WorkItem(
            id="wi_eventful",
            kind=WorkItemKind.OPPORTUNITY,
            status=WorkItemStatus.BLOCKED,
            title="Eventful",
            updated_at=old,
        )
    )
    with store.managed_connection() as connection:
        connection.execute(
            "UPDATE work_items SET updated_at_utc = ? WHERE id = ?",
            (old, "wi_eventful"),
        )
        connection.execute(
            """
            INSERT INTO work_item_events
                (work_item_id, event_type, actor, summary, created_at_utc,
                 created_at_et, created_date_et)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "wi_eventful",
                "reviewed",
                "test",
                "Recent review",
                recent,
                recent,
                recent[:10],
            ),
        )

    payload = json.loads(inspect_active_work_items(database_url=database_url))

    assert payload["oldest_active_work_item"]["id"] == "wi_eventful"
    assert payload["oldest_active_work_item"]["age_days"] < 2


def test_active_execution_summary_is_compact_and_orders_verified_items(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    now = datetime.now(UTC)
    old = (now - timedelta(days=12)).isoformat()
    recent = (now - timedelta(days=2)).isoformat()

    for item_id, artifact_time in (("wi_old", old), ("wi_recent", recent)):
        artifact = WorkItemArtifactRef(
            artifact_type="company_profile",
            artifact_id=f"artifact_{item_id}",
            source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        )
        item = WorkItem(
            id=item_id,
            kind=WorkItemKind.COMPANY_RESEARCH,
            status=WorkItemStatus.IN_PROGRESS,
            title=item_id,
            current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            artifact_refs=[artifact],
            blockers=(
                [WorkItemBlocker(code="source_review_needed", message="Review sources")]
                if item_id == "wi_old"
                else []
            ),
            approval_gates=(
                [WorkItemApprovalGate(scope="external_use")]
                if item_id == "wi_old"
                else []
            ),
        )
        store.save_work_item(item)
        artifact_row_id = store.save_work_item_artifact(item.id, artifact)
        with store.managed_connection() as connection:
            connection.execute(
                "UPDATE work_item_artifacts SET created_at_utc = ? WHERE id = ?",
                (artifact_time, artifact_row_id),
            )

    store.save_work_item(
        WorkItem(
            id="wi_without_receipt",
            kind=WorkItemKind.RESEARCH_BRIEF,
            status=WorkItemStatus.IN_PROGRESS,
            title="No verified execution",
        )
    )

    raw = inspect_active_work_item_execution_summary(
        database_url=database_url,
        limit=1,
    )
    payload = json.loads(raw)

    assert len(raw) < 5_000
    assert payload["active_work_item_count"] == 3
    assert payload["verified_active_work_item_count"] == 2
    assert payload["returned_item_count"] == 1
    assert payload["active_work_items"][0]["id"] == "wi_old"
    assert payload["active_work_items"][0]["open_blocker_codes"] == [
        "source_review_needed"
    ]
    assert payload["active_work_items"][0]["unresolved_approval_scopes"] == [
        "external_use"
    ]
    assert payload["active_work_items"][0]["last_verified_stage"]["status"] == (
        "artifact_persisted"
    )
    assert "artifact_refs" not in payload["active_work_items"][0]
