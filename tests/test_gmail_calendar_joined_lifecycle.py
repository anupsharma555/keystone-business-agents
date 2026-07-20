from __future__ import annotations

from datetime import date
from typing import Any

from keystone_agents.calendar_actions import extract_email_calendar_candidate
from scripts.run_gmail_calendar_joined_lifecycle import execute_joined_lifecycle


class FakeGmail:
    def search_message_summaries(self, **_: Any) -> list[dict[str, Any]]:
        return [{"message_id": "msg-1"}]

    def get_message(self, message_id: str) -> dict[str, Any]:
        assert message_id == "msg-1"
        return {
            "id": "msg-1",
            "threadId": "thread-1",
            "subject": "KBA validation review",
            "from": "alex@example.test",
            "to": "operator@example.test",
            "normalized_body": (
                "Please join the review on July 15, 2026 from 2 to 3:30 PM."
            ),
        }


class FakeCalendar:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}

    def create_event(
        self, calendar_id: str, event_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        event = {"id": event_id, **payload}
        self.events[event_id] = event
        return event

    def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any]:
        return dict(self.events.get(event_id, {"_not_found": True}))

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        self.events.pop(event_id, None)


def test_email_candidate_extracts_future_timed_event_locally() -> None:
    candidate = extract_email_calendar_candidate(
        subject="Re: KBA validation review",
        body="Please join on July 15, 2026 from 2 to 3:30 PM.",
        sender_email="alex@example.test",
        recipient_text="operator@example.test",
        today=date(2026, 7, 11),
    )

    assert candidate.complete is True
    assert candidate.title == "KBA validation review"
    assert candidate.start_date == "2026-07-15"
    assert candidate.start_time == "14:00"
    assert candidate.end_time == "15:30"
    assert candidate.all_day is False
    assert len(candidate.attendee_hashes) == 2


def test_email_candidate_blocks_multiple_dates() -> None:
    candidate = extract_email_calendar_candidate(
        subject="Choose a review date",
        body="Either July 15, 2026 or July 16, 2026 could work.",
        today=date(2026, 7, 11),
    )

    assert candidate.complete is False
    assert candidate.start_date == ""
    assert candidate.blockers == ("multiple event dates",)


def test_joined_gmail_calendar_lifecycle_verifies_and_cleans_up(monkeypatch) -> None:
    monkeypatch.setenv("KEYSTONE_GOOGLE_CALENDAR_ALLOW_WRITES", "true")
    calendar = FakeCalendar()

    result = execute_joined_lifecycle(
        gmail=FakeGmail(),  # type: ignore[arg-type]
        calendar=calendar,  # type: ignore[arg-type]
        query="bounded event query",
        max_messages=5,
        calendar_id="primary",
        today=date(2026, 7, 11),
    )

    assert result["status"] == "pass"
    assert result["openai_requests"] == 0
    assert result["gmail"]["raw_body_persisted"] is False
    assert result["extracted_event"]["start_time"] == "14:00"
    assert result["calendar"]["create_verified"] is True
    assert result["calendar"]["event_absent_after"] is True
    assert result["calendar"]["invitations_sent"] is False
    assert calendar.events == {}
