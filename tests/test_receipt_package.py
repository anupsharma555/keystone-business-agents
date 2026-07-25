from __future__ import annotations

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
from keystone_agents.receipts.normalization import normalize_tool_output_receipt


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
