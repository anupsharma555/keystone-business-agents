"""Approval-gated Gmail draft create/update lifecycle execution."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Protocol

GMAIL_TEST_DRAFT_MARKER = "KBA_TEST_DRAFT"
GMAIL_TEST_EMAIL_MARKER = "KBA_TEST_EMAIL"
GMAIL_TEST_DRAFT_DELETE_ENV = "KEYSTONE_GMAIL_ALLOW_TEST_DRAFT_DELETES"
GMAIL_TEST_SEND_ENV = "KEYSTONE_GMAIL_ALLOW_TEST_SENDS"
GMAIL_TEST_SEND_RECIPIENT_ENV = "KEYSTONE_GMAIL_TEST_SEND_RECIPIENT"
GMAIL_TEST_SEND_MAX_ENV = "KEYSTONE_GMAIL_TEST_SEND_MAX"
GMAIL_DRAFT_ATTACHMENT_ENV = "KEYSTONE_GMAIL_ALLOW_DRAFT_ATTACHMENTS"
GMAIL_DRAFT_ATTACHMENT_RECIPIENT_ENV = "KEYSTONE_GMAIL_DRAFT_ATTACHMENT_RECIPIENT"
GMAIL_OPERATOR_APPROVAL_ENV = "KEYSTONE_GMAIL_OPERATOR_APPROVAL_REFERENCE"


class GmailDraftProvider(Protocol):
    live: bool

    def current_account_email(self) -> str: ...

    def create_draft_reply(self, message_id: str, body: str) -> dict[str, Any]: ...

    def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        expected_account: str | None = None,
    ) -> dict[str, Any]: ...

    def get_draft(self, draft_id: str) -> dict[str, Any]: ...

    def create_draft_with_attachment(
        self,
        to: str,
        subject: str,
        body: str,
        attachment_path: str,
        *,
        expected_account: str | None = None,
    ) -> dict[str, Any]: ...

    def update_draft_with_attachment(
        self,
        draft_id: str,
        to: str,
        subject: str,
        body: str,
        attachment_path: str,
        *,
        expected_account: str | None = None,
    ) -> dict[str, Any]: ...

    def get_draft_attachment(self, draft_id: str, filename: str) -> dict[str, Any]: ...

    def list_recent_drafts(self, *, max_results: int = 20) -> list[dict[str, Any]]: ...

    def update_draft(
        self,
        draft_id: str,
        to: str,
        subject: str,
        body: str,
        *,
        expected_account: str | None = None,
    ) -> dict[str, Any]: ...

    def delete_draft(
        self,
        draft_id: str,
        *,
        expected_account: str | None = None,
    ) -> dict[str, Any]: ...

    def draft_exists(self, draft_id: str) -> bool: ...

    def send_draft(
        self,
        draft_id: str,
        *,
        expected_account: str,
    ) -> dict[str, Any]: ...

    def get_message(self, message_id: str) -> dict[str, Any]: ...


def execute_gmail_test_draft_lifecycle(
    gmail: GmailDraftProvider,
    *,
    marker: str,
    expected_account: str,
    recipient: str,
    approval_reference: str,
) -> dict[str, Any]:
    """Create, verify, update, verify, and remove one exact marked test draft."""

    clean_marker = " ".join(str(marker or "").split()).strip()
    account = expected_account.strip()
    to = recipient.strip()
    approval = str(
        approval_reference or os.getenv(GMAIL_OPERATOR_APPROVAL_ENV, "")
    ).strip()
    if GMAIL_TEST_DRAFT_MARKER not in clean_marker:
        raise ValueError(
            f"Gmail test-draft lifecycle marker must contain {GMAIL_TEST_DRAFT_MARKER}."
        )
    if not account or not to:
        raise ValueError(
            "Gmail test-draft lifecycle requires an exact account and draft recipient."
        )
    if not approval:
        raise RuntimeError(
            "Gmail test-draft lifecycle requires a non-empty approval_reference."
        )
    if gmail.live and not _truthy(os.getenv(GMAIL_TEST_DRAFT_DELETE_ENV)):
        raise RuntimeError(
            "Gmail test-draft lifecycle requires its cleanup gate before create. "
            f"Set {GMAIL_TEST_DRAFT_DELETE_ENV}=true for the approved lifecycle window."
        )

    draft_id = ""
    create_result: dict[str, Any] = {}
    update_result: dict[str, Any] = {}
    delete_result: dict[str, Any] = {}
    failure = ""
    try:
        create_result = execute_approved_gmail_draft_action(
            gmail,
            to=to,
            subject=f"{clean_marker} operational validation",
            body=(
                f"{clean_marker}\n"
                "Created for bounded Gmail draft lifecycle validation."
            ),
            expected_account=account,
            approval_reference=f"{approval}:create",
        )
        draft_id = str(create_result.get("draft_id") or "").strip()
        if not gmail.live:
            return {
                "status": "dry-run",
                "operation": "test_draft_lifecycle",
                "required_marker": GMAIL_TEST_DRAFT_MARKER,
                "approval_reference": approval,
                "openai_requests": 0,
                "sent": False,
                "send_enabled": False,
                "create": _gmail_lifecycle_step_receipt(create_result),
            }
        if not draft_id or not _gmail_verification_passed(create_result):
            failure = "Gmail test-draft create did not pass read-back verification."
        else:
            update_result = execute_approved_gmail_draft_action(
                gmail,
                to=to,
                subject=f"{clean_marker} operational validation updated",
                body=f"{clean_marker}\nModified in place and ready for verified cleanup.",
                expected_account=account,
                approval_reference=f"{approval}:update",
                draft_id=draft_id,
            )
            if not _gmail_verification_passed(update_result):
                failure = "Gmail test-draft update did not pass read-back verification."
    except Exception as exc:  # cleanup must still run after a partial lifecycle
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if gmail.live and draft_id:
            try:
                delete_result = delete_approved_gmail_test_draft(
                    gmail,
                    draft_id=draft_id,
                    expected_account=account,
                    approval_reference=f"{approval}:delete",
                )
            except Exception as exc:
                delete_result = {
                    "status": "failed",
                    "operation": "delete_test_draft",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "verification": {"passed": False},
                    "sent": False,
                    "send_enabled": False,
                }

    create_passed = _gmail_verification_passed(create_result)
    update_passed = _gmail_verification_passed(update_result)
    cleanup_passed = _gmail_verification_passed(delete_result)
    passed = create_passed and update_passed and cleanup_passed and not failure
    return {
        "status": "success" if passed else "failed",
        "operation": "test_draft_lifecycle",
        "draft_id": draft_id,
        "required_marker": GMAIL_TEST_DRAFT_MARKER,
        "approval_reference": approval,
        "failure": failure,
        "openai_requests": 0,
        "sent": False,
        "send_enabled": False,
        "create": _gmail_lifecycle_step_receipt(create_result),
        "update": _gmail_lifecycle_step_receipt(update_result),
        "delete": _gmail_lifecycle_step_receipt(delete_result),
        "verification": {
            "passed": passed,
            "create_read_back": create_passed,
            "same_draft_update_read_back": update_passed,
            "draft_absent_after_cleanup": cleanup_passed,
        },
    }


def _gmail_verification_passed(result: dict[str, Any]) -> bool:
    verification = result.get("verification")
    return bool(isinstance(verification, dict) and verification.get("passed"))


def _gmail_lifecycle_step_receipt(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "status",
            "operation",
            "draft_id",
            "approval_reference",
            "before",
            "after",
            "verification",
            "reason",
            "sent",
            "send_enabled",
        )
        if key in result
    }


def execute_approved_gmail_draft_action(
    gmail: GmailDraftProvider,
    *,
    to: str,
    subject: str,
    body: str,
    expected_account: str,
    approval_reference: str,
    draft_id: str = "",
) -> dict[str, Any]:
    """Create or update one approved draft and verify live provider state."""

    if not approval_reference.strip():
        raise RuntimeError("Gmail draft actions require a non-empty approval_reference.")
    recipient = to.strip()
    subject_text = subject.strip()
    body_text = body.strip()
    if not recipient or not subject_text or not body_text:
        raise ValueError("Gmail draft actions require recipient, subject, and body.")

    clean_draft_id = draft_id.strip()
    before: dict[str, Any] = {}
    before_receipt: dict[str, Any] = {}
    if clean_draft_id and gmail.live:
        before = gmail.get_draft(clean_draft_id)
        before_receipt = _draft_receipt(before)

    if clean_draft_id:
        result = gmail.update_draft(
            clean_draft_id,
            recipient,
            subject_text,
            body_text,
            expected_account=expected_account,
        )
        operation = "update"
    else:
        result = gmail.create_draft(
            recipient,
            subject_text,
            body_text,
            expected_account=expected_account,
        )
        operation = "create"

    resolved_draft_id = str(result.get("draft_id") or clean_draft_id).strip()
    after: dict[str, Any] = {}
    after_receipt: dict[str, Any] = {}
    verification = {
        "status": "preview" if not gmail.live else "not_verified",
        "passed": False,
        "draft_id_match": bool(resolved_draft_id),
        "recipient_match": False,
        "subject_match": False,
        "body_match": False,
        "sent_false": result.get("sent") is False,
    }
    if gmail.live and resolved_draft_id:
        after = gmail.get_draft(resolved_draft_id)
        after_receipt = _draft_receipt(after)
        verification.update(
            {
                "status": "verified",
                "draft_id_match": str(after.get("draft_id") or "") == resolved_draft_id,
                "recipient_match": _normalized(after.get("to")) == _normalized(recipient),
                "subject_match": _normalized(after.get("subject"))
                == _normalized(subject_text),
                "body_match": _normalized(after.get("body")) == _normalized(body_text),
                "sent_false": after.get("sent") is False and result.get("sent") is False,
            }
        )
        verification["passed"] = all(
            bool(verification[key])
            for key in (
                "draft_id_match",
                "recipient_match",
                "subject_match",
                "body_match",
                "sent_false",
            )
        )
        if not verification["passed"]:
            result = {**result, "status": "verification_failed"}

    receipt = {
        **result,
        "operation": operation,
        "approval_reference": approval_reference.strip(),
        "before": before_receipt,
        "after": after_receipt,
        "verification": verification,
        "sent": False,
        "send_enabled": False,
    }
    return receipt


def execute_approved_gmail_draft_reply_action(
    gmail: GmailDraftProvider,
    *,
    message_id: str,
    body: str,
    expected_to: str,
    expected_subject: str,
    expected_account: str,
    approval_reference: str,
) -> dict[str, Any]:
    """Create one thread-linked approved reply draft and verify provider state."""

    if not approval_reference.strip():
        raise RuntimeError("Gmail reply-draft actions require a non-empty approval_reference.")
    clean_message_id = message_id.strip()
    body_text = body.strip()
    account = expected_account.strip()
    if not clean_message_id or not body_text or not expected_to.strip() or not account:
        raise ValueError(
            "Gmail reply-draft actions require message, body, recipient, and account scope."
        )
    if gmail.live:
        actual_account = gmail.current_account_email().strip()
        if actual_account.casefold() != account.casefold():
            raise RuntimeError(
                "Gmail reply-draft account mismatch; no provider draft was created."
            )

    result = gmail.create_draft_reply(clean_message_id, body_text)
    resolved_draft_id = str(result.get("draft_id") or "").strip()
    verification = {
        "status": "preview" if not gmail.live else "not_verified",
        "passed": False,
        "draft_id_match": bool(resolved_draft_id),
        "recipient_match": False,
        "subject_match": False,
        "body_match": False,
        "sent_false": result.get("sent") is False,
    }
    after_receipt: dict[str, Any] = {}
    if gmail.live and resolved_draft_id:
        after = gmail.get_draft(resolved_draft_id)
        after_receipt = _draft_receipt(after)
        verification.update(
            {
                "status": "verified",
                "draft_id_match": str(after.get("draft_id") or "") == resolved_draft_id,
                "recipient_match": _normalized(after.get("to"))
                == _normalized(expected_to),
                "subject_match": _normalized(after.get("subject"))
                == _normalized(expected_subject),
                "body_match": _normalized(after.get("body")) == _normalized(body_text),
                "sent_false": after.get("sent") is False and result.get("sent") is False,
            }
        )
        verification["passed"] = all(
            bool(verification[key])
            for key in (
                "draft_id_match",
                "recipient_match",
                "subject_match",
                "body_match",
                "sent_false",
            )
        )

    return {
        **result,
        "status": (
            "draft_created"
            if verification["passed"]
            else result.get("status", "verification_failed")
        ),
        "operation": "create_reply_draft",
        "approval_reference": approval_reference.strip(),
        "gmail_account": account,
        "after": after_receipt,
        "verification": verification,
        "sent": False,
        "send_enabled": False,
    }


def execute_approved_gmail_draft_attachment_action(
    gmail: GmailDraftProvider,
    *,
    to: str,
    subject: str,
    body: str,
    attachment_path: str,
    expected_account: str,
    approval_reference: str,
    draft_id: str = "",
) -> dict[str, Any]:
    """Create or update one exact no-send draft with one verified local attachment."""

    approval = approval_reference.strip()
    recipient = to.strip()
    subject_text = subject.strip()
    body_text = body.strip()
    clean_path = str(attachment_path or "").strip()
    if not approval:
        raise RuntimeError("Gmail attachment-draft actions require approval_reference.")
    if not recipient or not subject_text or not body_text or not clean_path:
        raise ValueError(
            "Gmail attachment-draft actions require recipient, subject, body, and attachment."
        )
    if gmail.live:
        if not _truthy(os.getenv(GMAIL_DRAFT_ATTACHMENT_ENV)):
            raise RuntimeError(
                f"Gmail attachment drafts require {GMAIL_DRAFT_ATTACHMENT_ENV}=true."
            )
        allowed_recipient = os.getenv(GMAIL_DRAFT_ATTACHMENT_RECIPIENT_ENV, "").strip()
        if not allowed_recipient:
            raise RuntimeError(
                "Gmail attachment drafts require a configured exact recipient."
            )
        if recipient.casefold() != allowed_recipient.casefold():
            raise RuntimeError("Gmail attachment-draft recipient is not allowlisted.")

    clean_draft_id = draft_id.strip()
    before_receipt: dict[str, Any] = {}
    if clean_draft_id and gmail.live:
        before_receipt = _draft_receipt(gmail.get_draft(clean_draft_id))
    if clean_draft_id:
        result = gmail.update_draft_with_attachment(
            clean_draft_id,
            recipient,
            subject_text,
            body_text,
            clean_path,
            expected_account=expected_account,
        )
        operation = "update_draft_attachment"
    else:
        result = gmail.create_draft_with_attachment(
            recipient,
            subject_text,
            body_text,
            clean_path,
            expected_account=expected_account,
        )
        operation = "create_draft_attachment"
    resolved_draft_id = str(result.get("draft_id") or clean_draft_id).strip()
    verification = {
        "status": "preview" if not gmail.live else "not_verified",
        "passed": False,
        "draft_id_match": bool(resolved_draft_id),
        "recipient_match": False,
        "subject_match": False,
        "body_match": False,
        "attachment_filename_match": False,
        "attachment_size_match": False,
        "attachment_sha256_match": False,
        "sent_false": result.get("sent") is False,
    }
    after_receipt: dict[str, Any] = {}
    attachment_receipt: dict[str, Any] = {}
    if gmail.live and resolved_draft_id:
        after = gmail.get_draft(resolved_draft_id)
        after_receipt = _draft_receipt(after)
        filename = Path(clean_path).name
        attachment_receipt = gmail.get_draft_attachment(resolved_draft_id, filename)
        expected_size = int(result.get("attachment_size") or 0)
        expected_hash = str(result.get("attachment_sha256") or "")
        verification.update(
            {
                "status": "verified",
                "draft_id_match": str(after.get("draft_id") or "") == resolved_draft_id,
                "recipient_match": _normalized(after.get("to")) == _normalized(recipient),
                "subject_match": _normalized(after.get("subject"))
                == _normalized(subject_text),
                "body_match": _normalized(after.get("body")) == _normalized(body_text),
                "attachment_filename_match": attachment_receipt.get("filename") == filename,
                "attachment_size_match": attachment_receipt.get("size") == expected_size,
                "attachment_sha256_match": attachment_receipt.get("sha256")
                == expected_hash,
                "sent_false": after.get("sent") is False and result.get("sent") is False,
            }
        )
        verification["passed"] = all(
            bool(verification[key])
            for key in (
                "draft_id_match",
                "recipient_match",
                "subject_match",
                "body_match",
                "attachment_filename_match",
                "attachment_size_match",
                "attachment_sha256_match",
                "sent_false",
            )
        )
    return {
        **result,
        "status": (
            result.get("status", "dry-run")
            if not gmail.live or verification["passed"]
            else "verification_failed"
        ),
        "operation": operation,
        "approval_reference": approval,
        "before": before_receipt,
        "after": after_receipt,
        "attachment": attachment_receipt,
        "verification": verification,
        "sent": False,
        "send_enabled": False,
    }
def existing_provider_draft_id(metadata: dict[str, Any]) -> str:
    """Resolve a prior provider draft id from approval metadata."""

    for key in ("gmail_draft_id", "provider_draft_id", "existing_draft_id"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    result = metadata.get("gmail_draft_result")
    if isinstance(result, dict):
        return str(result.get("draft_id") or result.get("existing_draft_id") or "").strip()
    return ""


def resolve_unique_gmail_draft(
    gmail: GmailDraftProvider,
    *,
    subject_hint: str = "",
    recipient_hint: str = "",
    max_results: int = 20,
) -> dict[str, Any]:
    """Resolve one Gmail draft from natural subject/recipient hints without guessing."""

    subject = _normalized(subject_hint).removeprefix("re: ")
    recipient = _normalized(recipient_hint)
    if not subject and not recipient:
        return {
            "status": "clarification_required",
            "reason_code": "draft_reference_missing",
            "candidate_count": 0,
            "draft_id": "",
            "matches": [],
        }
    bounded_max = max(1, min(50, int(max_results)))
    candidates = gmail.list_recent_drafts(max_results=bounded_max)
    matches: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_subject = _normalized(candidate.get("subject")).removeprefix("re: ")
        candidate_recipient = _normalized(candidate.get("to"))
        if subject and subject not in candidate_subject:
            continue
        if recipient and recipient not in candidate_recipient:
            continue
        matches.append(candidate)

    receipts = [_draft_receipt(candidate) for candidate in matches]
    if len(matches) != 1:
        return {
            "status": "clarification_required" if matches else "blocked",
            "reason_code": "draft_target_ambiguous" if matches else "draft_target_not_found",
            "candidate_count": len(matches),
            "draft_id": "",
            "matches": receipts,
        }
    return {
        "status": "resolved",
        "reason_code": "",
        "candidate_count": 1,
        "draft_id": str(matches[0].get("draft_id") or ""),
        "matches": receipts,
    }


def delete_approved_gmail_test_draft(
    gmail: GmailDraftProvider,
    *,
    draft_id: str,
    expected_account: str,
    approval_reference: str,
) -> dict[str, Any]:
    """Delete one exact marked test draft and verify provider absence."""

    clean_draft_id = draft_id.strip()
    if not clean_draft_id:
        raise ValueError("Gmail test-draft deletion requires an exact draft_id.")
    if not approval_reference.strip():
        raise RuntimeError("Gmail test-draft deletion requires an approval_reference.")
    if not _truthy(os.getenv(GMAIL_TEST_DRAFT_DELETE_ENV)):
        raise RuntimeError(
            "Gmail test-draft deletion requires "
            f"{GMAIL_TEST_DRAFT_DELETE_ENV}=true."
        )
    if not gmail.live:
        return {
            "status": "dry-run",
            "operation": "delete_test_draft",
            "draft_id": clean_draft_id,
            "approval_reference": approval_reference.strip(),
            "verification": {"status": "preview", "passed": False, "absent": False},
            "sent": False,
            "send_enabled": False,
        }

    before = gmail.get_draft(clean_draft_id)
    subject = str(before.get("subject") or "")
    body = str(before.get("body") or "")
    if GMAIL_TEST_DRAFT_MARKER not in subject or GMAIL_TEST_DRAFT_MARKER not in body:
        raise RuntimeError(
            "Gmail test-draft deletion refused: both subject and body must contain "
            f"{GMAIL_TEST_DRAFT_MARKER}."
        )
    if before.get("sent") is not False:
        raise RuntimeError("Gmail test-draft deletion refused because sent=false was not verified.")

    deletion = gmail.delete_draft(
        clean_draft_id,
        expected_account=expected_account,
    )
    absent = not gmail.draft_exists(clean_draft_id)
    verification = {
        "status": "verified" if absent else "verification_failed",
        "passed": absent,
        "absent": absent,
        "draft_id_match": str(before.get("draft_id") or "") == clean_draft_id,
        "test_marker_verified": True,
        "sent_false": True,
    }
    return {
        **deletion,
        "status": "draft_deleted" if absent else "verification_failed",
        "operation": "delete_test_draft",
        "approval_reference": approval_reference.strip(),
        "before": _draft_receipt(before),
        "verification": verification,
        "sent": False,
        "send_enabled": False,
    }


def send_approved_gmail_test_draft(
    gmail: GmailDraftProvider,
    *,
    draft_id: str,
    expected_account: str,
    approval_reference: str,
    send_number: int = 1,
) -> dict[str, Any]:
    """Send one exact marked test draft and verify the sent provider message."""

    clean_draft_id = draft_id.strip()
    expected = expected_account.strip()
    approval = approval_reference.strip()
    if not clean_draft_id:
        raise ValueError("Gmail test send requires an exact draft_id.")
    if not expected:
        raise ValueError("Gmail test send requires expected_account.")
    if not approval:
        raise RuntimeError("Gmail test send requires approval_reference.")
    if not _truthy(os.getenv(GMAIL_TEST_SEND_ENV)):
        raise RuntimeError(f"Gmail test send requires {GMAIL_TEST_SEND_ENV}=true.")
    allowed_recipient = os.getenv(GMAIL_TEST_SEND_RECIPIENT_ENV, "").strip()
    if not allowed_recipient:
        raise RuntimeError(
            f"Gmail test send requires {GMAIL_TEST_SEND_RECIPIENT_ENV}."
        )
    try:
        max_sends = int(os.getenv(GMAIL_TEST_SEND_MAX_ENV, "0").strip() or "0")
    except ValueError as exc:
        raise RuntimeError(f"{GMAIL_TEST_SEND_MAX_ENV} must be an integer.") from exc
    if max_sends < 1 or max_sends > 2:
        raise RuntimeError(f"{GMAIL_TEST_SEND_MAX_ENV} must be between 1 and 2.")
    if send_number < 1 or send_number > max_sends:
        raise RuntimeError("Gmail test send exceeds the approved send count.")
    if not gmail.live:
        return {
            "status": "dry-run",
            "operation": "send_test_draft",
            "draft_id": clean_draft_id,
            "approval_reference": approval,
            "send_number": send_number,
            "sent": False,
            "send_enabled": False,
            "verification": {"status": "preview", "passed": False},
        }

    before = gmail.get_draft(clean_draft_id)
    recipient = str(before.get("to") or "").strip()
    subject = str(before.get("subject") or "")
    body = str(before.get("body") or "")
    if _normalized(recipient) != _normalized(allowed_recipient):
        raise RuntimeError(
            "Gmail test send refused: draft recipient is not the approved recipient."
        )
    if GMAIL_TEST_EMAIL_MARKER not in subject or GMAIL_TEST_EMAIL_MARKER not in body:
        raise RuntimeError(
            "Gmail test send refused: subject and body must contain "
            f"{GMAIL_TEST_EMAIL_MARKER}."
        )
    if before.get("sent") is not False:
        raise RuntimeError("Gmail test send refused because sent=false was not verified.")

    sent_result = gmail.send_draft(clean_draft_id, expected_account=expected)
    sent_message_id = str(sent_result.get("message_id") or "").strip()
    if not sent_message_id or sent_result.get("sent") is not True:
        raise RuntimeError("Gmail provider did not confirm the test draft send.")
    after = gmail.get_message(sent_message_id)
    draft_absent_after_send = not gmail.draft_exists(clean_draft_id)
    after_labels = {str(label) for label in after.get("labelIds", [])}
    verification = {
        "status": "verified",
        "passed": (
            _normalized(after.get("to")) == _normalized(recipient)
            and _normalized(after.get("subject")) == _normalized(subject)
            and _normalized(after.get("body")) == _normalized(body)
            and (not after_labels or "SENT" in after_labels)
            and draft_absent_after_send
        ),
        "message_id_match": str(after.get("id") or after.get("message_id") or "")
        == sent_message_id,
        "recipient_match": _normalized(after.get("to")) == _normalized(recipient),
        "subject_match": _normalized(after.get("subject")) == _normalized(subject),
        "body_match": _normalized(after.get("body")) == _normalized(body),
        "sent_label_present": not after_labels or "SENT" in after_labels,
        "draft_absent_after_send": draft_absent_after_send,
    }
    verification["passed"] = bool(
        verification["passed"] and verification["message_id_match"]
    )
    return {
        **sent_result,
        "status": "sent" if verification["passed"] else "verification_failed",
        "operation": "send_test_draft",
        "approval_reference": approval,
        "send_number": send_number,
        "before": _draft_receipt(before),
        "after": _sent_message_receipt(after),
        "verification": verification,
        "sent": True,
        "send_enabled": True,
    }


def _normalized(value: object) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def _draft_receipt(draft: dict[str, Any]) -> dict[str, Any]:
    """Return verification evidence without persisting private draft content."""

    normalized_body = _normalized(draft.get("body"))
    attachments = draft.get("attachments") if isinstance(draft.get("attachments"), list) else []
    receipt = {
        "draft_id": str(draft.get("draft_id") or ""),
        "message_id": str(draft.get("message_id") or ""),
        "to": str(draft.get("to") or ""),
        "subject": str(draft.get("subject") or ""),
        "body_sha256": hashlib.sha256(normalized_body.encode("utf-8")).hexdigest(),
        "sent": draft.get("sent") is True,
    }
    if attachments:
        receipt["attachment_count"] = len(attachments)
        receipt["attachments"] = [
            {
                "filename": str(item.get("filename") or ""),
                "mime_type": str(item.get("mime_type") or ""),
                "size": int(item.get("size") or 0),
            }
            for item in attachments
            if isinstance(item, dict)
        ]
    return receipt


def _sent_message_receipt(message: dict[str, Any]) -> dict[str, Any]:
    """Return sent-message verification without persisting the message body."""

    normalized_body = _normalized(message.get("body"))
    return {
        "message_id": str(message.get("id") or message.get("message_id") or ""),
        "thread_id": str(message.get("threadId") or message.get("thread_id") or ""),
        "to": str(message.get("to") or ""),
        "subject": str(message.get("subject") or ""),
        "body_sha256": hashlib.sha256(normalized_body.encode("utf-8")).hexdigest(),
        "sent_label_present": "SENT"
        in {str(label) for label in message.get("labelIds", [])},
    }


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
