from __future__ import annotations

from typing import Any

from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemKind,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemStatus,
)
from keystone_agents.tools.chief_context_tools import (
    acquire_chief_context_evidence,
)


class FakeGmailTool:
    def __init__(self, summaries: list[dict[str, Any]]) -> None:
        self.summaries = summaries
        self.calls: list[dict[str, Any]] = []

    def search_message_summaries(
        self,
        *,
        query: str,
        max_results: int,
    ) -> list[dict[str, Any]]:
        self.calls.append({"query": query, "max_results": max_results})
        return self.summaries


class FakeWorkItemStore:
    def __init__(self, work_items: list[WorkItem]) -> None:
        self.work_items = work_items
        self.calls: list[dict[str, Any]] = []

    def list_work_items(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[WorkItem]:
        self.calls.append({"status": status, "kind": kind, "limit": limit})
        return self.work_items


def _schema_result() -> dict[str, Any]:
    return {
        "status": "success",
        "schema": {
            "allowed_tables": ["Projects", "Expenses"],
            "tables": [
                {"name": "Projects", "fields": []},
                {"name": "Expenses", "fields": []},
            ],
        },
    }


def test_acquire_chief_context_evidence_reads_every_typed_source() -> None:
    gmail = FakeGmailTool(
        [
            {
                "id": "msg-1",
                "thread_id": "thread-1",
                "sender_name": "Alex",
                "subject": "Review needed",
                "snippet": "Can you review the updated proposal?",
                "received_at": "2026-07-25T12:00:00Z",
            }
        ]
    )
    store = FakeWorkItemStore(
        [
            WorkItem(
                id="wi-current",
                kind=WorkItemKind.RESEARCH_BRIEF,
                title="Current context review",
                status=WorkItemStatus.IN_PROGRESS,
            ),
            WorkItem(
                id="wi-open",
                kind=WorkItemKind.RESEARCH_BRIEF,
                title="Prepare partner brief",
                status=WorkItemStatus.NEEDS_CONTEXT,
                current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                next_action=WorkItemNextAction(
                    action="provide_context",
                    description="Add the source-backed outcomes note.",
                ),
            ),
        ]
    )
    record_calls: list[dict[str, Any]] = []

    def read_records(table: str, **kwargs: Any) -> dict[str, Any]:
        record_calls.append({"table": table, **kwargs})
        return {
            "status": "success",
            "records": (
                [
                    {
                        "id": f"rec-{table.lower()}",
                        "fields": {"Name": f"Current {table}", "Status": "Open"},
                    }
                ]
                if table == "Projects"
                else []
            ),
        }

    evidence = acquire_chief_context_evidence(
        required_sources=["gmail", "work_items", "airtable"],
        gmail_query="in:inbox after:2026/07/25 before:2026/07/26",
        store=store,
        current_work_item_id="wi-current",
        live=True,
        gmail_tool=gmail,
        airtable_schema_reader=lambda **_kwargs: _schema_result(),
        airtable_records_reader=read_records,
    )

    assert evidence.complete is True
    assert evidence.blockers == []
    assert [receipt.source for receipt in evidence.receipts] == [
        "gmail",
        "work_items",
        "airtable",
    ]
    assert all(receipt.verified for receipt in evidence.receipts)
    assert {item.source_id for item in evidence.items} == {
        "msg-1",
        "wi-open",
        "rec-projects",
    }
    assert gmail.calls == [
        {
            "query": "in:inbox after:2026/07/25 before:2026/07/26",
            "max_results": 5,
        }
    ]
    assert [call["table"] for call in record_calls] == ["Projects", "Expenses"]
    assert all(call["live"] is True for call in record_calls)
    assert all(call["max_records"] == 3 for call in record_calls)


def test_successful_zero_result_reads_are_verified_evidence() -> None:
    evidence = acquire_chief_context_evidence(
        required_sources=["gmail", "work_items", "airtable"],
        gmail_query="after:2026/07/25 before:2026/07/26",
        store=FakeWorkItemStore([]),
        current_work_item_id="wi-current",
        live=True,
        gmail_tool=FakeGmailTool([]),
        airtable_schema_reader=lambda **_kwargs: _schema_result(),
        airtable_records_reader=lambda *_args, **_kwargs: {
            "status": "success",
            "records": [],
        },
    )

    assert evidence.complete is True
    assert evidence.items == []
    assert [receipt.status for receipt in evidence.receipts] == [
        "empty",
        "empty",
        "empty",
    ]
    assert all(receipt.provider_read for receipt in evidence.receipts)


def test_airtable_provider_failure_blocks_complete_context() -> None:
    def fail_records(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("provider unavailable")

    evidence = acquire_chief_context_evidence(
        required_sources=["airtable"],
        live=True,
        airtable_schema_reader=lambda **_kwargs: _schema_result(),
        airtable_records_reader=fail_records,
    )

    assert evidence.complete is False
    assert evidence.receipts[0].status == "blocked"
    assert evidence.receipts[0].verified is False
    assert evidence.receipts[0].error_type == "RuntimeError"
    assert "provider unavailable" not in " ".join(evidence.blockers)


def test_airtable_schema_without_approved_readable_table_blocks() -> None:
    record_calls: list[str] = []

    evidence = acquire_chief_context_evidence(
        required_sources=["airtable"],
        live=True,
        airtable_schema_reader=lambda **_kwargs: {
            "status": "success",
            "schema": {
                "allowed_tables": ["Approved"],
                "tables": [{"name": "Unapproved", "fields": []}],
            },
        },
        airtable_records_reader=lambda table, **_kwargs: record_calls.append(table)
        or {"status": "success", "records": []},
    )

    assert evidence.complete is False
    assert evidence.receipts[0].status == "blocked"
    assert "no approved table" in evidence.receipts[0].details.lower()
    assert record_calls == []


def test_airtable_context_excludes_attachment_url_and_nested_blob_fields() -> None:
    evidence = acquire_chief_context_evidence(
        required_sources=["airtable"],
        live=True,
        airtable_schema_reader=lambda **_kwargs: {
            "status": "success",
            "schema": {
                "allowed_tables": ["Projects"],
                "tables": [
                    {
                        "name": "Projects",
                        "fields": [
                            {"name": "Name", "field_type": "singleLineText"},
                            {"name": "Status", "field_type": "singleSelect"},
                            {"name": "Receipt Attachments", "field_type": "multipleAttachments"},
                            {"name": "Source URL", "field_type": "url"},
                            {"name": "Linked Records", "field_type": "multipleRecordLinks"},
                            {"name": "Mystery"},
                        ],
                    }
                ],
            },
        },
        airtable_records_reader=lambda *_args, **_kwargs: {
            "status": "success",
            "records": [
                {
                    "id": "rec-safe",
                    "fields": {
                        "Name": "Validation project",
                        "Status": "Open",
                        "Receipt Attachments": [
                            {
                                "url": "https://files.example.test/private",
                                "filename": "receipt.pdf",
                            }
                        ],
                        "Source URL": "https://example.test/private",
                        "Linked Records": ["rec-linked"],
                        "Mystery": "unknown field types must not enter model context",
                    },
                }
            ],
        },
    )

    assert evidence.complete is True
    assert len(evidence.items) == 1
    assert evidence.items[0].metadata["fields"] == {
        "Name": "Validation project",
        "Status": "Open",
    }
    serialized = evidence.model_dump_json()
    assert "receipt.pdf" not in serialized
    assert "files.example.test" not in serialized
    assert "rec-linked" not in serialized
    assert "unknown field types" not in serialized
