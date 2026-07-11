from __future__ import annotations

from typing import Any

from scripts import run_google_calendar_test_event_lifecycle as lifecycle


class FakeCalendarTool:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}

    def create_event(
        self, calendar_id: str, event_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        event = {"id": event_id, "htmlLink": "https://calendar.test/event", **payload}
        self.events[event_id] = event
        return dict(event)

    def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any]:
        return dict(self.events.get(event_id, {"_not_found": True}))

    def update_event(
        self, calendar_id: str, event_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.events[event_id].update(payload)
        return dict(self.events[event_id])

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        del self.events[event_id]


def test_calendar_lifecycle_dry_run_has_no_side_effects() -> None:
    tool = FakeCalendarTool()

    result = lifecycle.execute_google_calendar_test_lifecycle(
        suffix="dryrun",
        calendar_id="primary",
        start_date="2026-11-04",
        updated_date="2026-11-05",
        approval_reference="anu-211-dryrun",
        live=False,
        tool=tool,  # type: ignore[arg-type]
    )

    assert result["status"] == "dry-run"
    assert result["openai_requests"] == 0
    assert tool.events == {}


def test_calendar_lifecycle_creates_updates_and_cleans_up(monkeypatch) -> None:
    monkeypatch.setenv("KEYSTONE_GOOGLE_CALENDAR_ALLOW_WRITES", "true")
    tool = FakeCalendarTool()

    result = lifecycle.execute_google_calendar_test_lifecycle(
        suffix="liveproof",
        calendar_id="primary",
        start_date="2026-11-04",
        updated_date="2026-11-05",
        approval_reference="anu-211-liveproof",
        live=True,
        tool=tool,  # type: ignore[arg-type]
    )

    assert result["status"] == "passed"
    assert result["openai_requests"] == 0
    assert result["receipts"]["create"]["verification"]["passed"] is True
    assert result["receipts"]["update"]["start_date"] == "2026-11-05"
    assert result["receipts"]["delete"]["verification"]["event_absent_after"] is True
    assert tool.events == {}
