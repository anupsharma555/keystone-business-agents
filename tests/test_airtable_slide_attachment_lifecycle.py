from __future__ import annotations

from pathlib import Path

import pytest

import scripts.run_airtable_slide_attachment_lifecycle as lifecycle


def _verified(**values):
    return {"status": "success", "verification": {"passed": True}, **values}


def test_airtable_slide_lifecycle_preserves_record_and_cleans_both_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "KBA_TEST_SLIDE-case-airtable.png"
    artifact.write_bytes(b"png")
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        lifecycle,
        "presentation_extract_slide_copy_local_impl",
        lambda *args, **kwargs: _verified(
            artifact_path=str(artifact),
            derived_copy_created=True,
            parent_modified=False,
        ),
    )

    def fake_write(fields_json, **kwargs):
        operation = kwargs["operation"]
        record_id = kwargs.get("record_id") or "rec-slide-1"
        writes.append((operation, record_id))
        return _verified(operation=operation, record_id=record_id, table=kwargs["table"])

    monkeypatch.setattr(lifecycle, "airtable_write_record_impl", fake_write)
    monkeypatch.setattr(
        lifecycle,
        "airtable_upload_attachment_impl",
        lambda *args, **kwargs: _verified(
            operation="upload_attachment",
            record_id=kwargs["record_id"],
            table=kwargs["table"],
            field_name=kwargs["field_name"],
            filename=artifact.name,
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "airtable_delete_test_record_impl",
        lambda record_id, **kwargs: _verified(
            operation="delete_test_record",
            record_id=record_id,
            table=kwargs["table"],
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "presentation_delete_test_artifact_local_impl",
        lambda *args, **kwargs: _verified(
            operation="delete_test_slide_artifact",
            artifact_path=str(artifact),
        ),
    )

    result = lifecycle.execute_airtable_slide_attachment_lifecycle(
        relative_path="deck.pptx",
        slide_number=2,
        base_alias="finance_tax_tracker",
        table="Business Expenses",
        attachment_field="Attachments",
        suffix="case",
        approval_reference="operator:airtable-slide",
        live=True,
    )

    assert result["status"] == "passed"
    assert result["openai_requests"] == 0
    assert result["send_or_post"] is False
    assert writes == [("create", "rec-slide-1"), ("update", "rec-slide-1")]
    assert result["create"]["record_id"] == result["update"]["record_id"]
    assert result["record_cleanup"]["verification"]["passed"] is True
    assert result["artifact_cleanup"]["verification"]["passed"] is True


def test_airtable_slide_lifecycle_recovers_timed_out_create_by_exact_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "KBA_TEST_SLIDE-timeout-airtable.png"
    artifact.write_bytes(b"png")
    deleted: list[str] = []
    monkeypatch.setattr(
        lifecycle,
        "presentation_extract_slide_copy_local_impl",
        lambda *args, **kwargs: _verified(
            artifact_path=str(artifact),
            derived_copy_created=True,
            parent_modified=False,
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "airtable_write_record_impl",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("provider timeout")),
    )
    monkeypatch.setattr(
        lifecycle,
        "airtable_read_records_impl",
        lambda *args, **kwargs: {
            "status": "success",
            "records": [
                {
                    "id": "rec-recovered",
                    "fields": {"Item": "KBA_TEST_RECORD slide-timeout"},
                }
            ],
        },
    )

    def fake_delete(record_id, **kwargs):
        deleted.append(record_id)
        return _verified(operation="delete_test_record", record_id=record_id)

    monkeypatch.setattr(lifecycle, "airtable_delete_test_record_impl", fake_delete)
    monkeypatch.setattr(
        lifecycle,
        "presentation_delete_test_artifact_local_impl",
        lambda *args, **kwargs: _verified(
            operation="delete_test_slide_artifact",
            artifact_path=str(artifact),
        ),
    )

    result = lifecycle.execute_airtable_slide_attachment_lifecycle(
        relative_path="deck.pptx",
        slide_number=2,
        base_alias="finance_tax_tracker",
        table="Business Expenses",
        attachment_field="Attachments",
        suffix="timeout",
        approval_reference="operator:airtable-slide",
        live=True,
    )

    assert result["status"] == "failed"
    assert "provider timeout" in result["failure"]
    assert result["orphan_check"]["recovered_record"] is True
    assert result["orphan_check"]["verification"]["matching_record_count"] == 1
    assert deleted == ["rec-recovered"]
    assert result["record_cleanup"]["verification"]["passed"] is True
