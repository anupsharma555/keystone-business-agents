from __future__ import annotations

from typing import Any

from scripts.run_gmail_mailbox_state_lifecycle import execute_lifecycle


class FakeLifecycleGmail:
    def __init__(self) -> None:
        self.labels = {"INBOX", "UNREAD"}

    def search_message_summaries(self, **_: Any) -> list[dict[str, str]]:
        return [{"id": "message-1", "subject": "KBA_TEST_EMAIL lifecycle", "snippet": ""}]

    def get_message(self, message_id: str) -> dict[str, Any]:
        return {"id": message_id, "threadId": "thread-1", "labelIds": sorted(self.labels)}

    def modify_message_state(
        self,
        message_id: str,
        operation: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        label = str(kwargs.get("label") or "")
        mapping = {
            "archive": ("INBOX", False),
            "unarchive": ("INBOX", True),
            "mark_read": ("UNREAD", False),
            "mark_unread": ("UNREAD", True),
            "star": ("STARRED", True),
            "unstar": ("STARRED", False),
            "mark_important": ("IMPORTANT", True),
            "mark_not_important": ("IMPORTANT", False),
            "trash": ("TRASH", True),
            "restore": ("TRASH", False),
            "add_label": (label, True),
            "remove_label": (label, False),
        }
        target, present = mapping[operation]
        if present:
            self.labels.add(target)
        else:
            self.labels.discard(target)
        return {
            "status": "message_state_modified",
            "message_id": message_id,
            "provider_write": True,
            "verification": {"passed": True},
            "send_enabled": False,
            "sent": False,
        }


def test_lifecycle_restores_initial_state_and_persists_no_content() -> None:
    payload = execute_lifecycle(
        FakeLifecycleGmail(),  # type: ignore[arg-type]
        query='"KBA_TEST_EMAIL"',
        expected_account="operator@example.com",
        approval_reference="operator-command:anu-221-provider-lifecycle",
    )

    assert payload["status"] == "pass"
    assert payload["operation_count"] == 12
    assert payload["final_state_restored"] is True
    assert payload["test_label_absent"] is True
    assert payload["openai_requests"] == 0
    assert payload["email_sent"] is False
    assert payload["message_body_persisted"] is False
    assert "message-1" not in str(payload)
