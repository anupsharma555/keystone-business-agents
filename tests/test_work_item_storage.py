from __future__ import annotations

from pathlib import Path

from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemArtifactRef,
    WorkItemEvent,
    WorkItemKind,
    WorkItemRoute,
    WorkItemStatus,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.storage_tool import StorageTool


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'work_items.db'}"


def test_sqlite_store_persists_work_item_events_and_artifacts(tmp_path: Path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        status=WorkItemStatus.IN_PROGRESS,
        title="Research Lindus",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
    )
    artifact = WorkItemArtifactRef(
        artifact_type="company_profile",
        artifact_id=42,
        source_agent="business_research_analyst",
        approval_state="approved_for_research",
        title="Lindus Health",
    )

    assert store.save_work_item(item) == item.id
    event_id = store.save_work_item_event(
        item.id,
        WorkItemEvent(event_type="created", summary="Created WorkItem."),
    )
    artifact_id = store.save_work_item_artifact(item.id, artifact)

    loaded = store.get_work_item(item.id)
    assert loaded is not None
    assert loaded.title == "Research Lindus"
    assert loaded.status == WorkItemStatus.IN_PROGRESS
    assert event_id > 0
    assert store.list_work_item_events(item.id)[0].event_type == "created"
    assert artifact_id > 0
    assert store.list_work_item_artifacts(item.id)[0].artifact_id == "42"


def test_storage_tool_lists_work_items(tmp_path: Path) -> None:
    tool = StorageTool(_database_url(tmp_path))
    item = WorkItem(kind=WorkItemKind.OPPORTUNITY, title="Scout opportunities")

    saved = tool.save_work_item(item)
    listed = tool.list_work_items()

    assert saved["id"] == item.id
    assert listed[0]["id"] == item.id
