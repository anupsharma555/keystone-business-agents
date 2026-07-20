from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from keystone_agents.calendar_actions import infer_calendar_action_plan
from keystone_agents.tools.google_calendar_tool import (
    GOOGLE_CALENDAR_WRITE_ENV,
    GoogleCalendarError,
    GoogleCalendarTool,
    create_google_calendar_event_impl,
    delete_google_calendar_event_impl,
    read_google_calendar_window_impl,
    resolve_google_calendar_event_impl,
    update_google_calendar_event_impl,
)


class FakeCalendarTool:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.last_update_payload: dict[str, Any] = {}

    def create_event(
        self, calendar_id: str, event_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(("create", event_id))
        event = {"id": event_id, "htmlLink": "https://calendar.test/event", **payload}
        self.events[event_id] = event
        return dict(event)

    def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any]:
        self.calls.append(("get", event_id))
        return dict(self.events.get(event_id, {"_not_found": True}))

    def find_events(
        self, calendar_id: str, query: str, *, max_results: int = 10
    ) -> list[dict[str, Any]]:
        self.calls.append(("find", query))
        return [
            dict(event)
            for event in self.events.values()
            if query.lower() in str(event.get("summary") or "").lower()
        ][:max_results]

    def list_events_window(
        self,
        calendar_id: str,
        *,
        time_min: str,
        time_max: str,
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        self.calls.append(("list_window", f"{time_min}|{time_max}"))
        return list(self.events.values())[:max_results]

    def update_event(
        self, calendar_id: str, event_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(("update", event_id))
        self.last_update_payload = payload
        self.events[event_id].update(payload)
        return dict(self.events[event_id])

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        self.calls.append(("delete", event_id))
        del self.events[event_id]


class TombstoneCalendarTool(FakeCalendarTool):
    def delete_event(self, calendar_id: str, event_id: str) -> None:
        self.calls.append(("delete", event_id))
        self.events[event_id]["status"] = "cancelled"


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.content = b"{}" if status_code != 204 else b""

    def json(self) -> dict[str, Any]:
        return self._payload


class RetrySession:
    def __init__(self) -> None:
        self.responses = [FakeResponse(401), FakeResponse(200, {"id": "event-1"})]
        self.authorizations: list[str] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.authorizations.append(str(kwargs["headers"]["Authorization"]))
        return self.responses.pop(0)


def test_calendar_plan_infers_current_year_defaults_and_title() -> None:
    plan = infer_calendar_action_plan(
        "chief of staff on November 4th add an all day calendar event that "
        "Frontiers in Human Dynamics paper Due Date",
        today=date(2026, 7, 10),
    )

    assert plan is not None
    assert plan.operation == "create"
    assert plan.start_date == "2026-11-04"
    assert plan.title == "Frontiers in Human Dynamics paper Due Date"
    assert plan.calendar_id == "primary"
    assert plan.timezone == "America/New_York"
    assert plan.all_day is True
    assert plan.complete is True
    assert plan.blockers == ()


def test_calendar_plan_rolls_year_only_when_month_day_has_passed() -> None:
    plan = infer_calendar_action_plan(
        "add an all-day calendar event on January 5th titled Annual filing reminder",
        today=date(2026, 7, 10),
    )

    assert plan is not None
    assert plan.start_date == "2027-01-05"


def test_calendar_plan_extracts_timed_meeting_without_defaulting_all_day() -> None:
    plan = infer_calendar_action_plan(
        "schedule a calendar meeting on July 15, 2026 from 2 to 3:30 PM titled KBA review",
        today=date(2026, 7, 11),
    )

    assert plan is not None
    assert plan.start_date == "2026-07-15"
    assert plan.start_time == "14:00"
    assert plan.end_time == "15:30"
    assert plan.all_day is False
    assert plan.complete is True


def test_calendar_plan_extracts_labeled_daily_date_range() -> None:
    plan = infer_calendar_action_plan(
        "add this to the calendar. Event: Condo Walkthrough "
        "Dates: Monday, August 3 through Friday, August 7 "
        "Time: 11:00 AM to 4:00 PM each day Note: Inspect balconies",
        today=date(2026, 7, 16),
    )

    assert plan is not None
    assert plan.title == "Condo Walkthrough"
    assert plan.start_date == "2026-08-03"
    assert plan.end_date == "2026-08-07"
    assert plan.start_time == "11:00"
    assert plan.end_time == "16:00"
    assert plan.repeat_each_day is True
    assert plan.complete is True


def test_calendar_plan_ignores_update_words_inside_markdown_note() -> None:
    request = (
        "can u add this to the calendar for the dates and times specified here: "
        "Event: Condo Walkthrough "
        "**Dates: Monday, August 3 through Friday, August 7** "
        "*Time: 11:00 AM to 4:00 PM each day* "
        "Note: Please move balcony items prior to arrival and identify areas "
        "requiring attention."
    )

    plan = infer_calendar_action_plan(request, today=date(2026, 7, 16))

    assert plan is not None
    assert plan.operation == "create"
    assert plan.title == "Condo Walkthrough"
    assert plan.start_date == "2026-08-03"
    assert plan.end_date == "2026-08-07"
    assert plan.start_time == "11:00"
    assert plan.end_time == "16:00"
    assert plan.repeat_each_day is True
    assert plan.complete is True


def test_calendar_create_verifies_daily_recurrence(monkeypatch) -> None:
    monkeypatch.setenv(GOOGLE_CALENDAR_WRITE_ENV, "true")
    tool = FakeCalendarTool()

    result = create_google_calendar_event_impl(
        "Condo Walkthrough",
        "2026-08-03",
        end_date="2026-08-07",
        repeat_each_day=True,
        start_time="11:00",
        end_time="16:00",
        approval_reference="approved-calendar-range",
        live=True,
        tool=tool,  # type: ignore[arg-type]
    )

    assert result["status"] == "success"
    assert result["repeat_each_day"] is True
    assert result["end_date"] == "2026-08-07"
    assert result["verification"]["recurrence_match"] is True
    created = next(iter(tool.events.values()))
    assert created["recurrence"] == ["RRULE:FREQ=DAILY;COUNT=5"]


def test_calendar_plan_bounds_title_before_schedule_and_plural_notes() -> None:
    plan = infer_calendar_action_plan(
        "create a calendar event titled KBA_TEST_CAL_ANU120_R4 on July 15, 2026 "
        "from 4:10pm to 4:25pm ET with notes ANU-120 agent-specific write validation",
        today=date(2026, 7, 13),
    )

    assert plan is not None
    assert plan.title == "KBA_TEST_CAL_ANU120_R4"
    assert plan.start_date == "2026-07-15"
    assert plan.start_time == "16:10"
    assert plan.end_time == "16:25"
    assert plan.description == "ANU-120 agent-specific write validation"
    assert plan.complete is True


def test_calendar_plan_keeps_quoted_note_separate_from_followup_instruction() -> None:
    plan = infer_calendar_action_plan(
        'create a calendar event titled "KBA_TEST_CAL_ANU120_R4" on July 15, 2026 '
        'from 4:10pm to 4:25pm ET with notes "ANU-120 agent-specific write validation". '
        "Verify the provider event.",
        today=date(2026, 7, 13),
    )

    assert plan is not None
    assert plan.title == "KBA_TEST_CAL_ANU120_R4"
    assert plan.description == "ANU-120 agent-specific write validation"
    assert plan.complete is True


def test_calendar_plan_ignores_informational_event_and_negated_schedule() -> None:
    plan = infer_calendar_action_plan(
        "Research a company product, dataset, partnership, or funding event. "
        "Do not draft outreach, post elsewhere, schedule, share, save, or write externally."
    )

    assert plan is None


def test_calendar_plan_captures_note_and_exact_update_id() -> None:
    plan = infer_calendar_action_plan(
        "add a note to calendar event kba0123456789abcdef that note: submit final paper"
    )

    assert plan is not None
    assert plan.operation == "update"
    assert plan.event_id == "kba0123456789abcdef"
    assert plan.description == "submit final paper"
    assert plan.complete is True


def test_calendar_plan_resolves_natural_event_reference_without_id() -> None:
    plan = infer_calendar_action_plan(
        "change the note on the Frontiers in Human Dynamics paper Due Date event "
        "to submit the final paper"
    )

    assert plan is not None
    assert plan.operation == "update"
    assert plan.event_id == ""
    assert plan.event_reference == "Frontiers in Human Dynamics paper Due Date"
    assert plan.description == "submit the final paper"
    assert plan.complete is True
    assert plan.blockers == ()


def test_calendar_event_reference_resolves_only_one_active_match() -> None:
    tool = FakeCalendarTool()
    tool.events = {
        "event-1": {
            "id": "event-1",
            "status": "confirmed",
            "summary": "Frontiers in Human Dynamics paper Due Date",
            "start": {"date": "2026-11-04"},
        }
    }

    result = resolve_google_calendar_event_impl(
        "Frontiers in Human Dynamics paper Due Date",
        live=True,
        tool=tool,  # type: ignore[arg-type]
    )

    assert result["status"] == "success"
    assert result["match_count"] == 1
    assert result["event_id"] == "event-1"
    assert result["start_date"] == "2026-11-04"


def test_calendar_event_reference_blocks_ambiguous_matches() -> None:
    tool = FakeCalendarTool()
    tool.events = {
        "event-1": {
            "id": "event-1",
            "status": "confirmed",
            "summary": "Frontiers paper due date",
            "start": {"date": "2026-11-04"},
        },
        "event-2": {
            "id": "event-2",
            "status": "confirmed",
            "summary": "Frontiers paper due date",
            "start": {"date": "2027-11-04"},
        },
    }

    result = resolve_google_calendar_event_impl(
        "Frontiers paper due date",
        live=True,
        tool=tool,  # type: ignore[arg-type]
    )

    assert result["status"] == "ambiguous"
    assert result["match_count"] == 2
    assert "event_id" not in result


def test_calendar_event_reference_uses_operator_date_to_disambiguate() -> None:
    tool = FakeCalendarTool()
    tool.events = {
        "event-1": {
            "id": "event-1",
            "status": "confirmed",
            "summary": "Livestream",
            "start": {"dateTime": "2026-07-14T14:00:00-04:00"},
        },
        "event-2": {
            "id": "event-2",
            "status": "confirmed",
            "summary": "Livestream",
            "start": {"dateTime": "2026-07-21T14:00:00-04:00"},
        },
    }

    result = resolve_google_calendar_event_impl(
        "Livestream",
        start_date="2026-07-14",
        live=True,
        tool=tool,  # type: ignore[arg-type]
    )

    assert result["status"] == "success"
    assert result["event_id"] == "event-1"
    assert result["start_date"] == "2026-07-14"


def test_calendar_window_read_splits_one_time_and_recurring_without_private_bodies() -> None:
    tool = FakeCalendarTool()
    tool.events = {
        "one-time": {
            "id": "one-time",
            "status": "confirmed",
            "summary": "Client review",
            "description": "private notes must not leave the provider boundary",
            "attendees": [{"email": "private@example.com"}],
            "start": {"dateTime": "2026-07-06T10:00:00-04:00"},
            "end": {"dateTime": "2026-07-06T11:00:00-04:00"},
            "htmlLink": "https://calendar.test/one-time",
        },
        "recurring-instance": {
            "id": "recurring-instance",
            "recurringEventId": "recurring-series",
            "status": "confirmed",
            "summary": "Weekly operations sync",
            "start": {"dateTime": "2026-07-08T09:00:00-04:00"},
            "end": {"dateTime": "2026-07-08T09:30:00-04:00"},
        },
        "cancelled": {
            "id": "cancelled",
            "status": "cancelled",
            "summary": "Cancelled meeting",
            "start": {"date": "2026-07-09"},
            "end": {"date": "2026-07-10"},
        },
    }

    result = read_google_calendar_window_impl(
        "2026-07-04T00:00:00-04:00",
        "2026-07-11T00:00:00-04:00",
        live=True,
        tool=tool,  # type: ignore[arg-type]
    )

    assert result["status"] == "success"
    assert result["non_recurring_count"] == 1
    assert result["recurring_count"] == 1
    assert [event["event_id"] for event in result["events"]] == [
        "one-time",
        "recurring-instance",
    ]
    assert result["events"][1]["is_recurring"] is True
    assert "description" not in result["events"][0]
    assert "attendees" not in result["events"][0]
    assert result["external_writes_enabled"] is False
    assert result["send_enabled"] is False


def test_calendar_window_read_requires_ordered_timezone_bounds() -> None:
    with pytest.raises(ValueError, match="must include a timezone"):
        read_google_calendar_window_impl(
            "2026-07-04T00:00:00",
            "2026-07-11T00:00:00-04:00",
        )

    with pytest.raises(ValueError, match="end must be after"):
        read_google_calendar_window_impl(
            "2026-07-11T00:00:00-04:00",
            "2026-07-04T00:00:00-04:00",
        )


def test_calendar_create_update_delete_lifecycle_verifies_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GOOGLE_CALENDAR_WRITE_ENV, "true")
    tool = FakeCalendarTool()
    created = create_google_calendar_event_impl(
        "Frontiers paper due date",
        "2026-11-04",
        description="Submit final paper.",
        approval_reference="calendar-test:create",
        live=True,
        tool=tool,  # type: ignore[arg-type]
    )
    event_id = created["event_id"]

    assert created["status"] == "success"
    assert created["verification"]["passed"] is True
    assert created["description_present"] is True
    assert created["provider_link"] == "https://calendar.test/event"

    updated = update_google_calendar_event_impl(
        event_id,
        title="Frontiers paper final due date",
        start_date="2026-11-05",
        description="Submit revised final paper.",
        approval_reference="calendar-test:update",
        live=True,
        tool=tool,  # type: ignore[arg-type]
    )
    assert updated["status"] == "success"
    assert updated["start_date"] == "2026-11-05"
    assert updated["verification"]["passed"] is True
    assert updated["provider_link"] == "https://calendar.test/event"

    deleted = delete_google_calendar_event_impl(
        event_id,
        approval_reference="calendar-test:delete",
        live=True,
        tool=tool,  # type: ignore[arg-type]
    )
    assert deleted["status"] == "success"
    assert deleted["provider_link"] == "https://calendar.test/event"
    assert deleted["verification"]["event_absent_after"] is True


def test_calendar_timed_create_update_delete_preserves_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GOOGLE_CALENDAR_WRITE_ENV, "true")
    tool = FakeCalendarTool()
    created = create_google_calendar_event_impl(
        "KBA timed meeting",
        "2026-07-15",
        start_time="14:30",
        duration_minutes=45,
        timezone="America/New_York",
        description="Bounded timed-event proof.",
        approval_reference="calendar-timed:create",
        live=True,
        tool=tool,  # type: ignore[arg-type]
    )
    event_id = created["event_id"]

    assert created["status"] == "success"
    assert created["all_day"] is False
    assert created["start_time"] == "14:30"
    assert tool.events[event_id]["start"] == {
        "dateTime": "2026-07-15T14:30:00-04:00",
        "timeZone": "America/New_York",
    }
    assert tool.events[event_id]["end"] == {
        "dateTime": "2026-07-15T15:15:00-04:00",
        "timeZone": "America/New_York",
    }

    updated = update_google_calendar_event_impl(
        event_id,
        start_date="2026-07-16",
        start_time="09:00",
        end_time="10:30",
        timezone="America/New_York",
        approval_reference="calendar-timed:update",
        live=True,
        tool=tool,  # type: ignore[arg-type]
    )
    assert updated["status"] == "success"
    assert updated["all_day"] is False
    assert updated["start_date"] == "2026-07-16"
    assert updated["start_time"] == "09:00"

    deleted = delete_google_calendar_event_impl(
        event_id,
        approval_reference="calendar-timed:delete",
        live=True,
        tool=tool,  # type: ignore[arg-type]
    )
    assert deleted["verification"]["event_absent_after"] is True


def test_calendar_all_day_to_timed_update_clears_date_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GOOGLE_CALENDAR_WRITE_ENV, "true")
    tool = FakeCalendarTool()
    created = create_google_calendar_event_impl(
        "Partner livestream",
        "2026-07-14",
        approval_reference="calendar-conversion:create",
        live=True,
        tool=tool,  # type: ignore[arg-type]
    )

    updated = update_google_calendar_event_impl(
        created["event_id"],
        start_date="2026-07-14",
        start_time="14:00",
        end_time="15:00",
        timezone="America/New_York",
        approval_reference="calendar-conversion:update",
        live=True,
        tool=tool,  # type: ignore[arg-type]
    )

    assert updated["status"] == "success"
    assert updated["all_day"] is False
    assert tool.last_update_payload["start"] == {
        "dateTime": "2026-07-14T14:00:00-04:00",
        "timeZone": "America/New_York",
        "date": None,
    }
    assert tool.last_update_payload["end"] == {
        "dateTime": "2026-07-14T15:00:00-04:00",
        "timeZone": "America/New_York",
        "date": None,
    }


def test_calendar_note_append_preserves_existing_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GOOGLE_CALENDAR_WRITE_ENV, "true")
    tool = FakeCalendarTool()
    created = create_google_calendar_event_impl(
        "Partner livestream",
        "2026-07-14",
        description="Existing event context.",
        approval_reference="calendar-note:create",
        live=True,
        tool=tool,  # type: ignore[arg-type]
    )
    url = "https://example.test/thread/123"

    updated = update_google_calendar_event_impl(
        created["event_id"],
        description=url,
        append_description=True,
        approval_reference="calendar-note:append",
        live=True,
        tool=tool,  # type: ignore[arg-type]
    )

    assert updated["status"] == "success"
    assert updated["description_mode"] == "append"
    assert tool.events[created["event_id"]]["description"] == (
        f"Existing event context.\n{url}"
    )


def test_calendar_timed_event_requires_normalized_start_time() -> None:
    with pytest.raises(ValueError, match="HH:MM"):
        create_google_calendar_event_impl(
            "Bad timed meeting",
            "2026-07-15",
            start_time="2:30 PM",
            approval_reference="calendar-timed:create",
        )


def test_calendar_live_write_requires_separate_gate() -> None:
    with pytest.raises(GoogleCalendarError, match=GOOGLE_CALENDAR_WRITE_ENV):
        create_google_calendar_event_impl(
            "Blocked event",
            "2026-11-04",
            approval_reference="calendar-test:create",
            live=True,
            tool=FakeCalendarTool(),  # type: ignore[arg-type]
        )


def test_calendar_delete_accepts_google_cancelled_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GOOGLE_CALENDAR_WRITE_ENV, "true")
    tool = TombstoneCalendarTool()
    created = create_google_calendar_event_impl(
        "Calendar tombstone proof",
        "2026-11-04",
        approval_reference="calendar-test:create",
        live=True,
        tool=tool,  # type: ignore[arg-type]
    )

    deleted = delete_google_calendar_event_impl(
        created["event_id"],
        approval_reference="calendar-test:delete",
        live=True,
        tool=tool,  # type: ignore[arg-type]
    )

    assert deleted["status"] == "success"
    assert deleted["verification"] == {
        "status": "verified",
        "passed": True,
        "event_absent_after": True,
        "event_cancelled_after": True,
    }


def test_calendar_request_refreshes_once_after_401(monkeypatch: pytest.MonkeyPatch) -> None:
    session = RetrySession()
    tool = GoogleCalendarTool(
        live=True,
        access_token="expired-token",
        session=session,
    )
    monkeypatch.setattr(tool, "_refresh_token", lambda: "fresh-token")

    result = tool.get_event("primary", "event-1")

    assert result == {"id": "event-1"}
    assert session.authorizations == ["Bearer expired-token", "Bearer fresh-token"]


def test_calendar_error_reports_bounded_provider_reason() -> None:
    session = RetrySession()
    session.responses = [
        FakeResponse(
            403,
            {
                "error": {
                    "status": "PERMISSION_DENIED",
                    "message": "Calendar API has not been used in project 123 before.",
                    "errors": [{"reason": "accessNotConfigured"}],
                }
            },
        )
    ]
    tool = GoogleCalendarTool(live=True, access_token="token", session=session)

    with pytest.raises(
        GoogleCalendarError,
        match="PERMISSION_DENIED.*accessNotConfigured.*Calendar API",
    ):
        tool.get_event("primary", "event-1")
