from __future__ import annotations

import json
from typing import Any

import pytest

from keystone_agents.agent_tool_policy import tool_policy_for_agent
from keystone_agents.tools.gmail_tool import (
    GmailAPIError,
    GmailConfigurationError,
    GmailTool,
    modify_gmail_message_state,
)


class StatefulGmail(GmailTool):
    def __init__(self, labels: set[str]) -> None:
        super().__init__(live=True, access_token="test-token")
        self.labels = set(labels)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        if "Label_test" in labels:
            self._label_name_to_id["KBA_TEST_LABEL"] = "Label_test"

    def current_account_email(self) -> str:
        return "operator@example.com"

    def get_message(self, message_id: str) -> dict[str, Any]:
        return {
            "id": message_id,
            "threadId": "thread-1",
            "labelIds": sorted(self.labels),
        }

    def _label_id_for(self, label: str) -> str:
        return "Label_test" if label == "KBA_TEST_LABEL" else label

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, path, kwargs))
        if path.endswith("/trash"):
            self.labels.add("TRASH")
        elif path.endswith("/untrash"):
            self.labels.discard("TRASH")
        elif path.endswith("/modify"):
            payload = kwargs["json"]
            self.labels.update(payload["addLabelIds"])
            self.labels.difference_update(payload["removeLabelIds"])
        return {"id": "message-1", "labelIds": sorted(self.labels)}


@pytest.mark.parametrize(
    ("operation", "initial", "target", "present", "path_suffix"),
    [
        ("archive", {"INBOX"}, "INBOX", False, "/modify"),
        ("unarchive", set(), "INBOX", True, "/modify"),
        ("mark_read", {"UNREAD"}, "UNREAD", False, "/modify"),
        ("mark_unread", set(), "UNREAD", True, "/modify"),
        ("star", set(), "STARRED", True, "/modify"),
        ("unstar", {"STARRED"}, "STARRED", False, "/modify"),
        ("mark_important", set(), "IMPORTANT", True, "/modify"),
        ("mark_not_important", {"IMPORTANT"}, "IMPORTANT", False, "/modify"),
        ("trash", {"INBOX"}, "TRASH", True, "/trash"),
        ("restore", {"TRASH"}, "TRASH", False, "/untrash"),
    ],
)
def test_mailbox_state_operation_writes_and_verifies(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    initial: set[str],
    target: str,
    present: bool,
    path_suffix: str,
) -> None:
    monkeypatch.setenv("KEYSTONE_GMAIL_ALLOW_MAILBOX_WRITES", "true")
    gmail = StatefulGmail(initial)

    result = gmail.modify_message_state(
        "message-1",
        operation,  # type: ignore[arg-type]
        expected_account="operator@example.com",
        approval_reference=f"operator-command:{operation}",
    )

    assert result["status"] == "message_state_modified"
    assert result["verification"]["passed"] is True
    assert (target in result["after_label_ids"]) is present
    assert gmail.calls[0][1].endswith(path_suffix)
    assert result["send_enabled"] is False
    assert result["sent"] is False


@pytest.mark.parametrize(
    ("operation", "initial", "present"),
    [
        ("add_label", set(), True),
        ("remove_label", {"Label_test"}, False),
    ],
)
def test_custom_label_add_remove_is_verified(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    initial: set[str],
    present: bool,
) -> None:
    monkeypatch.setenv("KEYSTONE_GMAIL_ALLOW_MAILBOX_WRITES", "true")
    gmail = StatefulGmail(initial)

    result = gmail.modify_message_state(
        "message-1",
        operation,  # type: ignore[arg-type]
        label="KBA_TEST_LABEL",
        approval_reference=f"operator-command:{operation}",
    )

    assert ("Label_test" in result["after_label_ids"]) is present
    assert result["verification"]["target_label_id"] == "Label_test"


def test_mailbox_state_dry_run_is_inert() -> None:
    result = GmailTool(live=False).modify_message_state(
        "message-1",
        "archive",
        approval_reference="operator-command:archive",
    )

    assert result["status"] == "dry-run"
    assert result["provider_write"] is False
    assert result["verification"]["passed"] is False


def test_mailbox_state_live_requires_dedicated_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEYSTONE_GMAIL_ALLOW_MAILBOX_WRITES", raising=False)
    gmail = StatefulGmail({"INBOX"})

    with pytest.raises(GmailConfigurationError, match="ALLOW_MAILBOX_WRITES"):
        gmail.modify_message_state(
            "message-1",
            "archive",
            approval_reference="operator-command:archive",
        )

    assert gmail.calls == []


def test_mailbox_state_live_requires_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYSTONE_GMAIL_ALLOW_MAILBOX_WRITES", "true")
    gmail = StatefulGmail({"INBOX"})

    with pytest.raises(ValueError, match="approval_reference"):
        gmail.modify_message_state("message-1", "archive")

    assert gmail.calls == []


def test_mailbox_state_rejects_account_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYSTONE_GMAIL_ALLOW_MAILBOX_WRITES", "true")
    gmail = StatefulGmail({"INBOX"})

    with pytest.raises(GmailConfigurationError, match="authenticated as"):
        gmail.modify_message_state(
            "message-1",
            "archive",
            expected_account="wrong@example.com",
            approval_reference="operator-command:archive",
        )

    assert gmail.calls == []


def test_mailbox_state_rejects_unknown_operation() -> None:
    gmail = GmailTool(live=False)

    with pytest.raises(ValueError, match="Unsupported Gmail"):
        gmail.modify_message_state(
            "message-1",
            "permanently_delete",  # type: ignore[arg-type]
            approval_reference="operator-command:delete",
        )


def test_remove_missing_custom_label_does_not_create_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_GMAIL_ALLOW_MAILBOX_WRITES", "true")
    gmail = StatefulGmail(set())
    gmail._label_id_for = GmailTool._label_id_for.__get__(gmail, StatefulGmail)  # type: ignore[method-assign]
    gmail._existing_label_id_for = GmailTool._existing_label_id_for.__get__(  # type: ignore[method-assign]
        gmail, StatefulGmail
    )

    def list_no_labels(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        gmail.calls.append((method, path, kwargs))
        if path == "labels":
            return {"labels": []}
        raise AssertionError(f"Unexpected write call: {method} {path}")

    gmail._request = list_no_labels  # type: ignore[method-assign]
    with pytest.raises(GmailAPIError, match="does not exist"):
        gmail.modify_message_state(
            "message-1",
            "remove_label",
            label="MISSING_TEST_LABEL",
            approval_reference="operator-command:remove-label",
        )

    assert gmail.calls == [("GET", "labels", {"operation": "list labels"})]


def test_mailbox_state_rejects_unverified_readback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYSTONE_GMAIL_ALLOW_MAILBOX_WRITES", "true")
    gmail = StatefulGmail({"INBOX"})

    def ignore_write(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        gmail.calls.append((method, path, kwargs))
        return {}

    gmail._request = ignore_write  # type: ignore[method-assign]
    with pytest.raises(GmailAPIError, match="did not verify"):
        gmail.modify_message_state(
            "message-1",
            "archive",
            approval_reference="operator-command:archive",
        )


def test_agent_tool_wrapper_is_dry_run_without_live_flag() -> None:
    payload = json.loads(
        modify_gmail_message_state(
            message_id="message-1",
            operation="star",
            expected_account="operator@example.com",
            approval_reference="operator-command:star",
        )
    )
    assert payload["status"] == "dry-run"
    assert payload["provider_write"] is False


def test_mailbox_mutation_is_owned_only_by_gmail_triage() -> None:
    gmail_policy = tool_policy_for_agent("gmail_triage")
    assert gmail_policy is not None
    assert "modify_gmail_message_state" in gmail_policy.allowed_tool_names

    for agent_name in ("outreach_composer", "chief_of_staff", "orchestrator"):
        policy = tool_policy_for_agent(agent_name)
        assert policy is not None
        assert "modify_gmail_message_state" not in policy.allowed_tool_names
