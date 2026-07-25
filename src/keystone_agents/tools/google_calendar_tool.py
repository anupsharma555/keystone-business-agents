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
GOOGLE_CALENDAR_LIST_READ_SCOPE = (
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
)


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

    def list_selected_readable_calendars(self) -> list[dict[str, Any]]:
        """Return calendars selected in the connected account's Calendar UI."""

        params = urlencode(
            {
                "minAccessRole": "reader",
                "showHidden": "false",
                "maxResults": 250,
            }
        )
        try:
            payload = self._request("GET", f"users/me/calendarList?{params}")
        except GoogleCalendarError as exc:
            if "insufficientPermissions" in str(exc):
                raise GoogleCalendarError(
                    "Reading selected and shared calendars requires the read-only "
                    f"OAuth scope {GOOGLE_CALENDAR_LIST_READ_SCOPE}. Reauthorize the "
                    "local Google token before retrying."
                ) from exc
            raise
        items = payload.get("items") or []
        return [
            item
            for item in items
            if isinstance(item, dict)
            and bool(item.get("selected") or item.get("primary"))
            and str(item.get("id") or "").strip()
        ]

    def list_events_window(
        self,
        calendar_id: str,
        *,
        time_min: str,
        time_max: str,
        max_results: int = 100,
        query: str = "",
    ) -> list[dict[str, Any]]:
        """List bounded event instances in chronological order."""

        values = {
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "showDeleted": "false",
            "orderBy": "startTime",
            "maxResults": min(max(int(max_results), 1), 100),
        }
        clean_query = " ".join(str(query or "").split()).strip()[:200]
        if clean_query:
            values["q"] = clean_query
        params = urlencode(values)
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
    start_date: str = "",
    calendar_id: str = "",
    live: bool = False,
    tool: GoogleCalendarTool | None = None,
) -> dict[str, Any]:
    """Resolve one active Calendar event from a natural title reference."""

    reference = _required(event_reference, "Calendar event name")
    requested_date = _iso_date(start_date) if start_date.strip() else ""
    clean_calendar = _calendar_id(calendar_id)
    if not live:
        return {
            "status": "dry-run",
            "operation": "resolve_calendar_event",
            "calendar_id": clean_calendar,
            "event_reference": reference,
            "start_date": requested_date,
            "match_count": 0,
            "send_enabled": False,
        }
    calendar = tool or GoogleCalendarTool(live=True)
    if requested_date:
        zone = ZoneInfo(
            os.getenv(GOOGLE_CALENDAR_TIMEZONE_ENV, DEFAULT_CALENDAR_TIMEZONE).strip()
            or DEFAULT_CALENDAR_TIMEZONE
        )
        window_start = datetime.combine(
            date.fromisoformat(requested_date),
            datetime.min.time(),
            tzinfo=zone,
        )
        candidates = calendar.list_events_window(
            clean_calendar,
            time_min=window_start.isoformat(),
            time_max=(window_start + timedelta(days=1)).isoformat(),
            max_results=100,
        )
    else:
        candidates = calendar.find_events(clean_calendar, reference, max_results=10)
    candidates = [
        item
        for item in candidates
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
            or _event_title_tokens_match(reference, item.get("summary"))
        )
    ]
    if requested_date:
        matches = [
            item
            for item in matches
            if _provider_event_start_date(item) == requested_date
        ]
    if len(matches) != 1:
        return {
            "status": "not_found" if not matches else "ambiguous",
            "operation": "resolve_calendar_event",
            "calendar_id": clean_calendar,
            "event_reference": reference,
            "start_date": requested_date,
            "match_count": len(matches),
            "send_enabled": False,
        }
    event = matches[0]
    bounded_event = _bounded_calendar_event(event)
    start = str(bounded_event.get("start") or "")
    end = str(bounded_event.get("end") or "")
    all_day = bool(start and "T" not in start)
    return {
        "status": "success",
        "operation": "resolve_calendar_event",
        "calendar_id": clean_calendar,
        "event_reference": reference,
        "match_count": 1,
        "event_id": str(event.get("id") or ""),
        "provider_link": str(event.get("htmlLink") or ""),
        "title": str(event.get("summary") or ""),
        "start_date": _provider_event_start_date(event),
        "start": start,
        "end": end,
        "start_time": "" if all_day else start[11:16],
        "end_time": "" if all_day else end[11:16],
        "all_day": all_day,
        "timezone": str((event.get("start") or {}).get("timeZone") or ""),
        "send_enabled": False,
    }


def _provider_event_start_date(event: dict[str, Any]) -> str:
    start = event.get("start") if isinstance(event.get("start"), dict) else {}
    return str(start.get("date") or start.get("dateTime") or "")[:10]


def read_google_calendar_window_impl(
    time_min: str,
    time_max: str,
    *,
    calendar_id: str = "",
    calendar_scope: str = "configured",
    query: str = "",
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
    clean_scope = str(calendar_scope or "configured").strip().lower()
    if clean_scope not in {"configured", "selected_readable"}:
        raise ValueError("Calendar read scope must be configured or selected_readable.")
    clean_query = " ".join(str(query or "").split()).strip()[:200]
    bounded_max = min(max(int(max_results), 1), 100)
    if not live:
        return {
            "status": "dry-run",
            "operation": "read_calendar_window",
            "calendar_id": clean_calendar,
            "calendar_scope": clean_scope,
            "query": clean_query,
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
    calendar_entries = (
        calendar.list_selected_readable_calendars()
        if clean_scope == "selected_readable"
        else [{"id": clean_calendar, "primary": clean_calendar == "primary"}]
    )
    if not calendar_entries:
        calendar_entries = [{"id": clean_calendar, "primary": True}]
    events: list[dict[str, Any]] = []
    for entry in calendar_entries:
        source_calendar_id = str(entry.get("id") or "").strip()
        if not source_calendar_id:
            continue
        raw_events = calendar.list_events_window(
            source_calendar_id,
            time_min=start,
            time_max=end,
            max_results=bounded_max,
            query=clean_query,
        )
        for raw_event in raw_events:
            event = _bounded_calendar_event(raw_event)
            event["source_calendar_id"] = source_calendar_id
            events.append(event)
    events = [event for event in events if event["status"] != "cancelled"]
    events = sorted(events, key=lambda event: str(event.get("start") or ""))[:bounded_max]
    return {
        "status": "success",
        "operation": "read_calendar_window",
        "calendar_id": clean_calendar,
        "calendar_scope": clean_scope,
        "calendar_count": len(calendar_entries),
        "query": clean_query,
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
    end_date: str = "",
    repeat_each_day: bool = False,
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
    recurrence = _daily_recurrence(clean_date, end_date) if repeat_each_day else []
    payload = _event_payload(
        clean_title,
        dates,
        description,
        clean_timezone,
        recurrence=recurrence,
    )
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
        recurrence=recurrence,
    )
    html_link = str(created.get("htmlLink") or verified.get("htmlLink") or "")
    return {
        "status": "success" if verification["passed"] else "verification_failed",
        "operation": "create_calendar_event",
        "calendar_id": clean_calendar,
        "event_id": event_id,
        "html_link": html_link,
        "provider_link": html_link,
        "title": clean_title,
        "start_date": clean_date,
        "end_date": _iso_date(end_date) if end_date.strip() else "",
        "start_time": start_time.strip(),
        "end_time": end_time.strip(),
        "all_day": not timed,
        "repeat_each_day": bool(recurrence),
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
    append_description: bool = False,
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
            _calendar_update_dates(
                clean_date,
                start_time=start_time,
                end_time=end_time,
                duration_minutes=duration_minutes,
                timezone=clean_timezone,
            )
        )
    if description:
        changes["description"] = description.strip()
    if not changes:
        raise ValueError("Calendar event update requires title, start_date, or description.")
    calendar = tool or GoogleCalendarTool(live=live)
    if not live:
        preview = _preview("update", clean_calendar, clean_event_id, changes, approval)
        preview["description_mode"] = "append" if append_description else "replace"
        return preview
    _require_live_write_gate()
    before = calendar.get_event(clean_calendar, clean_event_id)
    if before.get("_not_found"):
        raise GoogleCalendarError("Exact Calendar event was not found for update.")
    if append_description and description.strip():
        changes["description"] = _append_calendar_description(
            str(before.get("description") or ""),
            description.strip(),
        )
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
    html_link = str(verified.get("htmlLink") or "")
    return {
        "status": "success" if verification["passed"] else "verification_failed",
        "operation": "update_calendar_event",
        "calendar_id": clean_calendar,
        "event_id": clean_event_id,
        "html_link": html_link,
        "provider_link": html_link,
        "title": expected_title,
        "start_date": expected_start[:10],
        "start_time": "" if expected_all_day else expected_start[11:16],
        "all_day": expected_all_day,
        "timezone": clean_timezone,
        "description_present": bool(expected_description),
        "description_mode": "append" if append_description else "replace",
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
        "provider_link": str(before.get("htmlLink") or ""),
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
    end_date: str = "",
    repeat_each_day: bool = False,
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
            end_date=end_date,
            repeat_each_day=repeat_each_day,
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
    append_description: bool = False,
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
            append_description=append_description,
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


def _event_title_tokens_match(reference: object, candidate: object) -> bool:
    """Allow a bounded title match when the operator omits nonessential words."""

    def tokens(value: object) -> set[str]:
        normalized: set[str] = set()
        for token in _normalize_event_title(value).split():
            if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
                token = token[:-1]
            if token not in {"a", "an", "the", "event"}:
                normalized.add(token)
        return normalized

    reference_tokens = tokens(reference)
    candidate_tokens = tokens(candidate)
    return bool(
        len(reference_tokens) >= 2
        and reference_tokens.issubset(candidate_tokens)
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
    start_value = str(start.get("dateTime") or start.get("date") or "")
    end_value = str(end.get("dateTime") or end.get("date") or "")
    all_day = bool(start.get("date") and not start.get("dateTime"))
    recurring_event_id = str(event.get("recurringEventId") or "")
    return {
        "event_id": str(event.get("id") or ""),
        "title": " ".join(str(event.get("summary") or "(untitled event)").split())[:240],
        "status": str(event.get("status") or "confirmed").lower(),
        "start_date": start_value[:10],
        "start": start_value,
        "end": end_value,
        "start_time": "" if all_day else start_value[11:16],
        "end_time": "" if all_day else end_value[11:16],
        "all_day": all_day,
        "timezone": str(start.get("timeZone") or ""),
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


def _calendar_update_dates(
    start_date: str,
    *,
    start_time: str,
    end_time: str,
    duration_minutes: int,
    timezone: str,
) -> dict[str, dict[str, Any]]:
    """Build mutually exclusive PATCH fields for all-day/timed conversions."""

    if start_time.strip():
        dates: dict[str, dict[str, Any]] = _timed_dates(
            start_date,
            start_time,
            end_time=end_time,
            duration_minutes=duration_minutes,
            timezone=timezone,
        )
        for boundary in ("start", "end"):
            dates[boundary]["date"] = None
        return dates
    dates = _all_day_dates(start_date)
    for boundary in ("start", "end"):
        dates[boundary]["dateTime"] = None
        dates[boundary]["timeZone"] = None
    return dates


def _append_calendar_description(existing: str, addition: str) -> str:
    """Append one note without duplicating content already present."""

    clean_existing = existing.strip()
    clean_addition = addition.strip()
    if not clean_existing:
        return clean_addition
    if clean_addition in clean_existing:
        return clean_existing
    return f"{clean_existing}\n{clean_addition}"


def _event_payload(
    title: str,
    dates: dict[str, dict[str, str]],
    description: str,
    timezone: str,
    *,
    recurrence: list[str] | None = None,
) -> dict[str, Any]:
    payload = {
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
    if recurrence:
        payload["recurrence"] = recurrence
    return payload


def _daily_recurrence(start_date: str, end_date: str) -> list[str]:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(_iso_date(end_date))
    if end < start:
        raise ValueError("Calendar recurrence end_date must not precede start_date.")
    count = (end - start).days + 1
    if count > 366:
        raise ValueError("Calendar daily recurrence is limited to 366 instances.")
    return [f"RRULE:FREQ=DAILY;COUNT={count}"]


def _event_verification(
    event: dict[str, Any],
    *,
    event_id: str,
    title: str,
    expected_start: str,
    all_day: bool,
    description: str,
    recurrence: list[str] | None = None,
) -> dict[str, Any]:
    checks = {
        "event_id_match": str(event.get("id") or "") == event_id,
        "title_match": str(event.get("summary") or "") == title,
        "start_match": str(
            (event.get("start") or {}).get("date" if all_day else "dateTime") or ""
        )
        == expected_start,
        "description_match": str(event.get("description") or "") == description.strip(),
        "recurrence_match": list(event.get("recurrence") or []) == list(recurrence or []),
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
