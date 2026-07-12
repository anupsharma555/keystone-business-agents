"""Natural-language Calendar action planning with operator defaults."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from zoneinfo import ZoneInfo

from keystone_agents.tools.google_calendar_tool import (
    DEFAULT_CALENDAR_ID,
    DEFAULT_CALENDAR_TIMEZONE,
    GOOGLE_CALENDAR_ID_ENV,
    GOOGLE_CALENDAR_TIMEZONE_ENV,
)

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


@dataclass(frozen=True)
class CalendarActionPlan:
    """Exact Calendar action inferred from one natural operator request."""

    operation: str
    title: str = ""
    start_date: str = ""
    start_time: str = ""
    end_time: str = ""
    description: str = ""
    event_id: str = ""
    event_reference: str = ""
    calendar_id: str = DEFAULT_CALENDAR_ID
    timezone: str = DEFAULT_CALENDAR_TIMEZONE
    all_day: bool = True
    complete: bool = False
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class EmailCalendarCandidate:
    """Bounded event fields extracted locally from one selected Gmail message."""

    title: str
    start_date: str
    start_time: str = ""
    end_time: str = ""
    all_day: bool = True
    source_subject_hash: str = ""
    attendee_hashes: tuple[str, ...] = ()
    complete: bool = False
    blockers: tuple[str, ...] = ()


def extract_email_calendar_candidate(
    *,
    subject: str,
    body: str,
    sender_email: str = "",
    recipient_text: str = "",
    today: date | None = None,
) -> EmailCalendarCandidate:
    """Extract obvious event fields locally and block ambiguous or past dates."""

    clean_subject = re.sub(r"^(?:(?:re|fwd?)\s*:\s*)+", "", subject.strip(), flags=re.I)
    title = clean_subject[:240]
    text = " ".join(part for part in (clean_subject, body) if str(part or "").strip())
    reference = today or current_calendar_date()
    dates = _event_dates(text, today=reference)
    start_time, end_time = _event_times(text)
    blockers: list[str] = []
    if not title:
        blockers.append("event title")
    if not dates:
        blockers.append("event date")
    elif len(dates) > 1:
        blockers.append("multiple event dates")
    elif date.fromisoformat(dates[0]) < reference:
        blockers.append("event date is in the past")
    attendee_emails = {
        value.lower()
        for value in re.findall(
            r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
            " ".join((sender_email, recipient_text)),
            re.I,
        )
    }
    return EmailCalendarCandidate(
        title=title,
        start_date=dates[0] if len(dates) == 1 else "",
        start_time=start_time,
        end_time=end_time,
        all_day=not bool(start_time),
        source_subject_hash=sha256(clean_subject.encode()).hexdigest()[:12] if title else "",
        attendee_hashes=tuple(
            sorted(sha256(email.encode()).hexdigest()[:12] for email in attendee_emails)
        ),
        complete=not blockers,
        blockers=tuple(blockers),
    )


def infer_calendar_action_plan(
    request_text: str,
    *,
    today: date | None = None,
) -> CalendarActionPlan | None:
    """Infer a direct Calendar CRUD action without asking for configured defaults."""

    text = " ".join(str(request_text or "").split()).strip()
    lower = text.lower()
    action_text = re.sub(
        r"\b(?:do\s+not|don't|dont|never|without)\b[^.;]*",
        " ",
        lower,
    )
    if not text or not re.search(r"\b(?:calendar|event|meeting)\b", action_text):
        return None
    operation = _operation(action_text)
    if not operation:
        return None
    calendar_id = os.getenv(GOOGLE_CALENDAR_ID_ENV, DEFAULT_CALENDAR_ID).strip()
    timezone = os.getenv(GOOGLE_CALENDAR_TIMEZONE_ENV, DEFAULT_CALENDAR_TIMEZONE).strip()
    event_id = _event_id(text)
    event_reference = _event_reference(text, operation=operation)
    title = _title(text, operation=operation)
    start_date = _event_date(text, today=today)
    start_time, end_time = _event_times(text)
    description = _description(text)
    blockers: list[str] = []
    if operation == "create":
        if not title:
            blockers.append("event title")
        if not start_date:
            blockers.append("event date")
    else:
        if not event_id and not event_reference:
            blockers.append("event name or exact event id")
        if operation == "update" and not any((title, start_date, start_time, description)):
            blockers.append("updated title, date, or note")
    return CalendarActionPlan(
        operation=operation,
        title=title,
        start_date=start_date,
        start_time=start_time,
        end_time=end_time,
        description=description,
        event_id=event_id,
        event_reference=event_reference,
        calendar_id=calendar_id or DEFAULT_CALENDAR_ID,
        timezone=timezone or DEFAULT_CALENDAR_TIMEZONE,
        all_day=bool(re.search(r"\ball[ -]?day\b", lower)) or not bool(start_time),
        complete=not blockers,
        blockers=tuple(blockers),
    )


def current_calendar_date(timezone: str = DEFAULT_CALENDAR_TIMEZONE) -> date:
    """Return today's date in the configured operator timezone."""

    from datetime import datetime

    return datetime.now(ZoneInfo(timezone)).date()


def _operation(lower: str) -> str:
    if re.search(r"\b(?:delete|remove|cancel)\b", lower):
        return "delete"
    if re.search(r"\b(?:update|modify|change|rename|move|edit|add\s+(?:a\s+)?note)\b", lower):
        return "update"
    if re.search(r"\b(?:add|create|schedule|put)\b", lower):
        return "create"
    return ""


def _event_date(text: str, *, today: date | None) -> str:
    dates = _event_dates(text, today=today)
    return dates[0] if dates else ""


def _event_dates(text: str, *, today: date | None) -> list[str]:
    found: list[str] = []
    for iso_value in re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", text):
        try:
            normalized = date.fromisoformat(iso_value).isoformat()
        except ValueError:
            continue
        if normalized not in found:
            found.append(normalized)
    month_names = "|".join(MONTHS)
    matches = re.finditer(
        rf"\b(?P<month>{month_names})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
        rf"(?:,?\s+(?P<year>20\d{{2}}))?\b",
        text,
        re.I,
    )
    reference = today or current_calendar_date()
    for match in matches:
        month = MONTHS[match.group("month").lower()]
        day = int(match.group("day"))
        year = int(match.group("year")) if match.group("year") else reference.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if not match.group("year") and candidate < reference:
            candidate = date(year + 1, month, day)
        normalized = candidate.isoformat()
        if normalized not in found:
            found.append(normalized)
    return found


def _event_times(text: str) -> tuple[str, str]:
    clock = (
        r"(?P<{name}_hour>\d{{1,2}})(?::(?P<{name}_minute>\d{{2}}))?\s*"
        r"(?P<{name}_ampm>a\.?m\.?|p\.?m\.?)?"
    )
    range_match = re.search(
        rf"\b(?:from|at)\s+{clock.format(name='start')}\s*(?:to|[-–])\s*"
        rf"{clock.format(name='end')}\b",
        text,
        re.I,
    )
    if range_match:
        end_ampm = range_match.group("end_ampm") or ""
        start_ampm = range_match.group("start_ampm") or end_ampm
        return (
            _normalize_clock(
                range_match.group("start_hour"),
                range_match.group("start_minute"),
                start_ampm,
            ),
            _normalize_clock(
                range_match.group("end_hour"),
                range_match.group("end_minute"),
                end_ampm,
            ),
        )
    single = re.search(
        rf"\b(?:at|from)\s+{clock.format(name='start')}\b",
        text,
        re.I,
    )
    if not single:
        return "", ""
    return (
        _normalize_clock(
            single.group("start_hour"),
            single.group("start_minute"),
            single.group("start_ampm") or "",
        ),
        "",
    )


def _normalize_clock(hour_text: str, minute_text: str | None, ampm_text: str) -> str:
    hour = int(hour_text)
    minute = int(minute_text or 0)
    marker = re.sub(r"[^apm]", "", ampm_text.lower())
    if minute > 59 or hour > (12 if marker else 23) or hour < 0:
        return ""
    if marker == "pm" and hour < 12:
        hour += 12
    elif marker == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _title(text: str, *, operation: str) -> str:
    explicit = re.search(
        r"\b(?:titled|called|named)\s+[\"“]?(?P<title>.+?)[\"”]?(?=\s+(?:with\s+note|note:|on\s+calendar\b)|$)",
        text,
        re.I,
    )
    if explicit:
        return _clean_title(explicit.group("title"))
    if operation == "create":
        trailing = re.search(
            r"\b(?:calendar\s+event|event|meeting)\s+(?:that|for)\s+(?P<title>.+)$",
            text,
            re.I,
        )
        if trailing:
            return _clean_title(trailing.group("title"))
    if operation == "update":
        rename = re.search(r"\b(?:rename|title)\b.+?\bto\s+[\"“]?(?P<title>.+)$", text, re.I)
        if rename:
            return _clean_title(rename.group("title"))
    return ""


def _clean_title(value: str) -> str:
    cleaned = re.split(r"\s+(?:with\s+note|note:)\s*", value, maxsplit=1, flags=re.I)[0]
    return cleaned.strip(" .,:;\"“”")[:240]


def _description(text: str) -> str:
    match = re.search(r"\b(?:with\s+(?:a\s+)?note|note:)\s*[\"“]?(?P<note>.+)$", text, re.I)
    if match:
        return match.group("note").strip(" .,:;\"“”")[:2000]
    change = re.search(
        r"\b(?:change|update|edit|add)\s+(?:the\s+|a\s+)?note\s+"
        r"(?:on|for)\s+(?:the\s+)?(?:.+?)\s+(?:calendar\s+)?event\s+"
        r"(?:to|as)\s+[\"“]?(?P<note>.+)$",
        text,
        re.I,
    )
    return change.group("note").strip(" .,:;\"“”")[:2000] if change else ""


def _event_reference(text: str, *, operation: str) -> str:
    if operation == "create":
        return ""
    note_change = re.search(
        r"\b(?:change|update|edit|add)\s+(?:the\s+|a\s+)?note\s+"
        r"(?:on|for)\s+(?:the\s+)?(?P<reference>.+?)\s+"
        r"(?:calendar\s+)?event\s+(?:to|as)\b",
        text,
        re.I,
    )
    if note_change:
        return _clean_event_reference(note_change.group("reference"))
    named = re.search(
        r"\b(?:calendar\s+)?event\s+(?:called|named|titled)\s+"
        r"[\"“]?(?P<reference>.+?)[\"”]?(?=\s+(?:to|with|on)\b|$)",
        text,
        re.I,
    )
    if named:
        return _clean_event_reference(named.group("reference"))
    action = re.search(
        r"\b(?:update|modify|change|edit|move|rename|delete|remove|cancel)\s+"
        r"(?:the\s+)?(?P<reference>.+?)\s+(?:calendar\s+)?event"
        r"(?=\s+(?:to|with|on)\b|$)",
        text,
        re.I,
    )
    return _clean_event_reference(action.group("reference")) if action else ""


def _clean_event_reference(value: str) -> str:
    cleaned = re.sub(r"^(?:the\s+)", "", str(value or "").strip(), flags=re.I)
    return cleaned.strip(" .,:;\"“”")[:240]


def _event_id(text: str) -> str:
    match = re.search(
        r"\b(?:event[_ -]?id|event)\s*[:#]?\s*(?P<id>kba[0-9a-f]{16,64})\b",
        text,
        re.I,
    )
    return match.group("id").lower() if match else ""
