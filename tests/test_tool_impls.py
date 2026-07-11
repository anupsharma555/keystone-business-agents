from __future__ import annotations

import json

import pytest

from keystone_agents.config import Settings, load_settings, require_live_mode
from keystone_agents.sdk import ToolGuardrailViolation
from keystone_agents.tools.gmail_tool import (
    GmailConfigurationError,
    GmailTool,
    create_gmail_draft_reply,
)
from keystone_agents.tools.serper_tool import SerperConfigurationError, SerperTool


def test_dry_run_is_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEYSTONE_LIVE_MODE", raising=False)
    settings = load_settings()

    assert settings.live_mode is False


def test_gmail_tools_use_fixtures_and_never_send(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEYSTONE_LIVE_MODE", raising=False)

    messages = GmailTool(live=False).list_recent_messages(max_results=1)
    draft = json.loads(
        create_gmail_draft_reply(
            message_id="dry-msg-001",
            body="Thanks for reaching out. We can review and follow up after human approval.",
        )
    )

    assert messages == []
    assert draft["status"] == "dry-run"
    assert draft["sent"] is False
    assert draft["approval_required"] is True


def test_gmail_create_draft_dry_run_records_target_account() -> None:
    draft = GmailTool(live=False).create_draft(
        to="andy@example.com",
        subject="Draft subject",
        body="Draft body for approval only.",
        expected_account="operator@example.com",
    )

    assert draft["status"] == "dry-run"
    assert draft["gmail_account"] == "operator@example.com"
    assert draft["sent"] is False
    assert draft["approval_required"] is True


def test_gmail_create_draft_live_requires_expected_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = GmailTool(live=True, access_token="fake-token")
    monkeypatch.setattr(tool, "current_account_email", lambda: "other@example.com")

    with pytest.raises(GmailConfigurationError, match="operator@example.com"):
        tool.create_draft(
            to="andy@example.com",
            subject="Draft subject",
            body="Draft body for approval only.",
            expected_account="operator@example.com",
        )


def test_gmail_update_draft_dry_run_preserves_no_send_boundary() -> None:
    result = GmailTool(live=False).update_draft(
        "draft-001",
        "reviewer@example.com",
        "Updated subject",
        "Updated body for review only.",
        expected_account="operator@example.com",
    )

    assert result["status"] == "dry-run"
    assert result["draft_id"] == "draft-001"
    assert result["gmail_account"] == "operator@example.com"
    assert result["sent"] is False
    assert result["approval_required"] is True


def test_gmail_get_draft_dry_run_is_read_only() -> None:
    result = GmailTool(live=False).get_draft("draft-001")

    assert result == {
        "status": "dry-run",
        "draft_id": "draft-001",
        "message_id": "",
        "to": "",
        "subject": "",
        "body": "",
        "attachments": [],
        "attachment_count": 0,
        "sent": False,
    }


def test_gmail_list_recent_drafts_is_bounded_and_reads_exact_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = GmailTool(live=True, access_token="fake-token")
    requests: list[tuple[str, str, dict[str, object]]] = []

    def fake_request(
        method: str, path: str, *, operation: str, **kwargs: object
    ) -> dict[str, object]:
        requests.append((method, path, kwargs))
        return {"drafts": [{"id": "draft-1"}, {"id": "draft-2"}]}

    monkeypatch.setattr(tool, "_request", fake_request)
    monkeypatch.setattr(
        tool,
        "get_draft",
        lambda draft_id: {
            "status": "success",
            "draft_id": draft_id,
            "message_id": f"message-{draft_id}",
            "to": "reviewer@example.com",
            "subject": "Project follow-up",
            "body": "Private draft body.",
            "sent": False,
        },
    )

    drafts = tool.list_recent_drafts(max_results=2)

    assert [draft["draft_id"] for draft in drafts] == ["draft-1", "draft-2"]
    assert requests == [("GET", "drafts", {"params": {"maxResults": 2}})]


def test_gmail_update_draft_live_is_account_scoped_and_uses_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = GmailTool(live=True, access_token="fake-token")
    requests: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(tool, "current_account_email", lambda: "operator@example.com")

    def fake_request(
        method: str, path: str, *, operation: str, **kwargs: object
    ) -> dict[str, object]:
        requests.append((method, path, kwargs))
        return {"id": "draft-001", "message": {"id": "message-002"}}

    monkeypatch.setattr(tool, "_request", fake_request)
    result = tool.update_draft(
        "draft-001",
        "reviewer@example.com",
        "Updated subject",
        "Updated body for review only.",
        expected_account="operator@example.com",
    )

    assert result["status"] == "draft_updated"
    assert result["message_id"] == "message-002"
    assert result["sent"] is False
    assert requests[0][0:2] == ("PUT", "drafts/draft-001")
    assert "raw" in requests[0][2]["json"]["message"]  # type: ignore[index]


def test_gmail_delete_draft_dry_run_preserves_exact_id_and_no_send() -> None:
    result = GmailTool(live=False).delete_draft(
        "draft-test-001",
        expected_account="operator@example.com",
    )

    assert result == {
        "status": "dry-run",
        "draft_id": "draft-test-001",
        "gmail_account": "operator@example.com",
        "sent": False,
    }


def test_gmail_send_draft_dry_run_preserves_no_send_boundary() -> None:
    result = GmailTool(live=False).send_draft(
        "draft-test-001",
        expected_account="operator@example.com",
    )

    assert result == {
        "status": "dry-run",
        "draft_id": "draft-test-001",
        "message_id": "",
        "thread_id": "",
        "gmail_account": "operator@example.com",
        "sent": False,
    }


def test_gmail_send_draft_live_is_account_scoped_and_uses_exact_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = GmailTool(live=True, access_token="fake-token")
    requests: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(tool, "current_account_email", lambda: "operator@example.com")

    def fake_request(
        method: str, path: str, *, operation: str, **kwargs: object
    ) -> dict[str, object]:
        requests.append((method, path, kwargs))
        return {"id": "message-sent", "threadId": "thread-sent"}

    monkeypatch.setattr(tool, "_request", fake_request)
    result = tool.send_draft(
        "draft-test-001",
        expected_account="operator@example.com",
    )

    assert result["status"] == "sent"
    assert result["message_id"] == "message-sent"
    assert result["sent"] is True
    assert requests == [
        ("POST", "drafts/send", {"json": {"id": "draft-test-001"}})
    ]


def test_gmail_send_draft_live_rejects_wrong_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = GmailTool(live=True, access_token="fake-token")
    monkeypatch.setattr(tool, "current_account_email", lambda: "other@example.com")

    with pytest.raises(GmailConfigurationError, match="operator@example.com"):
        tool.send_draft(
            "draft-test-001",
            expected_account="operator@example.com",
        )


def test_gmail_delete_and_absence_check_use_exact_provider_draft_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = GmailTool(live=True, access_token="fake-token")
    requests: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(tool, "current_account_email", lambda: "operator@example.com")

    def fake_request(
        method: str, path: str, *, operation: str, **kwargs: object
    ) -> dict[str, object]:
        requests.append((method, path, kwargs))
        return {"_not_found": True} if method == "GET" else {}

    monkeypatch.setattr(tool, "_request", fake_request)

    deleted = tool.delete_draft(
        "draft-test-001",
        expected_account="operator@example.com",
    )
    exists = tool.draft_exists("draft-test-001")

    assert deleted["status"] == "draft_deleted"
    assert deleted["sent"] is False
    assert exists is False
    assert requests[0][0:2] == ("DELETE", "drafts/draft-test-001")
    assert requests[1][0:2] == ("GET", "drafts/draft-test-001")
    assert requests[1][2]["allow_not_found"] is True


def test_outreach_draft_blocks_unsafe_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEYSTONE_LIVE_MODE", raising=False)

    with pytest.raises(ToolGuardrailViolation, match="possible PHI"):
        create_gmail_draft_reply(
            message_id="dry-msg-001",
            body="We can advise on this Patient Alex depression diagnosis.",
        )


def test_live_mode_requires_explicit_integration_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SERPER_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="Live mode is disabled"):
        require_live_mode(Settings())

    with pytest.raises(SerperConfigurationError, match="SERPER_API_KEY is required"):
        SerperTool(live=True).search(query="neuroinformatics pilot", max_results=1)
