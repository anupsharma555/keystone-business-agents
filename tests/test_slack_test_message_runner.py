from __future__ import annotations

import scripts.run_slack_test_message_lifecycle as lifecycle


class FakeSlack:
    def __init__(self) -> None:
        self.ts = "1780000000.000100"
        self.present = False
        self.text = ""

    def post_test_message(self, channel: str, text: str, *, approval_reference: str):
        self.present = True
        self.text = text
        return {
            "status": "posted",
            "operation": "post_test_message",
            "channel": channel,
            "ts": self.ts,
            "verification": {"passed": True},
        }

    def update_test_message(
        self,
        channel: str,
        ts: str,
        text: str,
        *,
        approval_reference: str,
    ):
        assert ts == self.ts
        self.text = text
        return {
            "status": "updated",
            "operation": "update_test_message",
            "channel": channel,
            "ts": ts,
            "verification": {"passed": True},
        }

    def delete_test_message(self, channel: str, ts: str, *, approval_reference: str):
        assert ts == self.ts
        self.present = False
        return {
            "status": "deleted",
            "operation": "delete_test_message",
            "channel": channel,
            "ts": ts,
            "verification": {"passed": True, "message_absent_after": True},
        }

    def find_test_messages(self, channel: str, exact_text: str, *, limit: int = 20):
        return [self.ts] if self.present and self.text == exact_text else []


def test_slack_test_message_runner_verifies_lifecycle_and_cleanup() -> None:
    slack = FakeSlack()

    result = lifecycle.execute_slack_test_message_lifecycle(
        channel="C_TEST",
        suffix="case",
        approval_reference="operator:test-message",
        live=True,
        slack=slack,  # type: ignore[arg-type]
    )

    assert result["status"] == "passed"
    assert result["openai_requests"] == 0
    assert result["message_present_after"] is False
    assert result["create"]["ts"] == result["update"]["ts"]
    assert result["delete"]["verification"]["message_absent_after"] is True
    assert slack.present is False


class TimeoutSlack(FakeSlack):
    def post_test_message(self, channel: str, text: str, *, approval_reference: str):
        self.present = True
        self.text = text
        raise TimeoutError("ambiguous provider response")


def test_slack_test_message_runner_recovers_ambiguous_create_and_deletes() -> None:
    slack = TimeoutSlack()

    result = lifecycle.execute_slack_test_message_lifecycle(
        channel="C_TEST",
        suffix="timeout",
        approval_reference="operator:test-message",
        live=True,
        slack=slack,  # type: ignore[arg-type]
    )

    assert result["status"] == "failed"
    assert result["recovery"]["recovered_message"] is True
    assert result["delete"]["verification"]["passed"] is True
    assert slack.present is False
