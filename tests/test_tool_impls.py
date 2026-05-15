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
        expected_account="wisegrow05@gmail.com",
    )

    assert draft["status"] == "dry-run"
    assert draft["gmail_account"] == "wisegrow05@gmail.com"
    assert draft["sent"] is False
    assert draft["approval_required"] is True


def test_gmail_create_draft_live_requires_expected_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = GmailTool(live=True, access_token="fake-token")
    monkeypatch.setattr(tool, "current_account_email", lambda: "other@example.com")

    with pytest.raises(GmailConfigurationError, match="wisegrow05@gmail.com"):
        tool.create_draft(
            to="andy@example.com",
            subject="Draft subject",
            body="Draft body for approval only.",
            expected_account="wisegrow05@gmail.com",
        )


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
