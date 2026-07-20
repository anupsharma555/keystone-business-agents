from __future__ import annotations

import keystone_agents.tools.internal_data_tools as data_tools
import scripts.run_google_doc_test_lifecycle as lifecycle


def test_google_doc_lifecycle_dry_run_stops_before_provider_steps(monkeypatch) -> None:
    monkeypatch.setattr(
        lifecycle,
        "google_doc_write_impl",
        lambda *_args, **_kwargs: {
            "status": "dry-run",
            "title": "KBA_TEST_DOC_abc",
            "send_enabled": False,
        },
    )

    result = lifecycle.execute_google_doc_test_lifecycle(
        suffix="abc",
        folder_path="KNIOps",
        approval_reference="approval",
        live=False,
    )

    assert result["status"] == "dry-run"
    assert result["openai_requests"] == 0


def test_google_doc_lifecycle_verifies_create_update_and_trash(monkeypatch) -> None:
    write_calls = 0

    def fake_write(*_args, **_kwargs):
        nonlocal write_calls
        write_calls += 1
        return {
            "status": "success",
            "document_id": "doc-123",
            "title": "KBA_TEST_DOC_abc",
        }

    monkeypatch.setattr(lifecycle, "google_doc_write_impl", fake_write)
    read_calls = 0

    def fake_read(*_args, **_kwargs):
        nonlocal read_calls
        read_calls += 1
        text = (
            "KBA_TEST_DOC original content abc."
            if read_calls == 1
            else "KBA_TEST_DOC modified content abc."
        )
        return {
            "status": "success",
            "document_id": "doc-123",
            "title": "KBA_TEST_DOC_abc",
            "text": text,
            "char_count": len(text),
            "truncated": False,
        }

    monkeypatch.setattr(lifecycle, "google_doc_read_impl", fake_read)
    monkeypatch.setattr(
        lifecycle,
        "google_doc_trash_impl",
        lambda *_args, **_kwargs: {
            "status": "success",
            "document_id": "doc-123",
            "title": "KBA_TEST_DOC_abc",
            "trashed": True,
            "verification": {"passed": True},
        },
    )
    monkeypatch.setattr(
        lifecycle,
        "google_drive_get_file_metadata_impl",
        lambda *_args, **_kwargs: {"status": "success", "trashed": True},
    )

    result = lifecycle.execute_google_doc_test_lifecycle(
        suffix="abc",
        folder_path="KNIOps",
        approval_reference="approval",
        live=True,
    )

    assert result["status"] == "passed"
    assert write_calls == 2
    assert result["receipts"]["create_readback"]["passed"] is True
    assert result["receipts"]["update_readback"]["passed"] is True
    assert result["receipts"]["trash_readback"]["passed"] is True


def test_google_doc_lifecycle_trashes_after_update_failure(monkeypatch) -> None:
    write_calls = 0

    def fake_write(*_args, **_kwargs):
        nonlocal write_calls
        write_calls += 1
        if write_calls == 2:
            raise RuntimeError("update failed")
        return {
            "status": "success",
            "document_id": "doc-123",
            "title": "KBA_TEST_DOC_abc",
        }

    monkeypatch.setattr(lifecycle, "google_doc_write_impl", fake_write)
    monkeypatch.setattr(
        lifecycle,
        "google_doc_read_impl",
        lambda *_args, **_kwargs: {
            "status": "success",
            "document_id": "doc-123",
            "title": "KBA_TEST_DOC_abc",
            "text": "KBA_TEST_DOC original content abc.",
            "char_count": 35,
            "truncated": False,
        },
    )
    trash_calls: list[str] = []

    def fake_trash(document_id, **_kwargs):
        trash_calls.append(document_id)
        return {"status": "success", "document_id": document_id, "trashed": True}

    monkeypatch.setattr(lifecycle, "google_doc_trash_impl", fake_trash)
    monkeypatch.setattr(
        lifecycle,
        "google_drive_get_file_metadata_impl",
        lambda *_args, **_kwargs: {"status": "success", "trashed": True},
    )

    result = lifecycle.execute_google_doc_test_lifecycle(
        suffix="abc",
        folder_path="KNIOps",
        approval_reference="approval",
        live=True,
    )

    assert result["status"] == "failed"
    assert "update failed" in result["failure"]
    assert trash_calls == ["doc-123"]
    assert result["receipts"]["trash_readback"]["passed"] is True


def test_google_doc_agent_tool_lifecycle_uses_one_composite_call(monkeypatch) -> None:
    monkeypatch.setenv("KEYSTONE_GOOGLE_WORKSPACE_ALLOW_TEST_LIFECYCLE", "true")
    write_calls: list[dict[str, object]] = []
    trash_calls: list[dict[str, object]] = []

    def fake_write(title, body_text, **kwargs):
        write_calls.append({"title": title, "body_text": body_text, **kwargs})
        return {
            "status": "success",
            "operation": "write_doc",
            "document_id": "doc-123",
            "title": title,
            "content_verified": True,
            "provider_link": "https://docs.google.com/document/d/doc-123/edit",
            "send_enabled": False,
        }

    def fake_trash(document_id, **kwargs):
        trash_calls.append({"document_id": document_id, **kwargs})
        return {
            "status": "success",
            "operation": "trash_doc",
            "document_id": document_id,
            "trashed": True,
            "verification": {"passed": True},
            "send_enabled": False,
        }

    monkeypatch.setattr(data_tools, "google_doc_write_impl", fake_write)
    monkeypatch.setattr(data_tools, "google_doc_trash_impl", fake_trash)

    result = data_tools.google_doc_test_lifecycle_impl(
        "KBA_TEST_DOC_VALIDATION",
        "Provider lifecycle validation.",
        folder_path="KNIOps",
        approval_reference="ANU-120",
        live=True,
    )

    assert result["status"] == "success"
    assert result["provider_link"] == "https://docs.google.com/document/d/doc-123/edit"
    assert result["verification"] == {
        "passed": True,
        "create_read_back": True,
        "document_trashed_after_cleanup": True,
    }
    assert len(write_calls) == 1
    assert write_calls[0]["approval_reference"] == "ANU-120:create"
    assert trash_calls == [
        {
            "document_id": "doc-123",
            "folder_path": "KNIOps",
            "approval_reference": "ANU-120:trash",
            "live": True,
        }
    ]
