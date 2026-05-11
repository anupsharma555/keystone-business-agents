from __future__ import annotations

import ast
from pathlib import Path

import pytest

from keystone_agents.agents.gmail_triage import run_gmail_triage_fixture
from keystone_agents.schemas.approval import ApprovalRequest, ApprovalScope, ApprovalState
from keystone_agents.tools.approval_tool import post_approval_request
from keystone_agents.tools.slack_tool import SlackConfigurationError, SlackTool

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def test_slack_dry_run_works_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_CHANNEL_APPROVALS", raising=False)
    triage = run_gmail_triage_fixture(
        FIXTURES / "sample_email_consulting.txt",
        sender_name="Alex",
        sender_email="alex@example.com",
    )

    request = post_approval_request(
        triage,
        context={"object_type": "gmail_draft", "summary": triage.summary},
        live=False,
    )

    assert isinstance(request, ApprovalRequest)
    assert request.decision == ApprovalState.PENDING
    assert request.scope == ApprovalScope.SEND
    assert request.slack_ts == "dry-run-slack-ts"
    assert request.draft_text == triage.draft_reply


def test_live_mode_without_credentials_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_CHANNEL_APPROVALS", raising=False)

    with pytest.raises(SlackConfigurationError, match="SLACK_BOT_TOKEN.*SLACK_CHANNEL_APPROVALS"):
        SlackTool(live=True).post_message(None, "approval requested")


def test_live_slack_success_uses_explicit_credentials_without_returning_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeSlackResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {"ok": True, "ts": "123.456"}

    def fake_post(url: str, **kwargs: object) -> FakeSlackResponse:
        calls.append({"url": url, **kwargs})
        return FakeSlackResponse()

    monkeypatch.setattr("requests.post", fake_post)

    result = SlackTool(
        live=True,
        bot_token="fake-slack-unit-test-token",
        approvals_channel="C123",
    ).post_message(None, "approval requested")

    assert result == {"status": "posted", "channel": "C123", "ts": "123.456"}
    assert calls[0]["json"] == {"channel": "C123", "text": "approval requested"}
    assert "fake-slack-unit-test-token" not in str(result)


def test_live_slack_http_and_api_errors_are_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    class HttpErrorResponse:
        status_code = 500

        def json(self) -> dict[str, object]:
            return {"ok": False}

    class ApiErrorResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {"ok": False, "error": "channel_not_found"}

    monkeypatch.setattr("requests.post", lambda *_args, **_kwargs: HttpErrorResponse())
    with pytest.raises(RuntimeError, match="HTTP 500"):
        SlackTool(live=True, bot_token="token", approvals_channel="C123").post_message(
            None,
            "approval requested",
        )

    monkeypatch.setattr("requests.post", lambda *_args, **_kwargs: ApiErrorResponse())
    with pytest.raises(RuntimeError, match="channel_not_found"):
        SlackTool(live=True, bot_token="token", approvals_channel="C123").post_message(
            None,
            "approval requested",
        )


def test_approval_request_contains_draft_and_risk_flags() -> None:
    request = post_approval_request(
        {
            "message_id": "dry-msg-1",
            "summary": "Review legal draft.",
            "draft_reply": "Hi,\n\nThanks for sending this. I received it and will review it.",
            "risk_flags": ["legal_review"],
        },
        context={"object_type": "gmail_draft"},
        live=False,
    )

    assert request.object_type == "gmail_draft"
    assert request.object_id == "dry-msg-1"
    assert "received it" in request.draft_text
    assert request.risk_flags == ["legal_review"]


def test_rejected_status_prevents_draft_creation() -> None:
    request = post_approval_request(
        {
            "message_id": "dry-msg-2",
            "summary": "Rejected draft.",
            "draft_reply": "This should not be posted.",
            "risk_flags": [],
        },
        context={"decision": "rejected"},
        live=False,
    )

    assert request.decision == ApprovalState.REJECTED
    assert request.draft_text == ""
    assert request.slack_ts is None


def test_expired_decision_prevents_slack_post() -> None:
    request = post_approval_request(
        {
            "message_id": "dry-msg-3",
            "summary": "Expired draft.",
            "draft_reply": "This should not be posted.",
        },
        context={"decision": "expired"},
        live=False,
    )

    assert request.decision == ApprovalState.EXPIRED
    assert request.draft_text == ""
    assert request.slack_ts is None


def test_no_email_send_path_exists() -> None:
    checked_paths = [
        PROJECT_ROOT / "src" / "keystone_agents" / "tools" / "approval_tool.py",
        PROJECT_ROOT / "src" / "keystone_agents" / "tools" / "slack_tool.py",
        PROJECT_ROOT / "scripts" / "run_gmail_triage.py",
        PROJECT_ROOT / "scripts" / "run_outreach_draft.py",
    ]

    for path in checked_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        assert not any(name.startswith("send") or "send_email" in name for name in names)
