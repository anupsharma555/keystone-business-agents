from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from keystone_agents.gmail_triage.draft_actions import (
    GMAIL_DRAFT_ATTACHMENT_ENV,
    GMAIL_DRAFT_ATTACHMENT_RECIPIENT_ENV,
    GMAIL_TEST_DRAFT_DELETE_ENV,
    GMAIL_TEST_DRAFT_MARKER,
    GMAIL_TEST_EMAIL_MARKER,
    GMAIL_TEST_SEND_ENV,
    GMAIL_TEST_SEND_MAX_ENV,
    GMAIL_TEST_SEND_RECIPIENT_ENV,
    delete_approved_gmail_test_draft,
    execute_approved_gmail_draft_action,
    execute_approved_gmail_draft_attachment_action,
    execute_approved_gmail_draft_reply_action,
    existing_provider_draft_id,
    resolve_unique_gmail_draft,
    send_approved_gmail_test_draft,
)


class FakeGmailDraftProvider:
    def __init__(self, *, live: bool) -> None:
        self.live = live
        self.drafts: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []

    def current_account_email(self) -> str:
        self.calls.append(("profile", "operator@example.com"))
        return "operator@example.com"

    def create_draft_reply(self, message_id: str, body: str) -> dict[str, Any]:
        self.calls.append(("create_reply", message_id))
        if self.live:
            self.drafts["draft-reply"] = {
                "draft_id": "draft-reply",
                "message_id": "message-reply",
                "to": "sender@example.com",
                "subject": "Re: Project follow-up",
                "body": body,
                "sent": False,
            }
        return {
            "status": "draft_created" if self.live else "dry-run",
            "draft_id": "draft-reply" if self.live else "",
            "message_id": message_id,
            "sent": False,
        }

    def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        expected_account: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("create", "draft-created"))
        if self.live:
            self.drafts["draft-created"] = {
                "draft_id": "draft-created",
                "message_id": "message-created",
                "to": to,
                "subject": subject,
                "body": body,
                "sent": False,
            }
        return {
            "status": "draft_created" if self.live else "dry-run",
            "draft_id": "draft-created" if self.live else "",
            "gmail_account": expected_account or "",
            "sent": False,
        }

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        self.calls.append(("get", draft_id))
        return dict(self.drafts[draft_id])

    def create_draft_with_attachment(
        self,
        to: str,
        subject: str,
        body: str,
        attachment_path: str,
        *,
        expected_account: str | None = None,
    ) -> dict[str, Any]:
        return self._write_draft_with_attachment(
            "draft-attachment",
            to,
            subject,
            body,
            attachment_path,
            expected_account=expected_account,
            operation="create_attachment",
        )

    def update_draft_with_attachment(
        self,
        draft_id: str,
        to: str,
        subject: str,
        body: str,
        attachment_path: str,
        *,
        expected_account: str | None = None,
    ) -> dict[str, Any]:
        return self._write_draft_with_attachment(
            draft_id,
            to,
            subject,
            body,
            attachment_path,
            expected_account=expected_account,
            operation="update_attachment",
        )

    def _write_draft_with_attachment(
        self,
        draft_id: str,
        to: str,
        subject: str,
        body: str,
        attachment_path: str,
        *,
        expected_account: str | None,
        operation: str,
    ) -> dict[str, Any]:
        self.calls.append((operation, draft_id))
        content = Path(attachment_path).read_bytes()
        filename = Path(attachment_path).name
        digest = hashlib.sha256(content).hexdigest()
        if self.live:
            self.drafts[draft_id] = {
                "draft_id": draft_id,
                "message_id": f"message-{draft_id}",
                "to": to,
                "subject": subject,
                "body": body,
                "attachments": [
                    {
                        "filename": filename,
                        "mime_type": "image/png",
                        "size": len(content),
                    }
                ],
                "attachment_content": content,
                "sent": False,
            }
        return {
            "status": "draft_updated" if operation.startswith("update") else "draft_created",
            "draft_id": draft_id,
            "gmail_account": expected_account or "",
            "attachment_filename": filename,
            "attachment_size": len(content),
            "attachment_sha256": digest,
            "sent": False,
        }

    def get_draft_attachment(self, draft_id: str, filename: str) -> dict[str, Any]:
        self.calls.append(("get_attachment", draft_id))
        content = self.drafts[draft_id]["attachment_content"]
        return {
            "status": "success",
            "draft_id": draft_id,
            "filename": filename,
            "mime_type": "image/png",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def list_recent_drafts(self, *, max_results: int = 20) -> list[dict[str, Any]]:
        self.calls.append(("list_drafts", str(max_results)))
        return [dict(draft) for draft in list(self.drafts.values())[:max_results]]

    def update_draft(
        self,
        draft_id: str,
        to: str,
        subject: str,
        body: str,
        *,
        expected_account: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("update", draft_id))
        if self.live:
            self.drafts[draft_id] = {
                "draft_id": draft_id,
                "message_id": "message-updated",
                "to": to,
                "subject": subject,
                "body": body,
                "sent": False,
            }
        return {
            "status": "draft_updated" if self.live else "dry-run",
            "draft_id": draft_id,
            "gmail_account": expected_account or "",
            "sent": False,
        }

    def delete_draft(
        self,
        draft_id: str,
        *,
        expected_account: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("delete", draft_id))
        if self.live:
            del self.drafts[draft_id]
        return {
            "status": "draft_deleted" if self.live else "dry-run",
            "draft_id": draft_id,
            "gmail_account": expected_account or "",
            "sent": False,
        }

    def draft_exists(self, draft_id: str) -> bool:
        self.calls.append(("exists", draft_id))
        return draft_id in self.drafts

    def send_draft(
        self,
        draft_id: str,
        *,
        expected_account: str,
    ) -> dict[str, Any]:
        self.calls.append(("send", draft_id))
        draft = self.drafts.pop(draft_id)
        self.messages["message-sent"] = {
            "id": "message-sent",
            "threadId": "thread-sent",
            "to": draft["to"],
            "subject": draft["subject"],
            "body": draft["body"],
            "labelIds": ["SENT"],
        }
        return {
            "status": "sent",
            "draft_id": draft_id,
            "message_id": "message-sent",
            "thread_id": "thread-sent",
            "gmail_account": expected_account,
            "sent": True,
        }

    def get_message(self, message_id: str) -> dict[str, Any]:
        self.calls.append(("get_message", message_id))
        return dict(self.messages[message_id])


def test_approved_gmail_draft_create_reads_back_and_verifies() -> None:
    gmail = FakeGmailDraftProvider(live=True)

    result = execute_approved_gmail_draft_action(
        gmail,
        to="reviewer@example.com",
        subject="Review subject",
        body="Draft body for review.",
        expected_account="operator@example.com",
        approval_reference="approval-create",
    )

    assert result["status"] == "draft_created"
    assert result["operation"] == "create"
    assert result["verification"]["passed"] is True
    assert result["sent"] is False
    assert gmail.calls == [("create", "draft-created"), ("get", "draft-created")]


def test_approved_gmail_reply_draft_uses_thread_and_verifies_without_send() -> None:
    gmail = FakeGmailDraftProvider(live=True)

    result = execute_approved_gmail_draft_reply_action(
        gmail,
        message_id="message-source",
        body="Thanks for the note. I will review this and follow up.",
        expected_to="sender@example.com",
        expected_subject="Re: Project follow-up",
        expected_account="operator@example.com",
        approval_reference="operator-command:gmail-draft:abc123",
    )

    assert result["operation"] == "create_reply_draft"
    assert result["draft_id"] == "draft-reply"
    assert result["verification"]["passed"] is True
    assert result["sent"] is False
    assert result["send_enabled"] is False
    assert "body" not in result["after"]
    assert gmail.calls == [
        ("profile", "operator@example.com"),
        ("create_reply", "message-source"),
        ("get", "draft-reply"),
    ]


def test_natural_draft_reference_resolves_one_subject_and_recipient_match() -> None:
    gmail = FakeGmailDraftProvider(live=True)
    gmail.drafts = {
        "draft-one": {
            "draft_id": "draft-one",
            "message_id": "message-one",
            "to": "reviewer@example.com",
            "subject": "Re: Project follow-up",
            "body": "First draft body.",
            "sent": False,
        },
        "draft-two": {
            "draft_id": "draft-two",
            "message_id": "message-two",
            "to": "other@example.com",
            "subject": "Re: Project follow-up",
            "body": "Other draft body.",
            "sent": False,
        },
    }

    result = resolve_unique_gmail_draft(
        gmail,
        subject_hint="Project follow-up",
        recipient_hint="reviewer@example.com",
    )

    assert result["status"] == "resolved"
    assert result["draft_id"] == "draft-one"
    assert result["candidate_count"] == 1
    assert "body" not in result["matches"][0]


def test_natural_draft_reference_blocks_ambiguous_or_missing_targets() -> None:
    gmail = FakeGmailDraftProvider(live=True)
    gmail.drafts = {
        "draft-one": {
            "draft_id": "draft-one",
            "message_id": "message-one",
            "to": "reviewer@example.com",
            "subject": "Re: Project follow-up",
            "body": "First draft body.",
            "sent": False,
        },
        "draft-two": {
            "draft_id": "draft-two",
            "message_id": "message-two",
            "to": "other@example.com",
            "subject": "Re: Project follow-up",
            "body": "Other draft body.",
            "sent": False,
        },
    }

    ambiguous = resolve_unique_gmail_draft(gmail, subject_hint="Project follow-up")
    missing = resolve_unique_gmail_draft(gmail)

    assert ambiguous["status"] == "clarification_required"
    assert ambiguous["reason_code"] == "draft_target_ambiguous"
    assert ambiguous["candidate_count"] == 2
    assert ambiguous["draft_id"] == ""
    assert missing["reason_code"] == "draft_reference_missing"
    assert missing["draft_id"] == ""


def test_approved_gmail_draft_update_reuses_id_and_verifies_modification() -> None:
    gmail = FakeGmailDraftProvider(live=True)
    gmail.drafts["draft-existing"] = {
        "draft_id": "draft-existing",
        "message_id": "message-before",
        "to": "reviewer@example.com",
        "subject": "Original subject",
        "body": "Original body.",
        "sent": False,
    }

    result = execute_approved_gmail_draft_action(
        gmail,
        to="reviewer@example.com",
        subject="Updated subject",
        body="Shorter and warmer body.",
        expected_account="operator@example.com",
        approval_reference="approval-update",
        draft_id="draft-existing",
    )

    assert result["status"] == "draft_updated"
    assert result["operation"] == "update"
    assert result["before"]["subject"] == "Original subject"
    assert result["after"]["subject"] == "Updated subject"
    assert "body" not in result["before"]
    assert "body" not in result["after"]
    assert result["before"]["body_sha256"] != result["after"]["body_sha256"]
    assert result["verification"]["passed"] is True
    assert result["draft_id"] == "draft-existing"
    assert result["sent"] is False
    assert gmail.calls == [
        ("get", "draft-existing"),
        ("update", "draft-existing"),
        ("get", "draft-existing"),
    ]


def test_gmail_draft_action_requires_approval_reference() -> None:
    with pytest.raises(RuntimeError, match="approval_reference"):
        execute_approved_gmail_draft_action(
            FakeGmailDraftProvider(live=False),
            to="reviewer@example.com",
            subject="Review subject",
            body="Draft body for review.",
            expected_account="operator@example.com",
            approval_reference="",
        )


def test_gmail_attachment_draft_create_and_update_verify_same_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = tmp_path / "KBA_TEST_SLIDE-example.png"
    attachment.write_bytes(b"\x89PNG\r\n\x1a\nexample")
    monkeypatch.setenv(GMAIL_DRAFT_ATTACHMENT_ENV, "true")
    monkeypatch.setenv(GMAIL_DRAFT_ATTACHMENT_RECIPIENT_ENV, "reviewer@example.com")
    gmail = FakeGmailDraftProvider(live=True)

    created = execute_approved_gmail_draft_attachment_action(
        gmail,
        to="reviewer@example.com",
        subject="KBA_TEST_DRAFT slide review",
        body="KBA_TEST_DRAFT initial slide copy.",
        attachment_path=str(attachment),
        expected_account="operator@example.com",
        approval_reference="operator:create-attachment",
    )
    updated = execute_approved_gmail_draft_attachment_action(
        gmail,
        to="reviewer@example.com",
        subject="KBA_TEST_DRAFT updated slide review",
        body="KBA_TEST_DRAFT updated note for the same slide copy.",
        attachment_path=str(attachment),
        expected_account="operator@example.com",
        approval_reference="operator:update-attachment",
        draft_id=created["draft_id"],
    )

    assert created["verification"]["passed"] is True
    assert updated["verification"]["passed"] is True
    assert created["draft_id"] == updated["draft_id"] == "draft-attachment"
    assert updated["attachment"]["sha256"] == hashlib.sha256(
        attachment.read_bytes()
    ).hexdigest()
    assert updated["sent"] is False
    assert updated["send_enabled"] is False


def test_gmail_attachment_draft_blocks_unapproved_recipient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = tmp_path / "KBA_TEST_SLIDE-example.png"
    attachment.write_bytes(b"png")
    monkeypatch.setenv(GMAIL_DRAFT_ATTACHMENT_ENV, "true")
    monkeypatch.setenv(GMAIL_DRAFT_ATTACHMENT_RECIPIENT_ENV, "reviewer@example.com")

    with pytest.raises(RuntimeError, match="recipient is not allowlisted"):
        execute_approved_gmail_draft_attachment_action(
            FakeGmailDraftProvider(live=True),
            to="other@example.com",
            subject="KBA_TEST_DRAFT slide review",
            body="KBA_TEST_DRAFT slide copy.",
            attachment_path=str(attachment),
            expected_account="operator@example.com",
            approval_reference="operator:create-attachment",
        )


def test_existing_provider_draft_id_reads_prior_result() -> None:
    assert (
        existing_provider_draft_id(
            {"gmail_draft_result": {"status": "draft_created", "draft_id": "draft-123"}}
        )
        == "draft-123"
    )


def test_approved_marked_gmail_test_draft_delete_verifies_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GMAIL_TEST_DRAFT_DELETE_ENV, "true")
    gmail = FakeGmailDraftProvider(live=True)
    gmail.drafts["draft-test"] = {
        "draft_id": "draft-test",
        "message_id": "message-test",
        "to": "reviewer@example.com",
        "subject": f"{GMAIL_TEST_DRAFT_MARKER} validation",
        "body": f"{GMAIL_TEST_DRAFT_MARKER}\nSynthetic validation body.",
        "sent": False,
    }

    result = delete_approved_gmail_test_draft(
        gmail,
        draft_id="draft-test",
        expected_account="operator@example.com",
        approval_reference="approval-delete",
    )

    assert result["status"] == "draft_deleted"
    assert result["verification"]["passed"] is True
    assert result["verification"]["absent"] is True
    assert "body" not in result["before"]
    assert result["sent"] is False
    assert gmail.calls == [
        ("get", "draft-test"),
        ("delete", "draft-test"),
        ("exists", "draft-test"),
    ]


def test_gmail_test_draft_delete_requires_marker_gate_and_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gmail = FakeGmailDraftProvider(live=True)
    gmail.drafts["draft-test"] = {
        "draft_id": "draft-test",
        "message_id": "message-test",
        "to": "reviewer@example.com",
        "subject": "Ordinary draft",
        "body": "Ordinary body.",
        "sent": False,
    }
    monkeypatch.delenv(GMAIL_TEST_DRAFT_DELETE_ENV, raising=False)
    with pytest.raises(RuntimeError, match=GMAIL_TEST_DRAFT_DELETE_ENV):
        delete_approved_gmail_test_draft(
            gmail,
            draft_id="draft-test",
            expected_account="operator@example.com",
            approval_reference="approval-delete",
        )
    monkeypatch.setenv(GMAIL_TEST_DRAFT_DELETE_ENV, "true")
    with pytest.raises(RuntimeError, match="approval_reference"):
        delete_approved_gmail_test_draft(
            gmail,
            draft_id="draft-test",
            expected_account="operator@example.com",
            approval_reference="",
        )
    with pytest.raises(RuntimeError, match=GMAIL_TEST_DRAFT_MARKER):
        delete_approved_gmail_test_draft(
            gmail,
            draft_id="draft-test",
            expected_account="operator@example.com",
            approval_reference="approval-delete",
        )


def test_approved_marked_gmail_test_send_verifies_exact_sent_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GMAIL_TEST_SEND_ENV, "true")
    monkeypatch.setenv(GMAIL_TEST_SEND_RECIPIENT_ENV, "reviewer@example.com")
    monkeypatch.setenv(GMAIL_TEST_SEND_MAX_ENV, "2")
    gmail = FakeGmailDraftProvider(live=True)
    gmail.drafts["draft-send"] = {
        "draft_id": "draft-send",
        "message_id": "message-draft",
        "to": "reviewer@example.com",
        "subject": f"{GMAIL_TEST_EMAIL_MARKER} validation",
        "body": f"{GMAIL_TEST_EMAIL_MARKER}\nSynthetic validation response.",
        "sent": False,
    }

    result = send_approved_gmail_test_draft(
        gmail,
        draft_id="draft-send",
        expected_account="operator@example.com",
        approval_reference="approval-send",
        send_number=1,
    )

    assert result["status"] == "sent"
    assert result["operation"] == "send_test_draft"
    assert result["verification"]["passed"] is True
    assert result["verification"]["message_id_match"] is True
    assert result["verification"]["draft_absent_after_send"] is True
    assert result["after"]["sent_label_present"] is True
    assert "body" not in result["before"]
    assert "body" not in result["after"]
    assert result["sent"] is True
    assert result["send_enabled"] is True
    assert gmail.calls == [
        ("get", "draft-send"),
        ("send", "draft-send"),
        ("get_message", "message-sent"),
        ("exists", "draft-send"),
    ]


def test_gmail_test_send_blocks_unapproved_recipient_marker_and_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gmail = FakeGmailDraftProvider(live=True)
    gmail.drafts["draft-send"] = {
        "draft_id": "draft-send",
        "message_id": "message-draft",
        "to": "other@example.com",
        "subject": "Ordinary subject",
        "body": "Ordinary body.",
        "sent": False,
    }
    monkeypatch.delenv(GMAIL_TEST_SEND_ENV, raising=False)
    with pytest.raises(RuntimeError, match=GMAIL_TEST_SEND_ENV):
        send_approved_gmail_test_draft(
            gmail,
            draft_id="draft-send",
            expected_account="operator@example.com",
            approval_reference="approval-send",
        )

    monkeypatch.setenv(GMAIL_TEST_SEND_ENV, "true")
    monkeypatch.setenv(GMAIL_TEST_SEND_RECIPIENT_ENV, "reviewer@example.com")
    monkeypatch.setenv(GMAIL_TEST_SEND_MAX_ENV, "2")
    with pytest.raises(RuntimeError, match="approved recipient"):
        send_approved_gmail_test_draft(
            gmail,
            draft_id="draft-send",
            expected_account="operator@example.com",
            approval_reference="approval-send",
        )

    gmail.drafts["draft-send"]["to"] = "reviewer@example.com"
    with pytest.raises(RuntimeError, match=GMAIL_TEST_EMAIL_MARKER):
        send_approved_gmail_test_draft(
            gmail,
            draft_id="draft-send",
            expected_account="operator@example.com",
            approval_reference="approval-send",
        )

    with pytest.raises(RuntimeError, match="approved send count"):
        send_approved_gmail_test_draft(
            FakeGmailDraftProvider(live=False),
            draft_id="draft-send",
            expected_account="operator@example.com",
            approval_reference="approval-send",
            send_number=3,
        )
