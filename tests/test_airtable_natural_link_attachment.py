from __future__ import annotations

from types import SimpleNamespace

import scripts.run_airtable_natural_link_attachment as runner


def test_tool_names_reads_sdk_tool_call_items() -> None:
    raw = SimpleNamespace(
        new_items=[
            SimpleNamespace(raw_item=SimpleNamespace(name="airtable_link_attachment")),
            SimpleNamespace(raw_item=SimpleNamespace(name="airtable_link_attachment")),
        ]
    )

    assert runner._tool_names(raw) == ["airtable_link_attachment"]


def test_joined_validation_requires_link_tool_and_verified_cleanup(monkeypatch) -> None:
    monkeypatch.setattr(
        runner,
        "airtable_write_record_impl",
        lambda *_args, **_kwargs: {
            "record_id": "rec_test",
            "verification": {"passed": True},
        },
    )
    monkeypatch.setattr(
        runner,
        "airtable_read_records_impl",
        lambda *_args, **_kwargs: {
            "records": [{"id": "rec_test", "fields": {"Attachments": [{"id": "att1"}]}}]
        },
    )
    monkeypatch.setattr(
        runner,
        "airtable_delete_test_record_impl",
        lambda *_args, **_kwargs: {
            "verification": {"passed": True, "record_absent_after": True}
        },
    )
    raw = SimpleNamespace(
        new_items=[SimpleNamespace(raw_item=SimpleNamespace(name="airtable_link_attachment"))]
    )

    result = runner.execute_validation(
        model=runner.EXPECTED_MODEL,
        receipt_url=runner.DEFAULT_RECEIPT_URL,
        budget_usd=runner.MAX_BUDGET_USD,
        model_runner=lambda *_args, **_kwargs: SimpleNamespace(
            usage={"requests": 2},
            cost={"estimated_usd": 0.02},
            request_cache={"rate_limit_retries": 0},
            raw_result=raw,
        ),
    )

    assert result["status"] == "pass"
    assert result["tool_selection"]["selected_link_tool"] is True
    assert result["tool_selection"]["selected_local_upload_tool"] is False
    assert result["provider"]["record_absent_after"] is True


def test_joined_validation_rejects_local_upload_selection(monkeypatch) -> None:
    monkeypatch.setattr(
        runner,
        "airtable_write_record_impl",
        lambda *_args, **_kwargs: {
            "record_id": "rec_test",
            "verification": {"passed": True},
        },
    )
    monkeypatch.setattr(
        runner,
        "airtable_read_records_impl",
        lambda *_args, **_kwargs: {
            "records": [{"id": "rec_test", "fields": {"Attachments": [{"id": "att1"}]}}]
        },
    )
    monkeypatch.setattr(
        runner,
        "airtable_delete_test_record_impl",
        lambda *_args, **_kwargs: {
            "verification": {"passed": True, "record_absent_after": True}
        },
    )
    raw = SimpleNamespace(
        new_items=[SimpleNamespace(raw_item=SimpleNamespace(name="airtable_upload_attachment"))]
    )

    result = runner.execute_validation(
        model=runner.EXPECTED_MODEL,
        receipt_url=runner.DEFAULT_RECEIPT_URL,
        budget_usd=runner.MAX_BUDGET_USD,
        model_runner=lambda *_args, **_kwargs: SimpleNamespace(
            usage={"requests": 2},
            cost={"estimated_usd": 0.02},
            request_cache={"rate_limit_retries": 0},
            raw_result=raw,
        ),
    )

    assert result["status"] == "partial"
    assert result["tool_selection"]["selected_local_upload_tool"] is True
