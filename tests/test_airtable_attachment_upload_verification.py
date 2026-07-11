from __future__ import annotations

from pathlib import Path

import pytest

from keystone_agents.tools import internal_data_tools
from keystone_agents.tools.internal_data_tools import airtable_upload_attachment_impl


def test_live_airtable_attachment_upload_requires_provider_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = tmp_path / "KBA_TEST_SLIDE-example.png"
    content = b"\x89PNG\r\n\x1a\nslide"
    attachment.write_bytes(content)
    monkeypatch.setenv("AIRTABLE_BASE_ID", "app_test")
    monkeypatch.setenv("AIRTABLE_ACCESS_TOKEN", "pat_test")
    monkeypatch.setenv("AIRTABLE_ALLOWED_TABLES", "Business Expenses")
    monkeypatch.setenv("AIRTABLE_WRITE_DRY_RUN", "false")
    monkeypatch.setenv("AIRTABLE_ALLOW_WRITES", "true")
    monkeypatch.setenv("AIRTABLE_ALLOW_ATTACHMENT_UPLOADS", "true")
    monkeypatch.setattr(
        internal_data_tools,
        "airtable_get_base_schema_impl",
        lambda **kwargs: {
            "schema": {
                "tables": [
                    {
                        "name": "Business Expenses",
                        "fields": [
                            {
                                "name": "Attachments",
                                "field_type": "multipleAttachments",
                                "field_id": "fld_attachment",
                            }
                        ],
                    }
                ]
            }
        },
    )
    reads = iter(
        [
            {"records": [{"id": "rec_test", "fields": {"Attachments": []}}]},
            {
                "records": [
                    {
                        "id": "rec_test",
                        "fields": {
                            "Attachments": [
                                {
                                    "id": "att_test",
                                    "filename": attachment.name,
                                    "size": len(content),
                                    "type": "image/png",
                                }
                            ]
                        },
                    }
                ]
            },
        ]
    )
    monkeypatch.setattr(
        internal_data_tools,
        "airtable_read_records_impl",
        lambda *args, **kwargs: next(reads),
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_airtable_send",
        lambda request, *, access_token: {"id": "att_test", "filename": attachment.name},
    )

    result = airtable_upload_attachment_impl(
        str(attachment),
        table="Business Expenses",
        record_id="rec_test",
        field_name="Attachments",
        approval_reference="operator:upload-slide",
        live=True,
    )

    assert result["status"] == "success"
    assert result["verification"] == {
        "status": "verified",
        "passed": True,
        "attachment_count_before": 0,
        "attachment_count_after": 1,
        "filename_match": True,
        "size_match": True,
    }


def test_live_airtable_attachment_upload_fails_closed_without_matching_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = tmp_path / "KBA_TEST_SLIDE-example.png"
    attachment.write_bytes(b"png")
    monkeypatch.setenv("AIRTABLE_BASE_ID", "app_test")
    monkeypatch.setenv("AIRTABLE_ACCESS_TOKEN", "pat_test")
    monkeypatch.setenv("AIRTABLE_ALLOWED_TABLES", "Business Expenses")
    monkeypatch.setenv("AIRTABLE_WRITE_DRY_RUN", "false")
    monkeypatch.setenv("AIRTABLE_ALLOW_WRITES", "true")
    monkeypatch.setenv("AIRTABLE_ALLOW_ATTACHMENT_UPLOADS", "true")
    monkeypatch.setattr(
        internal_data_tools,
        "airtable_get_base_schema_impl",
        lambda **kwargs: {
            "schema": {
                "tables": [
                    {
                        "name": "Business Expenses",
                        "fields": [
                            {
                                "name": "Attachments",
                                "field_type": "multipleAttachments",
                                "field_id": "fld_attachment",
                            }
                        ],
                    }
                ]
            }
        },
    )
    monkeypatch.setattr(
        internal_data_tools,
        "airtable_read_records_impl",
        lambda *args, **kwargs: {
            "records": [{"id": "rec_test", "fields": {"Attachments": []}}]
        },
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_airtable_send",
        lambda request, *, access_token: {"id": "att_test"},
    )

    result = airtable_upload_attachment_impl(
        str(attachment),
        table="Business Expenses",
        record_id="rec_test",
        field_name="Attachments",
        approval_reference="operator:upload-slide",
        live=True,
    )

    assert result["status"] == "verification_failed"
    assert result["verification"]["passed"] is False
