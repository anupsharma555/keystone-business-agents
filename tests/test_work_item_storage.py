from __future__ import annotations

import json
from pathlib import Path

import pytest

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
from scripts.audit_work_item_integrity import main as audit_work_item_integrity_main


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'work_items.db'}"


def test_pytest_cannot_open_operator_database(monkeypatch: pytest.MonkeyPatch) -> None:
    operator_database = Path(__file__).resolve().parents[1] / "keystone_agents.db"
    monkeypatch.setenv("KEYSTONE_TEST_MODE", "1")

    with pytest.raises(RuntimeError, match="cannot use the operator"):
        SQLiteStore(operator_database)


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
    assert store.audit_work_item_integrity()["status"] == "pass"


def test_sqlite_store_rejects_orphan_work_item_event(tmp_path: Path) -> None:
    store = SQLiteStore(_database_url(tmp_path))

    try:
        store.save_work_item_event(
            "missing-work-item",
            WorkItemEvent(event_type="advance_started", summary="Should not persist."),
        )
    except ValueError as exc:
        assert "missing WorkItem" in str(exc)
        assert "missing-work-item" in str(exc)
    else:
        raise AssertionError("Expected missing WorkItem event save to fail.")


def test_sqlite_store_rejects_orphan_work_item_artifact(tmp_path: Path) -> None:
    store = SQLiteStore(_database_url(tmp_path))

    try:
        store.save_work_item_artifact(
            "missing-work-item",
            WorkItemArtifactRef(
                artifact_type="company_profile",
                artifact_id="42",
                title="Lindus Health",
            ),
        )
    except ValueError as exc:
        assert "missing WorkItem" in str(exc)
        assert "missing-work-item" in str(exc)
    else:
        raise AssertionError("Expected missing WorkItem artifact save to fail.")


def test_work_item_integrity_audit_reports_legacy_orphans_without_repair(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    with store.managed_connection() as connection:
        connection.execute(
            """
            INSERT INTO work_item_events (work_item_id, event_type)
            VALUES ('missing-legacy-item', 'advance_started')
            """
        )
        connection.execute(
            """
            INSERT INTO work_item_artifacts (work_item_id, artifact_type, artifact_id)
            VALUES ('missing-legacy-item', 'company_profile', '42')
            """
        )

    audit = store.audit_work_item_integrity()

    assert audit["status"] == "fail"
    assert audit["orphan_event_count"] == 1
    assert audit["orphan_artifact_count"] == 1
    assert audit["orphan_child_count"] == 2
    assert audit["missing_parent_count"] == 1
    assert audit["missing_work_item_ids"] == ["missing-legacy-item"]
    assert audit["event_groups"][0]["event_type"] == "advance_started"
    assert audit["artifact_groups"][0]["artifact_type"] == "company_profile"
    assert audit["repair_performed"] is False
    assert store.count("work_item_events") == 1
    assert store.count("work_item_artifacts") == 1


def test_work_item_integrity_cli_strict_mode_fails_on_orphans(
    tmp_path: Path,
    capsys,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    with store.managed_connection() as connection:
        connection.execute(
            """
            INSERT INTO work_item_events (work_item_id, event_type)
            VALUES ('missing-cli-item', 'skills_selected')
            """
        )

    exit_code = audit_work_item_integrity_main(
        ["--database-url", database_url, "--strict"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["schema"] == "keystone.work_item_integrity_audit.v1"
    assert payload["status"] == "fail"
    assert payload["missing_work_item_ids"] == ["missing-cli-item"]


def test_storage_tool_lists_work_items(tmp_path: Path) -> None:
    tool = StorageTool(_database_url(tmp_path))
    item = WorkItem(kind=WorkItemKind.OPPORTUNITY, title="Scout opportunities")

    saved = tool.save_work_item(item)
    listed = tool.list_work_items()

    assert saved["id"] == item.id
    assert listed[0]["id"] == item.id
