"""Typed Google Calendar read/write boundary for exact approved event actions."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from keystone_agents.config import parse_bool
from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.sdk import function_tool
from keystone_agents.tools.gmail_tool import GmailTool

GOOGLE_CALENDAR_API_BASE_URL = "https://www.googleapis.com/calendar/v3"
GOOGLE_CALENDAR_WRITE_ENV = "KEYSTONE_GOOGLE_CALENDAR_ALLOW_WRITES"
GOOGLE_CALENDAR_ID_ENV = "GOOGLE_CALENDAR_ID"
GOOGLE_CALENDAR_TIMEZONE_ENV = "GOOGLE_CALENDAR_TIMEZONE"
DEFAULT_CALENDAR_ID = "primary"
DEFAULT_CALENDAR_TIMEZONE = "America/New_York"


class GoogleCalendarError(RuntimeError):
    """Raised when Google Calendar configuration or execution fails."""


@dataclass
class GoogleCalendarTool:
    """Small Calendar API wrapper using the configured Google OAuth token."""

    live: bool = False
    access_token: str = ""
    timeout_seconds: float = 10.0
    session: Any = field(default_factory=requests.Session)
    api_base_url: str = GOOGLE_CALENDAR_API_BASE_URL

    def _token(self) -> str:
        if self.access_token:
            return self.access_token
        if not self.live:
            return ""
        return GmailTool(live=True)._access_token()

    def _refresh_token(self) -> str:
        token = GmailTool(live=True)._force_refresh_access_token()
        self.access_token = token
        return token

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        allow_not_found: bool = False,
    ) -> dict[str, Any]:
        if not self.live:
            return {}
        url = f"{self.api_base_url.rstrip('/')}/{path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
        }
        response = None
        for attempt in range(2):
            response = self.session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                timeout=self.timeout_seconds,
            )
            if response.status_code != 401 or attempt == 1:
                break
            headers["Authorization"] = f"Bearer {self._refresh_token()}"
        assert response is not None
        if allow_not_found and response.status_code == 404:
            return {"_not_found": True}
        if response.status_code >= 400:
            detail = _google_api_error_detail(response)
            raise GoogleCalendarError(
                f"Google Calendar API {method} failed with HTTP {response.status_code}."
                + (f" {detail}" if detail else "")
            )
        if response.status_code == 204 or not response.content:
            return {}
        payload = response.json()
        if not isinstance(payload, dict):
            raise GoogleCalendarError("Google Calendar API returned a non-object response.")
        return payload

    def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}",
            allow_not_found=True,
        )

    def find_events(
        self,
        calendar_id: str,
        query: str,
        *,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        params = urlencode(
            {
                "q": query,
                "singleEvents": "true",
                "showDeleted": "false",
                "maxResults": min(max(int(max_results), 1), 25),
            }
        )
        payload = self._request(
            "GET",
            f"calendars/{quote(calendar_id, safe='')}/events?{params}",
        )
        items = payload.get("items") or []
        return [item for item in items if isinstance(item, dict)]

    def list_events_window(
        self,
        calendar_id: str,
        *,
        time_min: str,
        time_max: str,
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        """List bounded event instances in chronological order."""

        params = urlencode(
            {
                "timeMin": time_min,
                "timeMax": time_max,
                "singleEvents": "true",
                "showDeleted": "false",
                "orderBy": "startTime",
                "maxResults": min(max(int(max_results), 1), 100),
            }
        )
        payload = self._request(
            "GET",
            f"calendars/{quote(calendar_id, safe='')}/events?{params}",
        )
        items = payload.get("items") or []
        return [item for item in items if isinstance(item, dict)]

    def create_event(
        self,
        calendar_id: str,
        event_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"calendars/{quote(calendar_id, safe='')}/events",
            json_body={"id": event_id, **payload},
        )

    def update_event(
        self,
        calendar_id: str,
        event_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}",
            json_body=payload,
        )

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        self._request(
            "DELETE",
            f"calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}",
        )


def _google_api_error_detail(response: Any) -> str:
    """Return bounded provider reason/message without echoing request credentials."""

    try:
        payload = response.json()
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    status = str(error.get("status") or "").strip()
    message = " ".join(str(error.get("message") or "").split())[:240]
    reasons = []
    for item in error.get("errors") or []:
        if isinstance(item, dict) and item.get("reason"):
            reasons.append(str(item["reason"]).strip())
    parts = [part for part in (status, ",".join(dict.fromkeys(reasons)), message) if part]
    return "Provider detail: " + " | ".join(parts) if parts else ""


def resolve_google_calendar_event_impl(
    event_reference: str,
    *,
    calendar_id: str = "",
    live: bool = False,
    tool: GoogleCalendarTool | None = None,
) -> dict[str, Any]:
    """Resolve one active Calendar event from a natural title reference."""

    reference = _required(event_reference, "Calendar event name")
    clean_calendar = _calendar_id(calendar_id)
    if not live:
        return {
            "status": "dry-run",
            "operation": "resolve_calendar_event",
            "calendar_id": clean_calendar,
            "event_reference": reference,
            "match_count": 0,
            "send_enabled": False,
        }
    calendar = tool or GoogleCalendarTool(live=True)
    candidates = [
        item
        for item in calendar.find_events(clean_calendar, reference, max_results=10)
        if str(item.get("status") or "confirmed").lower() != "cancelled"
    ]
    normalized_reference = _normalize_event_title(reference)
    exact = [
        item
        for item in candidates
        if _normalize_event_title(item.get("summary")) == normalized_reference
    ]
    matches = exact or [
        item
        for item in candidates
        if normalized_reference
        and (
            normalized_reference in _normalize_event_title(item.get("summary"))
            or _normalize_event_title(item.get("summary")) in normalized_reference
        )
    ]
    if len(matches) != 1:
        return {
            "status": "not_found" if not matches else "ambiguous",
            "operation": "resolve_calendar_event",
            "calendar_id": clean_calendar,
            "event_reference": reference,
            "match_count": len(matches),
            "send_enabled": False,
        }
    event = matches[0]
    return {
        "status": "success",
        "operation": "resolve_calendar_event",
        "calendar_id": clean_calendar,
        "event_reference": reference,
        "match_count": 1,
        "event_id": str(event.get("id") or ""),
        "title": str(event.get("summary") or ""),
        "start_date": str((event.get("start") or {}).get("date") or ""),
        "send_enabled": False,
    }


def read_google_calendar_window_impl(
    time_min: str,
    time_max: str,
    *,
    calendar_id: str = "",
    max_results: int = 100,
    live: bool = False,
    tool: GoogleCalendarTool | None = None,
) -> dict[str, Any]:
    """Read a bounded Calendar window and split one-time from recurring events."""

    start = _rfc3339(time_min, "Calendar window start")
    end = _rfc3339(time_max, "Calendar window end")
    parsed_start = datetime.fromisoformat(start.replace("Z", "+00:00"))
    parsed_end = datetime.fromisoformat(end.replace("Z", "+00:00"))
    if parsed_end <= parsed_start:
        raise ValueError("Calendar window end must be after its start.")
    clean_calendar = _calendar_id(calendar_id)
    bounded_max = min(max(int(max_results), 1), 100)
    if not live:
        return {
            "status": "dry-run",
            "operation": "read_calendar_window",
            "calendar_id": clean_calendar,
            "time_min": start,
            "time_max": end,
            "max_results": bounded_max,
            "events": [],
            "non_recurring_count": 0,
            "recurring_count": 0,
            "external_writes_enabled": False,
            "send_enabled": False,
        }
    calendar = tool or GoogleCalendarTool(live=True)
    raw_events = calendar.list_events_window(
        clean_calendar,
        time_min=start,
        time_max=end,
        max_results=bounded_max,
    )
    events = [_bounded_calendar_event(event) for event in raw_events]
    events = [event for event in events if event["status"] != "cancelled"]
    return {
        "status": "success",
        "operation": "read_calendar_window",
        "calendar_id": clean_calendar,
        "time_min": start,
        "time_max": end,
        "max_results": bounded_max,
        "events": events,
        "non_recurring_count": sum(not event["is_recurring"] for event in events),
        "recurring_count": sum(event["is_recurring"] for event in events),
        "external_writes_enabled": False,
        "send_enabled": False,
    }


def create_google_calendar_event_impl(
    title: str,
    start_date: str,
    *,
    description: str = "",
    calendar_id: str = "",
    timezone: str = "",
    start_time: str = "",
    end_time: str = "",
    duration_minutes: int = 60,
    approval_reference: str = "",
    live: bool = False,
    tool: GoogleCalendarTool | None = None,
) -> dict[str, Any]:
    """Create and verify one exact all-day or timed event."""

    clean_title = _required(title, "Calendar event title")
    clean_date = _iso_date(start_date)
    clean_calendar = _calendar_id(calendar_id)
    clean_timezone = _timezone(timezone)
    approval = _approval(approval_reference)
    timed = bool(start_time.strip())
    dates = (
        _timed_dates(
            clean_date,
            start_time,
            end_time=end_time,
            duration_minutes=duration_minutes,
            timezone=clean_timezone,
        )
        if timed
        else _all_day_dates(clean_date)
    )
    expected_start = str(dates["start"].get("dateTime") or dates["start"].get("date") or "")
    event_id = _deterministic_event_id(clean_calendar, clean_title, expected_start, approval)
    payload = _event_payload(clean_title, dates, description, clean_timezone)
    calendar = tool or GoogleCalendarTool(live=live)
    if not live:
        return _preview("create", clean_calendar, event_id, payload, approval)
    _require_live_write_gate()
    created = calendar.create_event(clean_calendar, event_id, payload)
    verified = calendar.get_event(clean_calendar, event_id)
    verification = _event_verification(
        verified,
        event_id=event_id,
        title=clean_title,
        expected_start=expected_start,
        all_day=not timed,
        description=description,
    )
    return {
        "status": "success" if verification["passed"] else "verification_failed",
        "operation": "create_calendar_event",
        "calendar_id": clean_calendar,
        "event_id": event_id,
        "html_link": str(created.get("htmlLink") or verified.get("htmlLink") or ""),
        "title": clean_title,
        "start_date": clean_date,
        "start_time": start_time.strip(),
        "end_time": end_time.strip(),
        "all_day": not timed,
        "timezone": clean_timezone,
        "description_present": bool(description.strip()),
        "approval_reference": approval,
        "verification": verification,
        "send_enabled": False,
    }


def update_google_calendar_event_impl(
    event_id: str,
    *,
    title: str = "",
    start_date: str = "",
    description: str = "",
    calendar_id: str = "",
    timezone: str = "",
    start_time: str = "",
    end_time: str = "",
    duration_minutes: int = 60,
    approval_reference: str = "",
    live: bool = False,
    tool: GoogleCalendarTool | None = None,
) -> dict[str, Any]:
    """Update and verify one exact existing all-day or timed event."""

    clean_event_id = _required(event_id, "Calendar event id")
    clean_calendar = _calendar_id(calendar_id)
    clean_timezone = _timezone(timezone)
    approval = _approval(approval_reference)
    changes: dict[str, Any] = {}
    if title.strip():
        changes["summary"] = title.strip()
    if start_time.strip() and not start_date.strip():
        raise ValueError("Calendar timed update requires start_date with start_time.")
    if start_date.strip():
        clean_date = _iso_date(start_date)
        changes.update(
            _timed_dates(
                clean_date,
                start_time,
                end_time=end_time,
                duration_minutes=duration_minutes,
                timezone=clean_timezone,
            )
            if start_time.strip()
            else _all_day_dates(clean_date)
        )
    if description:
        changes["description"] = description.strip()
    if not changes:
        raise ValueError("Calendar event update requires title, start_date, or description.")
    calendar = tool or GoogleCalendarTool(live=live)
    if not live:
        return _preview("update", clean_calendar, clean_event_id, changes, approval)
    _require_live_write_gate()
    before = calendar.get_event(clean_calendar, clean_event_id)
    if before.get("_not_found"):
        raise GoogleCalendarError("Exact Calendar event was not found for update.")
    calendar.update_event(clean_calendar, clean_event_id, changes)
    verified = calendar.get_event(clean_calendar, clean_event_id)
    expected_title = str(changes.get("summary") or before.get("summary") or "")
    expected_start_payload = changes.get("start") or before.get("start") or {}
    expected_start = str(
        expected_start_payload.get("dateTime") or expected_start_payload.get("date") or ""
    )
    expected_all_day = bool(expected_start_payload.get("date"))
    expected_description = str(
        changes.get("description", before.get("description") or "")
    )
    verification = _event_verification(
        verified,
        event_id=clean_event_id,
        title=expected_title,
        expected_start=expected_start,
        all_day=expected_all_day,
        description=expected_description,
    )
    return {
        "status": "success" if verification["passed"] else "verification_failed",
        "operation": "update_calendar_event",
        "calendar_id": clean_calendar,
        "event_id": clean_event_id,
        "html_link": str(verified.get("htmlLink") or ""),
        "title": expected_title,
        "start_date": expected_start[:10],
        "start_time": "" if expected_all_day else expected_start[11:16],
        "all_day": expected_all_day,
        "timezone": clean_timezone,
        "description_present": bool(expected_description),
        "approval_reference": approval,
        "verification": verification,
        "send_enabled": False,
    }


def delete_google_calendar_event_impl(
    event_id: str,
    *,
    calendar_id: str = "",
    approval_reference: str = "",
    live: bool = False,
    tool: GoogleCalendarTool | None = None,
) -> dict[str, Any]:
    """Delete one exact event and verify provider absence."""

    clean_event_id = _required(event_id, "Calendar event id")
    clean_calendar = _calendar_id(calendar_id)
    approval = _approval(approval_reference)
    calendar = tool or GoogleCalendarTool(live=live)
    if not live:
        return _preview("delete", clean_calendar, clean_event_id, {}, approval)
    _require_live_write_gate()
    before = calendar.get_event(clean_calendar, clean_event_id)
    if before.get("_not_found"):
        raise GoogleCalendarError("Exact Calendar event was not found for deletion.")
    calendar.delete_event(clean_calendar, clean_event_id)
    after = calendar.get_event(clean_calendar, clean_event_id)
    cancelled = str(after.get("status") or "").strip().lower() == "cancelled"
    absent = bool(after.get("_not_found") or cancelled)
    return {
        "status": "success" if absent else "verification_failed",
        "operation": "delete_calendar_event",
        "calendar_id": clean_calendar,
        "event_id": clean_event_id,
        "title": str(before.get("summary") or ""),
        "start_date": str((before.get("start") or {}).get("date") or ""),
        "approval_reference": approval,
        "verification": {
            "status": "verified" if absent else "verification_failed",
            "passed": absent,
            "event_absent_after": absent,
            "event_cancelled_after": cancelled,
        },
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def read_google_calendar_window(
    time_min: str,
    time_max: str,
    calendar_id: str = "",
    max_results: int = 100,
    live: bool = False,
) -> str:
    """Read a bounded Calendar window and label recurring event instances."""

    return json.dumps(
        read_google_calendar_window_impl(
            time_min,
            time_max,
            calendar_id=calendar_id,
            max_results=max_results,
            live=live,
        ),
        sort_keys=True,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def create_google_calendar_event(
    title: str,
    start_date: str,
    description: str = "",
    calendar_id: str = "",
    timezone: str = "",
    start_time: str = "",
    end_time: str = "",
    duration_minutes: int = 60,
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Create and verify an approved all-day or timed Calendar event, including a note."""

    return json.dumps(
        create_google_calendar_event_impl(
            title,
            start_date,
            description=description,
            calendar_id=calendar_id,
            timezone=timezone,
            start_time=start_time,
            end_time=end_time,
            duration_minutes=duration_minutes,
            approval_reference=approval_reference,
            live=live,
        ),
        sort_keys=True,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def update_google_calendar_event(
    event_id: str,
    title: str = "",
    start_date: str = "",
    description: str = "",
    calendar_id: str = "",
    timezone: str = "",
    start_time: str = "",
    end_time: str = "",
    duration_minutes: int = 60,
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Modify and verify one exact Calendar event by provider event ID."""

    return json.dumps(
        update_google_calendar_event_impl(
            event_id,
            title=title,
            start_date=start_date,
            description=description,
            calendar_id=calendar_id,
            timezone=timezone,
            start_time=start_time,
            end_time=end_time,
            duration_minutes=duration_minutes,
            approval_reference=approval_reference,
            live=live,
        ),
        sort_keys=True,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def delete_google_calendar_event(
    event_id: str,
    calendar_id: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Delete and verify one exact approved Calendar event by provider event ID."""

    return json.dumps(
        delete_google_calendar_event_impl(
            event_id,
            calendar_id=calendar_id,
            approval_reference=approval_reference,
            live=live,
        ),
        sort_keys=True,
    )


def _required(value: str, label: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{label} is required.")
    return cleaned


def _normalize_event_title(value: object) -> str:
    return " ".join(
        "".join(character.lower() if character.isalnum() else " " for character in str(value or ""))
        .split()
    )


def _iso_date(value: str) -> str:
    cleaned = _required(value, "Calendar event date")
    try:
        return date.fromisoformat(cleaned).isoformat()
    except ValueError as exc:
        raise ValueError("Calendar event date must use YYYY-MM-DD.") from exc


def _rfc3339(value: str, label: str) -> str:
    cleaned = _required(value, label)
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must use RFC3339 with a timezone.") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone.")
    return cleaned


def _bounded_calendar_event(event: dict[str, Any]) -> dict[str, Any]:
    start = event.get("start") if isinstance(event.get("start"), dict) else {}
    end = event.get("end") if isinstance(event.get("end"), dict) else {}
    recurring_event_id = str(event.get("recurringEventId") or "")
    return {
        "event_id": str(event.get("id") or ""),
        "title": " ".join(str(event.get("summary") or "(untitled event)").split())[:240],
        "status": str(event.get("status") or "confirmed").lower(),
        "start": str(start.get("dateTime") or start.get("date") or ""),
        "end": str(end.get("dateTime") or end.get("date") or ""),
        "html_link": str(event.get("htmlLink") or ""),
        "is_recurring": bool(recurring_event_id),
        "recurring_event_id": recurring_event_id,
    }


def _calendar_id(value: str) -> str:
    return str(value or os.getenv(GOOGLE_CALENDAR_ID_ENV) or DEFAULT_CALENDAR_ID).strip()


def _timezone(value: str) -> str:
    return str(
        value or os.getenv(GOOGLE_CALENDAR_TIMEZONE_ENV) or DEFAULT_CALENDAR_TIMEZONE
    ).strip()


def _approval(value: str) -> str:
    cleaned = _required(value, "Calendar approval reference")
    return cleaned


def _require_live_write_gate() -> None:
    if not parse_bool(os.getenv(GOOGLE_CALENDAR_WRITE_ENV)):
        raise GoogleCalendarError(
            f"Google Calendar writes require {GOOGLE_CALENDAR_WRITE_ENV}=true."
        )


def _deterministic_event_id(
    calendar_id: str,
    title: str,
    start_date: str,
    approval_reference: str,
) -> str:
    digest = hashlib.sha256(
        f"{calendar_id}|{title}|{start_date}|{approval_reference}".encode()
    ).hexdigest()
    return f"kba{digest[:40]}"


def _all_day_dates(start_date: str) -> dict[str, dict[str, str]]:
    start = date.fromisoformat(start_date)
    return {
        "start": {"date": start.isoformat()},
        "end": {"date": (start + timedelta(days=1)).isoformat()},
    }


def _clock_time(value: str, label: str) -> tuple[int, int]:
    cleaned = _required(value, label)
    try:
        parsed = datetime.strptime(cleaned, "%H:%M")
    except ValueError as exc:
        raise ValueError(f"{label} must use HH:MM in 24-hour time.") from exc
    return parsed.hour, parsed.minute


def _timed_dates(
    start_date: str,
    start_time: str,
    *,
    end_time: str,
    duration_minutes: int,
    timezone: str,
) -> dict[str, dict[str, str]]:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown Calendar timezone: {timezone}.") from exc
    start_hour, start_minute = _clock_time(start_time, "Calendar start time")
    start = datetime.combine(
        date.fromisoformat(start_date),
        datetime.min.time(),
        tzinfo=zone,
    ).replace(hour=start_hour, minute=start_minute)
    if end_time.strip():
        end_hour, end_minute = _clock_time(end_time, "Calendar end time")
        end = start.replace(hour=end_hour, minute=end_minute)
        if end <= start:
            end += timedelta(days=1)
    else:
        if duration_minutes < 1 or duration_minutes > 1440:
            raise ValueError("Calendar duration_minutes must be from 1 to 1440.")
        end = start + timedelta(minutes=duration_minutes)
    return {
        "start": {"dateTime": start.isoformat(), "timeZone": timezone},
        "end": {"dateTime": end.isoformat(), "timeZone": timezone},
    }


def _event_payload(
    title: str,
    dates: dict[str, dict[str, str]],
    description: str,
    timezone: str,
) -> dict[str, Any]:
    return {
        "summary": title,
        "description": description.strip(),
        **dates,
        "extendedProperties": {
            "private": {
                "keystoneCreated": "true",
                "keystoneTimezone": timezone,
            }
        },
    }


def _event_verification(
    event: dict[str, Any],
    *,
    event_id: str,
    title: str,
    expected_start: str,
    all_day: bool,
    description: str,
) -> dict[str, Any]:
    checks = {
        "event_id_match": str(event.get("id") or "") == event_id,
        "title_match": str(event.get("summary") or "") == title,
        "start_match": str(
            (event.get("start") or {}).get("date" if all_day else "dateTime") or ""
        )
        == expected_start,
        "description_match": str(event.get("description") or "") == description.strip(),
    }
    return {
        "status": "verified" if all(checks.values()) else "verification_failed",
        "passed": all(checks.values()),
        **checks,
    }


def _preview(
    operation: str,
    calendar_id: str,
    event_id: str,
    payload: dict[str, Any],
    approval_reference: str,
) -> dict[str, Any]:
    return {
        "status": "dry-run",
        "operation": f"{operation}_calendar_event",
        "calendar_id": calendar_id,
        "event_id": event_id,
        "title": str(payload.get("summary") or ""),
        "start_date": str(
            (payload.get("start") or {}).get("date")
            or (payload.get("start") or {}).get("dateTime")
            or ""
        )[:10],
        "start_time": str((payload.get("start") or {}).get("dateTime") or "")[11:16],
        "all_day": bool((payload.get("start") or {}).get("date")),
        "description_present": bool(str(payload.get("description") or "")),
        "approval_reference": approval_reference,
        "verification": {"status": "preview", "passed": False},
        "send_enabled": False,
    }
