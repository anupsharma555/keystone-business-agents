from __future__ import annotations

import scripts.run_google_sheet_test_lifecycle as lifecycle


def test_google_sheet_lifecycle_dry_run_stops_before_provider_steps(monkeypatch) -> None:
    monkeypatch.setattr(
        lifecycle,
        "google_sheet_create_impl",
        lambda *_args, **_kwargs: {
            "status": "dry-run",
            "title": "KBA_TEST_SHEET abc",
            "send_enabled": False,
        },
    )

    result = lifecycle.execute_google_sheet_test_lifecycle(
        suffix="abc",
        folder_path="KNIOps",
        approval_reference="approval",
        live=False,
    )

    assert result["status"] == "dry-run"
    assert result["openai_requests"] == 0


def test_google_sheet_lifecycle_verifies_each_step_and_trash(monkeypatch) -> None:
    monkeypatch.setattr(
        lifecycle,
        "google_sheet_create_impl",
        lambda *_args, **_kwargs: {
            "status": "success",
            "spreadsheet_id": "sheet-123",
            "title": "KBA_TEST_SHEET abc",
            "send_enabled": False,
        },
    )
    metadata_calls = 0

    def fake_metadata(*_args, **_kwargs):
        nonlocal metadata_calls
        metadata_calls += 1
        return {
            "status": "success",
            "file": {"name": "KBA_TEST_SHEET abc"},
            "trashed": metadata_calls == 2,
        }

    monkeypatch.setattr(lifecycle, "google_drive_get_file_metadata_impl", fake_metadata)
    monkeypatch.setattr(
        lifecycle,
        "google_sheet_append_rows_impl",
        lambda *_args, **_kwargs: {"status": "success", "spreadsheet_id": "sheet-123"},
    )
    monkeypatch.setattr(
        lifecycle,
        "google_sheet_update_row_impl",
        lambda *_args, **_kwargs: {"status": "success", "row_number": 2},
    )
    monkeypatch.setattr(
        lifecycle,
        "google_sheet_delete_rows_impl",
        lambda *_args, **_kwargs: {"status": "success", "deleted_row_index": 2},
    )
    monkeypatch.setattr(
        lifecycle,
        "google_sheet_trash_impl",
        lambda *_args, **_kwargs: {"status": "success", "trashed": True},
    )
    read_calls = 0

    def fake_read(*_args, **_kwargs):
        nonlocal read_calls
        read_calls += 1
        rows = {
            1: [["record_key", "status", "note"], ["KBA_TEST_ROW_abc", "created", "x"]],
            2: [["record_key", "status", "note"], ["KBA_TEST_ROW_abc", "modified", "y"]],
            3: [["record_key", "status", "note"]],
        }[read_calls]
        return {"status": "success", "rows": rows}

    monkeypatch.setattr(lifecycle, "google_sheet_read_table_impl", fake_read)

    result = lifecycle.execute_google_sheet_test_lifecycle(
        suffix="abc",
        folder_path="KNIOps",
        approval_reference="approval",
        live=True,
    )

    assert result["status"] == "passed"
    assert result["failure"] == ""
    assert result["receipts"]["append_readback"]["passed"] is True
    assert result["receipts"]["update_readback"]["passed"] is True
    assert result["receipts"]["delete_row_readback"]["passed"] is True
    assert result["receipts"]["trash_readback"]["passed"] is True


def test_google_sheet_lifecycle_trashes_sheet_after_midrun_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        lifecycle,
        "google_sheet_create_impl",
        lambda *_args, **_kwargs: {
            "status": "success",
            "spreadsheet_id": "sheet-123",
            "title": "KBA_TEST_SHEET abc",
        },
    )
    metadata_calls = 0

    def fake_metadata(*_args, **_kwargs):
        nonlocal metadata_calls
        metadata_calls += 1
        return {
            "status": "success",
            "file": {"name": "KBA_TEST_SHEET abc"},
            "trashed": metadata_calls == 2,
        }

    monkeypatch.setattr(lifecycle, "google_drive_get_file_metadata_impl", fake_metadata)
    monkeypatch.setattr(
        lifecycle,
        "google_sheet_append_rows_impl",
        lambda *_args, **_kwargs: {"status": "success"},
    )
    monkeypatch.setattr(
        lifecycle,
        "google_sheet_read_table_impl",
        lambda *_args, **_kwargs: {
            "status": "success",
            "rows": [["record_key", "status"], ["KBA_TEST_ROW_abc", "created"]],
        },
    )
    monkeypatch.setattr(
        lifecycle,
        "google_sheet_update_row_impl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("update failed")),
    )
    trash_calls: list[str] = []

    def fake_trash(spreadsheet_id, **_kwargs):
        trash_calls.append(spreadsheet_id)
        return {"status": "success", "trashed": True}

    monkeypatch.setattr(lifecycle, "google_sheet_trash_impl", fake_trash)

    result = lifecycle.execute_google_sheet_test_lifecycle(
        suffix="abc",
        folder_path="KNIOps",
        approval_reference="approval",
        live=True,
    )

    assert result["status"] == "failed"
    assert "update failed" in result["failure"]
    assert trash_calls == ["sheet-123"]
    assert result["receipts"]["trash_readback"]["passed"] is True
