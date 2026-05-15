from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import scripts.list_approvals as list_approvals
import scripts.update_approval as update_approval
from keystone_agents.feedback import build_operator_feedback_request
from keystone_agents.schemas.approval import (
    ApprovalQueueItem,
    ApprovalQueueObjectType,
    ApprovalQueueStatus,
    ApprovalTransitionError,
    approval_queue_status_allows_sending,
    validate_approval_queue_transition,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.approval_tool import (
    build_approval_queue_item,
    create_approval_queue_item,
)
from keystone_agents.tools.slack_tool import slack_review_message_from_approval_item

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'approval_queue.db'}"


def _approval_item(item_id: str = "approval-test-1") -> ApprovalQueueItem:
    return ApprovalQueueItem(
        id=item_id,
        object_type=ApprovalQueueObjectType.GMAIL_DRAFT,
        object_id="gmail-msg-1",
        title="Consulting inquiry reply",
        summary="Inbound consulting inquiry needs a human-reviewed reply draft.",
        draft_text="Hi Alex,\n\nThanks for reaching out. I would be glad to compare notes.",
        source_agent="gmail_triage",
        risk_flags=[],
        metadata={"fixture": "consulting"},
    )


def test_create_approval_item_and_fetch_by_id(tmp_path: Path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    item = _approval_item()

    item_id = store.save_approval_item(item)
    loaded = store.get_approval_item(item_id)

    assert item_id == "approval-test-1"
    assert loaded is not None
    assert loaded.object_type == ApprovalQueueObjectType.GMAIL_DRAFT
    assert loaded.approval_status == ApprovalQueueStatus.PENDING
    assert loaded.draft_text == item.draft_text
    assert loaded.metadata == {"fixture": "consulting"}


def test_list_pending_approvals(tmp_path: Path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    store.save_approval_item(_approval_item("approval-pending"))
    store.save_approval_item(
        _approval_item("approval-approved").model_copy(
            update={"approval_status": ApprovalQueueStatus.APPROVED}
        )
    )

    pending = store.get_pending_approvals()

    assert [item.id for item in pending] == ["approval-pending"]


def test_approve_reject_and_revise_items(tmp_path: Path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    store.save_approval_item(_approval_item("approval-approve"))
    store.save_approval_item(_approval_item("approval-reject"))
    store.save_approval_item(_approval_item("approval-revise"))

    approved = store.update_approval_status(
        "approval-approve",
        "approved",
        reviewer="owner@example.com",
    )
    rejected = store.update_approval_status(
        "approval-reject",
        "rejected",
        reviewer="owner@example.com",
    )
    revised = store.update_approval_status(
        "approval-revise",
        "revise",
        reviewer="owner@example.com",
        notes="Remove the claim and shorten the reply.",
    )

    assert approved.approval_status == ApprovalQueueStatus.APPROVED
    assert rejected.approval_status == ApprovalQueueStatus.REJECTED
    assert revised.approval_status == ApprovalQueueStatus.REVISE
    assert revised.reviewer_notes == "Remove the claim and shorten the reply."
    assert revised.updated_at is not None
    assert approval_queue_status_allows_sending(approved.approval_status) is False


def test_approval_queue_state_machine_rejects_invalid_transitions(tmp_path: Path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    store.save_approval_item(_approval_item("approval-transition"))

    validate_approval_queue_transition("pending", "revise")
    validate_approval_queue_transition("revise", "pending")
    validate_approval_queue_transition("pending", "expired")

    revised = store.update_approval_status("approval-transition", "revise")
    resubmitted = store.update_approval_status("approval-transition", "pending")
    approved = store.update_approval_status("approval-transition", "approved")

    assert revised.approval_status == ApprovalQueueStatus.REVISE
    assert resubmitted.approval_status == ApprovalQueueStatus.PENDING
    assert approved.approval_status == ApprovalQueueStatus.APPROVED
    with pytest.raises(ApprovalTransitionError, match="approved -> rejected"):
        store.update_approval_status("approval-transition", "rejected")
    with pytest.raises(ApprovalTransitionError, match="archived -> pending"):
        validate_approval_queue_transition("archived", "pending")


def test_expired_queue_items_are_supported(tmp_path: Path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    store.save_approval_item(_approval_item("approval-expired"))

    expired = store.update_approval_status(
        "approval-expired",
        "expired",
        reviewer="owner@example.com",
        notes="Review window expired.",
    )

    assert expired.approval_status == ApprovalQueueStatus.EXPIRED
    assert expired.reviewer == "owner@example.com"
    assert expired.reviewer_notes == "Review window expired."


def test_archive_approval_item(tmp_path: Path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    store.save_approval_item(_approval_item("approval-archive"))

    archived = store.archive_approval_item("approval-archive", notes="Closed after review.")

    assert archived.approval_status == ApprovalQueueStatus.ARCHIVED
    assert archived.reviewer_notes == "Closed after review."


def test_pending_approvals_serialize_to_json(tmp_path: Path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    store.save_approval_item(_approval_item())

    payload = [item.model_dump(mode="json") for item in store.get_pending_approvals()]
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True)

    assert "approval-test-1" in encoded
    assert "created_at" in payload[0]


def test_approval_queue_storage_summarizes_sensitive_inbound_metadata(tmp_path: Path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    store.save_approval_item(
        _approval_item("approval-sensitive").model_copy(
            update={
                "metadata": {
                    "body": "Full inbound email body with token=SHOULD_NOT_APPEAR_123456789.",
                    "send_enabled": False,
                }
            }
        )
    )

    loaded = store.get_approval_item("approval-sensitive")

    assert loaded is not None
    assert "body" not in loaded.metadata
    assert loaded.metadata["body_hash"]
    assert "Full inbound email body" in loaded.metadata["body_summary"]
    assert "SHOULD_NOT_APPEAR" not in json.dumps(loaded.metadata)


def test_sqlite_tmp_path_works_for_approval_queue(tmp_path: Path) -> None:
    store = SQLiteStore(_database_url(tmp_path))

    assert "approval_queue" in store.table_names()
    assert store.count("approval_queue") == 0


def test_approval_queue_cli_lists_json_and_updates_status(
    tmp_path: Path,
    capsys,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_approval_item(_approval_item("approval-cli"))

    assert list_approvals.main(["--database-url", database_url, "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["id"] == "approval-cli"
    assert listed[0]["approval_status"] == "pending"

    assert (
        update_approval.main(
            [
                "approval-cli",
                "approved",
                "--database-url",
                database_url,
                "--notes",
                "Reviewed locally.",
                "--json",
            ]
        )
        == 0
    )
    updated = json.loads(capsys.readouterr().out)
    assert updated["approval_status"] == "approved"
    assert updated["reviewer_notes"] == "Reviewed locally."
    assert updated["metadata"].get("send_enabled") is not True


def test_approval_queue_cli_filters_by_object_type_and_source_agent(
    tmp_path: Path,
    capsys,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_approval_item(_approval_item("approval-gmail"))
    store.save_approval_item(
        _approval_item("approval-outreach").model_copy(
            update={
                "object_type": ApprovalQueueObjectType.OUTREACH_DRAFT,
                "source_agent": "outreach_composer",
            }
        )
    )

    assert (
        list_approvals.main(
            [
                "--database-url",
                database_url,
                "--object-type",
                "outreach_draft",
                "--source-agent",
                "outreach_composer",
                "--json",
            ]
        )
        == 0
    )
    listed = json.loads(capsys.readouterr().out)

    assert [item["id"] for item in listed] == ["approval-outreach"]


def test_approval_queue_markdown_surfaces_operator_feedback_request(
    tmp_path: Path,
    capsys,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    feedback_request = build_operator_feedback_request(
        object_type="outreach_draft",
        object_id="draft-1",
        source_agent="outreach_composer",
    )
    store.save_approval_item(
        _approval_item("approval-feedback").model_copy(
            update={
                "object_type": ApprovalQueueObjectType.OUTREACH_DRAFT,
                "object_id": "draft-1",
                "source_agent": "outreach_composer",
                "metadata": {
                    "operator_feedback_request": feedback_request.model_dump(mode="json"),
                    "send_enabled": False,
                },
            }
        )
    )

    assert list_approvals.main(["--database-url", database_url]) == 0
    rendered = capsys.readouterr().out

    assert "Feedback Question" in rendered
    assert "Question: Should this artifact be approved, revised, or rejected?" in rendered
    assert "weak_personalization" in rendered
    assert "Full draft text is omitted" in rendered


def test_build_approval_queue_item_from_draft_payload() -> None:
    item = build_approval_queue_item(
        {
            "message_id": "gmail-msg-2",
            "summary": "Reply draft for a consulting inquiry.",
            "draft_reply": "Hi Alex,\n\nThanks for reaching out. A short call could help.",
        },
        context={"object_type": "gmail_draft", "source_agent": "gmail_triage"},
    )

    assert item.object_type == ApprovalQueueObjectType.GMAIL_DRAFT
    assert item.object_id == "gmail-msg-2"
    assert item.source_agent == "gmail_triage"
    assert item.approval_status == ApprovalQueueStatus.PENDING
    assert item.draft_text is not None


def test_create_approval_queue_item_tool_is_draft_only_json() -> None:
    payload = create_approval_queue_item(
        json.dumps(
            {
                "message_id": "gmail-msg-4",
                "summary": "Reply draft for review.",
                "draft_reply": "Hi Alex,\n\nThanks for reaching out.",
            }
        ),
        json.dumps({"object_type": "gmail_draft", "source_agent": "gmail_triage"}),
    )
    data = json.loads(payload)

    assert data["saved"] is False
    assert data["posted"] is False
    assert data["send_enabled"] is False
    assert data["item"]["object_type"] == "gmail_draft"
    assert data["item"]["source_agent"] == "gmail_triage"


def test_approval_queue_item_builds_slack_review_message_with_local_actions() -> None:
    item = _approval_item().model_copy(
        update={
            "risk_flags": ["legal_review"],
            "metadata": {
                "approval_scope": "send",
                "evidence": [
                    {
                        "text": "Inbound message asks for a human-reviewed reply.",
                        "source_id": "fixture:gmail",
                        "confidence": "high",
                    }
                ],
                "sources": [
                    {
                        "source_id": "fixture:gmail",
                        "title": "Gmail fixture",
                        "url": "fixture://sample_email_consulting.txt",
                    }
                ],
            },
        }
    )

    message = slack_review_message_from_approval_item(item)
    payload = message.as_payload()
    thread_text = "\n\n".join(payload["thread_blocks"])
    action_block = next(block for block in payload["root_blocks"] if block["type"] == "actions")
    action_texts = [
        element["text"]["text"]
        for element in action_block["elements"]
        if element["type"] == "button"
    ]

    assert "approval-test-1" in payload["root_text"]
    assert "Ready for approval" in payload["root_text"]
    assert "Gate: send" in payload["root_text"]
    assert "Slack buttons update only the referenced WorkItem gate" in payload["root_text"]
    assert "Reject:" not in payload["root_text"]
    assert action_texts[:2] == ["Approve draft review", "Revise draft"]
    assert "Evidence" in thread_text
    assert "Draft text" in thread_text
    assert "Hi Alex" in thread_text
    assert "fixture:gmail" not in payload["root_text"]
    assert "fixture:gmail" not in thread_text
    assert payload["object_type"] == "gmail_draft"
    assert payload["risk_flags"] == ["legal_review"]
    assert payload["approval_action_label"] == "draft review only"
    assert payload["send_enabled"] is False
    assert payload["interactive_actions_enabled"] is True


def test_build_queue_item_maps_legacy_decisions_and_does_not_store_inbound_body() -> None:
    approved = build_approval_queue_item(
        {
            "company_name": "Curebase",
            "email_subject": "Clinical trial workflow discussion",
            "email_body": "Outbound draft body for human review only.",
        },
        context={
            "object_type": "outreach_draft",
            "source_agent": "outreach_composer",
            "decision": "approved_for_send",
        },
    )
    gmail = build_approval_queue_item(
        {
            "message_id": "gmail-msg-3",
            "summary": "Inbound body should not become draft text.",
            "body": "Full inbound email body that must not be stored as review draft text.",
        },
        context={"object_type": "gmail_draft", "source_agent": "gmail_triage"},
    )

    assert approved.approval_status == ApprovalQueueStatus.APPROVED
    assert approval_queue_status_allows_sending(approved.approval_status) is False
    assert gmail.draft_text is None


def test_approval_queue_has_no_email_send_path() -> None:
    checked_paths = [
        PROJECT_ROOT / "scripts" / "list_approvals.py",
        PROJECT_ROOT / "scripts" / "update_approval.py",
        PROJECT_ROOT / "src" / "keystone_agents" / "storage" / "sqlite_store.py",
        PROJECT_ROOT / "src" / "keystone_agents" / "tools" / "approval_tool.py",
    ]

    for path in checked_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        assert not any(name.startswith("send") or "send_email" in name for name in names)
