from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from keystone_agents import provider_recovery, tool_receipt_journal
from keystone_agents.receipts import journal, recovery
from keystone_agents.receipts.mutations import (
    mutation_tool_names,
    operation_is_mutation,
    receipt_reports_possible_write,
)
from keystone_agents.receipts.normalization import (
    identity_fingerprint,
    identity_fingerprints,
    normalize_tool_output_receipt,
)


def test_tool_invocation_journal_counts_repeated_calls_and_terminal_failures() -> None:
    async def invoke(_context: object, tool_input: str) -> str:
        if tool_input == "fail-with-private-input":
            raise RuntimeError("private provider response must not enter telemetry")
        return json.dumps(
            {
                "status": "success",
                "operation": "read_records",
                "provider": "example",
                "item_count": 1,
            }
        )

    tool = SimpleNamespace(
        name="read_example_records",
        on_invoke_tool=invoke,
        is_enabled=True,
    )
    agent = SimpleNamespace(tools=[tool])
    journal.reset_tool_receipt_journal()
    journal.instrument_agent_tools(agent)

    asyncio.run(tool.on_invoke_tool(None, "first-private-input"))
    asyncio.run(tool.on_invoke_tool(None, "second-private-input"))
    with pytest.raises(RuntimeError):
        asyncio.run(tool.on_invoke_tool(None, "fail-with-private-input"))

    assert journal.tool_invocation_journal() == [
        {
            "tool_name": "read_example_records",
            "invocation_index": 1,
            "status": "started",
            "arguments_sha256": journal.tool_payload_fingerprint("first-private-input"),
        },
        {
            "tool_name": "read_example_records",
            "invocation_index": 1,
            "status": "completed",
            "output_sha256": journal.tool_payload_fingerprint({
                "status": "success", "operation": "read_records",
                "provider": "example", "item_count": 1,
            }),
        },
        {
            "tool_name": "read_example_records",
            "invocation_index": 2,
            "status": "started",
            "arguments_sha256": journal.tool_payload_fingerprint("second-private-input"),
        },
        {
            "tool_name": "read_example_records",
            "invocation_index": 2,
            "status": "completed",
            "output_sha256": journal.tool_payload_fingerprint({
                "status": "success", "operation": "read_records",
                "provider": "example", "item_count": 1,
            }),
        },
        {
            "tool_name": "read_example_records",
            "invocation_index": 3,
            "status": "started",
            "arguments_sha256": journal.tool_payload_fingerprint("fail-with-private-input"),
        },
        {
            "tool_name": "read_example_records",
            "invocation_index": 3,
            "status": "failed",
            "error_type": "RuntimeError",
        },
    ]
    serialized = json.dumps(journal.tool_invocation_journal())
    assert "private-input" not in serialized
    assert "private provider response" not in serialized


def test_tool_invocation_journal_distinguishes_returned_dry_run_from_success() -> None:
    async def invoke(_context: object, _tool_input: str) -> str:
        return json.dumps(
            {
                "status": "dry-run",
                "operation": "read_calendar_window",
                "events": [],
            }
        )

    tool = SimpleNamespace(
        name="read_google_calendar_window",
        on_invoke_tool=invoke,
        is_enabled=True,
    )
    journal.reset_tool_receipt_journal()
    journal.instrument_agent_tools(SimpleNamespace(tools=[tool]))

    asyncio.run(tool.on_invoke_tool(None, "{}"))

    assert journal.tool_invocation_journal()[-1] == {
        "tool_name": "read_google_calendar_window",
        "invocation_index": 1,
        "status": "returned_unsuccessful",
    }


@pytest.mark.parametrize(
    "operation",
    [
        "append_rows",
        "attach_file",
        "create_record",
        "delete_test_record",
        "link_attachment",
        "reconcile_duplicate_expense",
        "remove_label",
        "send_message",
        "trash_sheet",
        "update_note",
        "extract_slide_copy",
        "apply_gmail_labels",
        "star",
        "unstar",
        "mark_read",
        "mark_unread",
        "mark_important",
        "mark_not_important",
        "restore",
        "unarchive",
    ],
)
def test_shared_mutation_classifier_covers_existing_write_families(
    operation: str,
) -> None:
    assert operation_is_mutation(operation) is True
    assert receipt_reports_possible_write({"operation": operation}) is True


@pytest.mark.parametrize(
    "receipt",
    [
        {"operation": "read_record"},
        {"tool_name": "airtable_get_record", "status": "success"},
        {"operation": "create_record", "status": "dry-run"},
        {"operation": "create_record", "dry_run": True},
    ],
)
def test_shared_mutation_classifier_does_not_report_read_or_dry_run_as_write(
    receipt: dict[str, object],
) -> None:
    assert receipt_reports_possible_write(receipt) is False


def test_shared_mutation_classifier_uses_tool_name_and_approval_evidence() -> None:
    receipts = [
        {
            "tool_name": "airtable_create_record",
            "status": "success",
        },
        {
            "tool_name": "workspace_lookup",
            "approval_reference": "approval-fixture",
        },
        {
            "tool_name": "airtable_get_record",
            "operation": "read_record",
        },
    ]

    assert mutation_tool_names(receipts) == {
        "airtable_create_record",
        "workspace_lookup",
    }


@pytest.mark.parametrize(
    "operation",
    [
        "star",
        "unstar",
        "mark_read",
        "mark_unread",
        "mark_important",
        "mark_not_important",
        "restore",
        "unarchive",
    ],
)
def test_shared_classifier_recognizes_exact_gmail_mailbox_mutations(
    operation: str,
) -> None:
    assert operation_is_mutation(operation) is True


def test_shared_classifier_does_not_use_a_broad_mark_keyword_rule() -> None:
    assert operation_is_mutation("mark_for_review") is False


def test_shared_classifier_recognizes_realistic_gmail_label_receipt() -> None:
    receipt = {
        "status": "labels_applied",
        "tool_name": "apply_gmail_labels",
        "provider_write": True,
        "label_ids": ["Label_fixture"],
    }

    assert receipt_reports_possible_write(receipt) is True
    assert mutation_tool_names([receipt]) == {"apply_gmail_labels"}
    assert normalize_tool_output_receipt("apply_gmail_labels", receipt) == {
        "status": "labels_applied",
        "provider_write": True,
        "tool_name": "apply_gmail_labels",
    }


def test_provider_write_true_is_authoritative_except_for_dry_run() -> None:
    assert receipt_reports_possible_write(
        {
            "status": "success",
            "tool_name": "provider_fixture",
            "provider_write": True,
        }
    )
    assert not receipt_reports_possible_write(
        {
            "status": "dry-run",
            "tool_name": "apply_gmail_labels",
            "provider_write": True,
        }
    )


def test_verified_gmail_state_receipt_journals_checkpoints_and_reuses(
    tmp_path,
) -> None:
    checkpoint = tmp_path / "gmail-state-recovery.json"
    store = recovery.ProviderRecoveryStore(
        checkpoint,
        idempotency_key="gmail-state-fixture",
    )
    receipt = {
        "status": "message_state_modified",
        "operation": "mark_read",
        "provider": "gmail",
        "message_id": "message-fixture",
        "provider_write": True,
        "verification": {"passed": True},
        "send_enabled": False,
    }

    journal.reset_tool_receipt_journal(receipt_sink=store.record_receipt)
    journal.record_tool_output("modify_gmail_message_state", receipt)

    assert len(journal.tool_receipt_journal()) == 1
    assert checkpoint.exists()
    assert store.state.status == "partial_success"
    assert len(store.state.receipts) == 1
    assert store.state.receipts[0].object_id == "message-fixture"
    assert store.state.receipts[0].payload["status"] == "message_state_modified"

    resumed = recovery.ProviderRecoveryStore(
        checkpoint,
        idempotency_key="gmail-state-fixture",
    )
    execute_calls = 0

    def repeat_mutation() -> dict[str, object]:
        nonlocal execute_calls
        execute_calls += 1
        return receipt

    reused = resumed.reuse_or_execute_mutation(
        tool_name="modify_gmail_message_state",
        operation="mark_read",
        execute=repeat_mutation,
    )

    assert reused["message_id"] == "message-fixture"
    assert execute_calls == 0
    assert resumed.state.retry_reused is True


@pytest.mark.parametrize(
    "status",
    ["blocked", "cancelled", "denied", "error", "failed", "rejected", "dry-run"],
)
def test_recovery_rejects_non_successful_mutation_evidence(
    tmp_path,
    status: str,
) -> None:
    store = recovery.ProviderRecoveryStore(
        tmp_path / f"{status}.json",
        idempotency_key=f"gmail-state-{status}",
    )

    with pytest.raises(recovery.ProviderRecoveryError, match="provider-verified"):
        store.record_receipt(
            {
                "status": status,
                "operation": "mark_read",
                "tool_name": "modify_gmail_message_state",
                "provider": "gmail",
                "message_id": "message-fixture",
                "provider_write": True,
                "verification": {"passed": True},
            }
        )


def test_recovery_rejects_unverified_gmail_label_receipt(tmp_path) -> None:
    store = recovery.ProviderRecoveryStore(
        tmp_path / "gmail-labels.json",
        idempotency_key="gmail-labels-unverified",
    )

    with pytest.raises(recovery.ProviderRecoveryError, match="provider-verified"):
        store.record_receipt(
            {
                "status": "labels_applied",
                "operation": "apply_gmail_labels",
                "tool_name": "apply_gmail_labels",
                "provider": "gmail",
                "message_id": "message-fixture",
                "provider_write": True,
            }
        )

    assert store.state.receipts == []


def test_verified_provider_write_summary_requires_current_identity_and_readback() -> None:
    verified = {
        "status": "success",
        "operation": "update",
        "tool_name": "airtable_write_record",
        "provider": "airtable",
        "table": "Business Expenses",
        "record_id": "recVerified123",
        "provider_write": True,
        "verification": {"passed": True},
    }

    assert recovery.verified_provider_write_summary([verified]) == (
        "Updated and provider-verified Business Expenses recVerified123 in place."
    )
    assert recovery.verified_provider_write_summary(
        [{**verified, "verification": {"passed": False}}]
    ) == ""
    assert recovery.verified_provider_write_summary(
        [{key: value for key, value in verified.items() if key != "record_id"}]
    ) == ""


def test_tool_output_normalization_preserves_the_existing_bounded_contract() -> None:
    receipt = normalize_tool_output_receipt(
        "calendar_create_event",
        json.dumps(
            {
                "status": "success",
                "operation": "create_event",
                "event_id": "event-fixture",
                "verification": {"passed": True},
                "private_body": "must not be retained",
            }
        ),
    )

    assert receipt == {
        "status": "success",
        "operation": "create_event",
        "event_id": "event-fixture",
        "verification": {"passed": True},
        "tool_name": "calendar_create_event",
    }
    assert normalize_tool_output_receipt("tool", "not-json") is None
    assert normalize_tool_output_receipt("tool", SimpleNamespace()) is None


def test_calendar_receipt_normalization_preserves_trace_safe_provider_proof() -> None:
    receipt = normalize_tool_output_receipt(
        "create_google_calendar_event",
        {
            "status": "success",
            "operation": "create_calendar_event",
            "provider": "google_calendar",
            "provider_read": True,
            "provider_write": True,
            "provider_mutated": True,
            "provider_request_attempt_count": 3,
            "provider_request_success_count": 3,
            "event_id": "event-fixture",
            "verification": {"passed": True},
            "verified": True,
            "complete": True,
            "private_body": "must not be retained",
        },
    )

    assert receipt == {
        "status": "success",
        "operation": "create_calendar_event",
        "provider": "google_calendar",
        "provider_read": True,
        "provider_write": True,
        "provider_mutated": True,
        "provider_request_attempt_count": 3,
        "provider_request_success_count": 3,
        "event_id": "event-fixture",
        "verification": {"passed": True},
        "verified": True,
        "complete": True,
        "tool_name": "create_google_calendar_event",
    }


def test_identity_fingerprints_hash_each_generator_item_and_remain_receipt_safe() -> None:
    fingerprints = identity_fingerprints(value for value in ("record-a", "record-b"))

    assert fingerprints == [
        identity_fingerprint("record-a"),
        identity_fingerprint("record-b"),
    ]
    receipt = normalize_tool_output_receipt(
        "airtable_read_records",
        {
            "status": "success",
            "identity_fingerprints": fingerprints,
            "records": [{"id": "record-a", "private": "not journaled"}],
        },
    )
    assert receipt == {
        "status": "success",
        "identity_fingerprints": fingerprints,
        "tool_name": "airtable_read_records",
    }


def test_search_result_receipt_binds_urls_without_journaling_result_content() -> None:
    receipt = normalize_tool_output_receipt(
        "search_web",
        [
            {
                "title": "Private-looking result title",
                "link": "https://example.org/evidence",
                "snippet": "This content must not enter the bounded receipt.",
            }
        ],
    )

    assert receipt == {
        "status": "success",
        "operation": "search",
        "item_count": 1,
        "identity_fingerprints": identity_fingerprints(
            ["https://example.org/evidence"]
        ),
        "tool_name": "search_web",
    }
    assert "Private-looking" not in json.dumps(receipt)
    assert "content must not" not in json.dumps(receipt)


def test_legacy_receipt_and_recovery_imports_are_compatibility_facades() -> None:
    assert tool_receipt_journal.record_tool_output is journal.record_tool_output
    assert (
        tool_receipt_journal.reset_tool_receipt_journal
        is journal.reset_tool_receipt_journal
    )
    assert provider_recovery.ProviderRecoveryStore is recovery.ProviderRecoveryStore
    assert (
        provider_recovery.ProviderPartialSuccessError
        is recovery.ProviderPartialSuccessError
    )
