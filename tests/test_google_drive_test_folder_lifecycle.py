from __future__ import annotations

import scripts.run_google_drive_test_folder_lifecycle as lifecycle


def test_google_drive_folder_lifecycle_dry_run_stops_before_provider_steps(monkeypatch) -> None:
    monkeypatch.setattr(
        lifecycle,
        "google_drive_create_folder_impl",
        lambda *_args, **_kwargs: {
            "status": "dry-run",
            "folder_path": "KNIOps / KBA_TEST_FOLDER_abc",
            "send_enabled": False,
        },
    )

    result = lifecycle.execute_google_drive_test_folder_lifecycle(
        suffix="abc",
        parent_folder="KNIOps",
        approval_reference="approval",
        live=False,
    )

    assert result["status"] == "dry-run"
    assert result["openai_requests"] == 0


def test_google_drive_folder_lifecycle_verifies_rename_and_trash(monkeypatch) -> None:
    monkeypatch.setattr(
        lifecycle,
        "google_drive_create_folder_impl",
        lambda *_args, **_kwargs: {
            "status": "success",
            "folder_id": "folder-123",
            "folder_path": "KNIOps / KBA_TEST_FOLDER_abc",
        },
    )
    names = iter(
        [
            ("KBA_TEST_FOLDER_abc", False),
            ("KBA_TEST_FOLDER_abc_RENAMED", False),
            ("KBA_TEST_FOLDER_abc_RENAMED", True),
        ]
    )

    def fake_metadata(*_args, **_kwargs):
        name, trashed = next(names)
        return {
            "status": "success",
            "file": {
                "name": name,
                "mime_type": "application/vnd.google-apps.folder",
            },
            "trashed": trashed,
        }

    monkeypatch.setattr(lifecycle, "google_drive_get_file_metadata_impl", fake_metadata)
    monkeypatch.setattr(
        lifecycle,
        "google_drive_rename_folder_impl",
        lambda *_args, **_kwargs: {
            "status": "success",
            "folder_id": "folder-123",
            "name": "KBA_TEST_FOLDER_abc_RENAMED",
        },
    )
    monkeypatch.setattr(
        lifecycle,
        "google_drive_remove_folder_impl",
        lambda *_args, **_kwargs: {
            "status": "success",
            "folder_id": "folder-123",
            "name": "KBA_TEST_FOLDER_abc_RENAMED",
            "trashed": True,
        },
    )

    result = lifecycle.execute_google_drive_test_folder_lifecycle(
        suffix="abc",
        parent_folder="KNIOps",
        approval_reference="approval",
        live=True,
    )

    assert result["status"] == "passed"
    assert result["folder_id"] == "folder-123"
    assert result["receipts"]["create_readback"]["passed"] is True
    assert result["receipts"]["rename_readback"]["passed"] is True
    assert result["receipts"]["trash_readback"]["passed"] is True


def test_google_drive_folder_lifecycle_trashes_after_rename_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        lifecycle,
        "google_drive_create_folder_impl",
        lambda *_args, **_kwargs: {"status": "success", "folder_id": "folder-123"},
    )
    metadata_calls = 0

    def fake_metadata(*_args, **_kwargs):
        nonlocal metadata_calls
        metadata_calls += 1
        return {
            "status": "success",
            "file": {
                "name": (
                    "KBA_TEST_FOLDER_abc"
                ),
                "mime_type": "application/vnd.google-apps.folder",
            },
            "trashed": metadata_calls == 2,
        }

    monkeypatch.setattr(lifecycle, "google_drive_get_file_metadata_impl", fake_metadata)
    monkeypatch.setattr(
        lifecycle,
        "google_drive_rename_folder_impl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("rename failed")),
    )
    trash_calls: list[str] = []

    def fake_remove(folder_id, **_kwargs):
        trash_calls.append(folder_id)
        return {"status": "success", "folder_id": folder_id, "trashed": True}

    monkeypatch.setattr(lifecycle, "google_drive_remove_folder_impl", fake_remove)

    result = lifecycle.execute_google_drive_test_folder_lifecycle(
        suffix="abc",
        parent_folder="KNIOps",
        approval_reference="approval",
        live=True,
    )

    assert result["status"] == "failed"
    assert "rename failed" in result["failure"]
    assert trash_calls == ["folder-123"]
    assert result["receipts"]["trash_readback"]["passed"] is True
