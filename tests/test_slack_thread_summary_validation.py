from __future__ import annotations

from scripts.run_slack_thread_summary_validation import run_validation


class FakeSlack:
    def resolve_latest_thread_root(self, channel: str, *, scan_limit: int):
        assert channel == "C123"
        assert scan_limit == 50
        return {"thread_ts": "1.0", "reply_count": 2}

    def read_thread(self, channel: str, thread_ts: str, *, limit: int):
        assert (channel, thread_ts, limit) == ("C123", "1.0", 30)
        return {
            "status": "success",
            "messages": [
                {"ts": "1.0", "text": "We should keep the write path immediate."},
                {
                    "ts": "1.1",
                    "text": (
                        "*Answer:* The request is ready. "
                        "*Recommended actions:* - Update the event note and verify it."
                    ),
                    "bot_id": "B1",
                },
                {"ts": "1.2", "text": "Approved; no extra approval loop is needed."},
            ],
        }


def test_latest_thread_summary_returns_decisions_actions_and_no_post() -> None:
    payload = run_validation(
        channel="C123",
        thread_ts="",
        max_messages=30,
        slack=FakeSlack(),
    )

    assert payload["status"] == "pass"
    assert payload["message_count"] == 3
    assert payload["summary"]
    assert payload["decisions"]
    assert payload["action_items"]
    assert payload["openai_requests"] == 0
    assert payload["slack_posts"] == 0
    assert payload["provider_mutations"] == 0
    assert payload["post_enabled"] is False
    assert "channel" not in payload["thread_ref"]
    assert "thread_ts" not in payload["thread_ref"]


def test_latest_thread_summary_ignores_roots_without_replies() -> None:
    class NoReplySlack(FakeSlack):
        def resolve_latest_thread_root(self, channel: str, *, scan_limit: int):
            return {}

    payload = run_validation(
        channel="C123",
        thread_ts="",
        max_messages=30,
        slack=NoReplySlack(),
    )

    assert payload == {
        "status": "blocked",
        "reason": "no_thread_root",
        "openai_requests": 0,
    }


def test_latest_thread_summary_redacts_provider_id_and_avoids_receipt_hyphens() -> None:
    class CalendarSlack(FakeSlack):
        def read_thread(self, channel: str, thread_ts: str, *, limit: int):
            return {
                "status": "success",
                "messages": [
                    {
                        "text": (
                            "<@U1> chief of staff change the event note "
                            "*Sent using* <@U2>"
                        )
                    },
                    {
                        "bot_id": "B1",
                        "text": (
                            "*Calendar event updated:* Paper Due Date - Date: 2026-11-04 "
                            "- Event ID: kba-secret-provider-id - Provider verification: "
                            "passed - To change or delete it later, name the event and "
                            "include the date only when needed to disambiguate."
                        ),
                    },
                ],
            }

    payload = run_validation(
        channel="C123",
        thread_ts="1.0",
        max_messages=30,
        slack=CalendarSlack(),
    )

    rendered = str(payload)
    assert payload["status"] == "pass"
    assert "kba-secret-provider-id" not in rendered
    assert "Sent using" not in rendered
    assert payload["action_items"] == [
        "To change or delete it later, name the event and include the date only "
        "when needed to disambiguate"
    ]
