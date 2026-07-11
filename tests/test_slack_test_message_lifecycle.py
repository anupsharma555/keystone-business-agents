from __future__ import annotations

from typing import Any

import pytest

from keystone_agents.tools import slack_tool
from keystone_agents.tools.slack_tool import (
    SLACK_TEST_CHANNEL_ENV,
    SLACK_TEST_MESSAGE_MARKER,
    SLACK_TEST_WRITES_ENV,
    SlackTool,
)


def test_slack_marked_message_create_update_delete_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = "C_TEST"
    ts = "1780000000.000100"
    messages: dict[str, str] = {}
    monkeypatch.setenv(SLACK_TEST_WRITES_ENV, "true")
    monkeypatch.setenv(SLACK_TEST_CHANNEL_ENV, channel)

    def fake_post(self, *, channel, text, **kwargs):
        messages[ts] = text
        return {"status": "posted", "channel": channel, "ts": ts}

    def fake_api(
        method: str,
        endpoint: str,
        *,
        token: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if endpoint == "conversations.history":
            selected_ts = str((params or {}).get("oldest") or "")
            text = messages.get(selected_ts)
            return {
                "ok": True,
                "messages": [] if text is None else [{"ts": selected_ts, "text": text}],
            }
        if endpoint == "chat.update":
            messages[str((json_body or {})["ts"])] = str((json_body or {})["text"])
            return {"ok": True}
        if endpoint == "chat.delete":
            messages.pop(str((json_body or {})["ts"]), None)
            return {"ok": True}
        raise AssertionError(endpoint)

    monkeypatch.setattr(SlackTool, "_post_message_payload", fake_post)
    monkeypatch.setattr(slack_tool, "_slack_api_request", fake_api)
    tool = SlackTool(live=True, bot_token="token", approvals_channel=channel)
    initial = f"{SLACK_TEST_MESSAGE_MARKER} initial provider validation"
    revised = f"{SLACK_TEST_MESSAGE_MARKER} revised provider validation"

    created = tool.post_test_message(channel, initial, approval_reference="operator:create")
    updated = tool.update_test_message(
        channel,
        ts,
        revised,
        approval_reference="operator:update",
    )
    deleted = tool.delete_test_message(channel, ts, approval_reference="operator:delete")

    assert created["verification"]["passed"] is True
    assert updated["verification"] == {
        "status": "verified",
        "passed": True,
        "same_ts": True,
        "text_match": True,
    }
    assert deleted["verification"]["passed"] is True
    assert messages == {}


def test_slack_test_writes_require_marker_gate_channel_and_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = SlackTool(live=True, bot_token="token", approvals_channel="C_TEST")
    monkeypatch.setenv(SLACK_TEST_WRITES_ENV, "true")
    monkeypatch.setenv(SLACK_TEST_CHANNEL_ENV, "C_TEST")

    with pytest.raises(RuntimeError, match="test marker"):
        tool.post_test_message("C_TEST", "ordinary message", approval_reference="operator")
    with pytest.raises(RuntimeError, match="approval reference"):
        tool.post_test_message("C_TEST", SLACK_TEST_MESSAGE_MARKER, approval_reference="")
    with pytest.raises(RuntimeError, match="exact configured channel"):
        tool.post_test_message(
            "C_OTHER",
            SLACK_TEST_MESSAGE_MARKER,
            approval_reference="operator",
        )
    monkeypatch.setenv(SLACK_TEST_WRITES_ENV, "false")
    with pytest.raises(RuntimeError, match=SLACK_TEST_WRITES_ENV):
        tool.post_test_message(
            "C_TEST",
            SLACK_TEST_MESSAGE_MARKER,
            approval_reference="operator",
        )
