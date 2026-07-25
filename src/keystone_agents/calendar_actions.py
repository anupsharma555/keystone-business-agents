"""Natural-language Calendar action planning with operator defaults."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
    read_scope: str = "single_event"
    read_selection: str = "all"
    date_scope: str = "unspecified"
    query: str = ""
    title: str = ""
    start_date: str = ""
    end_date: str = ""
    start_time: str = ""
    end_time: str = ""
    repeat_each_day: bool = False
    description: str = ""
    append_description: bool = False
    event_id: str = ""
    event_reference: str = ""
    event_reference_date: str = ""
    calendar_id: str = DEFAULT_CALENDAR_ID
    calendar_scope: str = "configured"
    timezone: str = DEFAULT_CALENDAR_TIMEZONE
    all_day: bool = True
    complete: bool = False
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class CalendarInterpretationContext:
    """Separate action language from immutable event-description content."""

    directive_text: str
    thread_context: str = ""
    description_payload: str = ""
    event_fact_text: str = ""
    description_payload_kind: str = ""
    description_payload_sha256: str = ""


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
    previous_request, latest_followup = _slack_thread_action_context(text)
    action_source = _strip_slack_emphasis(latest_followup or text)
    directive_source = re.split(
        r"\s+notes?\s*:\s*",
        action_source,
        maxsplit=1,
        flags=re.I,
    )[0]
    lower = directive_source.lower()
    action_text = re.sub(
        r"\b(?:do\s+not|don't|dont|never|without)\b[^.;]*",
        " ",
        lower,
    )
    if not text or not is_calendar_action_candidate(text):
        return None
    if latest_followup and not (
        _explicit_calendar_mutation_requested(latest_followup)
        or _looks_like_implicit_calendar_deadline(latest_followup)
        or _looks_like_implicit_calendar_change(latest_followup)
    ):
        # The prior thread establishes Calendar identity, but the deterministic
        # parser must not reinterpret a generic continuation such as “add this
        # link to the notes” as a new create. The bounded Calendar interpreter
        # resolves that follow-up with the prior event context.
        return None
    operation = _operation(action_text)
    if not operation:
        return None
    calendar_id = os.getenv(GOOGLE_CALENDAR_ID_ENV, DEFAULT_CALENDAR_ID).strip()
    timezone = os.getenv(GOOGLE_CALENDAR_TIMEZONE_ENV, DEFAULT_CALENDAR_TIMEZONE).strip()
    event_id = _event_id(text)
    event_reference = _event_reference(action_source, operation=operation)
    if operation != "create" and not event_id and not event_reference and previous_request:
        event_reference = _title(previous_request, operation="create")
    event_reference_date = (
        _prior_thread_event_date(previous_request, today=today)
        if operation != "create" and previous_request
        else ""
    )
    title = _title(action_source, operation=operation)
    event_dates = _event_dates(action_source, today=today)
    start_date = event_dates[0] if event_dates else ""
    if operation == "update" and _has_calendar_date_replacement(
        action_source,
        event_dates,
    ):
        event_reference_date = event_reference_date or event_dates[0]
        start_date = event_dates[-1]
    repeat_each_day = bool(
        len(event_dates) > 1
        and re.search(
            r"\b(?:each|every)\s+(?:day|morning|afternoon|evening|weekday)\b",
            action_source,
            re.I,
        )
        and re.search(
            r"\b(?:through|until|between|complete|finish|end|from|to)\b",
            action_source,
            re.I,
        )
    )
    end_date = event_dates[-1] if repeat_each_day else ""
    start_time, end_time = _event_times(action_source)
    if start_time and not end_time:
        end_time = default_calendar_end_time(start_time)
    description = _description(action_source)
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
        end_date=end_date,
        start_time=start_time,
        end_time=end_time,
        repeat_each_day=repeat_each_day,
        description=description,
        event_id=event_id,
        event_reference=event_reference,
        event_reference_date=event_reference_date,
        calendar_id=calendar_id or DEFAULT_CALENDAR_ID,
        timezone=timezone or DEFAULT_CALENDAR_TIMEZONE,
        all_day=not bool(start_time),
        complete=not blockers,
        blockers=tuple(blockers),
    )


def _strip_slack_emphasis(text: str) -> str:
    """Remove Slack mrkdwn emphasis without rewriting operator content."""

    return str(text or "").replace("*", "")


def _slack_thread_action_context(text: str) -> tuple[str, str]:
    """Return the prior request and latest instruction from a Slack continuation wrapper."""

    if "continue this prior slack thread" not in text.lower():
        return "", ""
    previous = re.findall(
        r"Previous request:\s*(.*?)"
        r"(?=\s+(?:Previous result title:|Previous result:|User follow-up:|"
        r"Continue the same agent task\b)|$)",
        text,
        flags=re.I,
    )
    followups = re.findall(
        r"User follow-up:\s*(.*?)"
        r"(?=\s+(?:Continue the same agent task\b|Previous result title:|"
        r"Previous result:|User follow-up:)|$)",
        text,
        flags=re.I,
    )
    return (
        " ".join(previous[-1].split()).strip() if previous else "",
        " ".join(followups[-1].split()).strip() if followups else "",
    )


def current_calendar_date(timezone: str = DEFAULT_CALENDAR_TIMEZONE) -> date:
    """Return today's date in the configured operator timezone."""

    from datetime import datetime

    return datetime.now(ZoneInfo(timezone)).date()


def compact_calendar_interpretation_request(request_text: str) -> str:
    """Remove accumulated Slack result/error text before the one-turn model call."""

    text = " ".join(str(request_text or "").split()).strip()
    previous_request, latest_followup = _slack_thread_action_context(text)
    if not latest_followup:
        return text
    lines = [f"Current operator request: {latest_followup}"]
    if previous_request:
        lines.append(f"Previous Calendar request: {previous_request}")
    event_id = _event_id(text)
    if event_id:
        lines.append(f"Prior Calendar event ID: {event_id}")
    return "\n".join(lines)


def calendar_interpretation_context(request_text: str) -> CalendarInterpretationContext:
    """Return bounded directive/context text plus an immutable note payload.

    Calendar action words inside an event description must never influence the
    requested operation. The model receives only a payload descriptor; Python
    preserves the exact operator text for the provider write.
    """

    compact = compact_calendar_interpretation_request(request_text)
    lines = compact.splitlines()
    current = (
        lines[0].removeprefix("Current operator request: ")
        if lines
        else ""
    )
    thread_context = "\n".join(lines[1:])
    payload = _description(current)
    directive = current
    if payload:
        payload_index = directive.find(payload)
        if payload_index >= 0:
            directive = (
                directive[:payload_index]
                + "[event description supplied]"
                + directive[payload_index + len(payload):]
            )
    directive = " ".join(directive.split()).strip()
    payload_kind = "url" if re.fullmatch(r"https?://\S+", payload) else "text"
    return CalendarInterpretationContext(
        directive_text=directive,
        thread_context=thread_context,
        description_payload=payload,
        event_fact_text=_calendar_payload_fact_evidence(payload),
        description_payload_kind=payload_kind if payload else "",
        description_payload_sha256=(
            sha256(payload.encode("utf-8")).hexdigest() if payload else ""
        ),
    )


def _calendar_payload_fact_evidence(payload: str, *, max_characters: int = 2_000) -> str:
    """Expose bounded scheduling clauses without asking the model to echo the note."""

    clean_payload = " ".join(str(payload or "").split()).strip(" :")
    if not clean_payload:
        return ""
    month_names = "|".join(MONTHS)
    fact_pattern = re.compile(
        rf"\b(?:{month_names}|scheduled|planned|expected|begin|start|complete|"
        r"finish|arriv\w*|each\s+(?:day|morning)|every\s+(?:day|morning)|"
        r"through|until)\b|\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b",
        re.I,
    )
    sentences = re.split(r"(?<=[.!?])\s+", clean_payload)
    selected = [sentence for sentence in sentences if fact_pattern.search(sentence)]
    evidence = " ".join(selected or sentences[:1]).strip()
    return evidence[:max_characters].rstrip()


def default_calendar_end_time(start_time: str, *, duration_minutes: int = 60) -> str:
    """Return the configured one-hour-style end time for a start-only event."""

    if duration_minutes < 1 or duration_minutes > 1_440:
        raise ValueError("Calendar duration_minutes must be from 1 to 1440.")
    try:
        start = datetime.strptime(start_time.strip(), "%H:%M")
    except ValueError:
        return ""
    return (start + timedelta(minutes=duration_minutes)).strftime("%H:%M")


def is_calendar_action_candidate(request_text: str) -> bool:
    """Admit only action-bound Calendar requests to the specialized interpreter.

    Calendar words are common in operator context. A mention of a meeting plus
    an unrelated verb such as ``what changed`` or ``remove blockers`` must stay
    on the shared semantic-planning path. This gate is only a high-confidence
    fast-path hint; requests it does not admit can still reach Calendar after
    normal interpretation.
    """

    text = " ".join(str(request_text or "").split()).strip()
    previous_request, latest_followup = _slack_thread_action_context(text)
    current_request = latest_followup or text
    return bool(
        _explicit_calendar_mutation_requested(current_request)
        or _looks_like_implicit_calendar_deadline(current_request)
        or _looks_like_implicit_calendar_change(current_request)
        or (
            previous_request
            and _calendar_provider_context_present(previous_request)
            and _calendar_thread_followup_mutation_requested(current_request)
        )
    )


def _explicit_calendar_mutation_requested(text: str) -> bool:
    """Require the mutation verb to be bound to a Calendar object or destination."""

    normalized = _without_negated_calendar_action_clauses(text)
    if not normalized:
        return False
    create_verb = r"(?:add|create|put|schedule)"
    mutate_verb = (
        r"(?:cancel|change|delete|edit|modify|move|remove|rename|"
        r"reschedule|shift|update)"
    )
    calendar_object = (
        r"(?:calendar\s+)?(?:event|meeting|appointment|reminder)"
        r"(?!\s+(?:agenda|minutes|notes?|recording|summary|transcript)\b)"
    )
    mutate_object = (
        rf"\b{mutate_verb}\b"
        r"(?:(?![.!?;]).){0,120}\b"
        rf"{calendar_object}\b"
    )
    create_object = (
        rf"\b{create_verb}\b"
        r"(?:(?![.!?;]).){0,100}\b"
        rf"{calendar_object}\b"
    )
    calendar_destination = (
        rf"\b(?:{create_verb}|{mutate_verb})\b"
        r"(?:(?![.!?;]).){0,120}\b"
        r"(?:to|on|into|from)\s+(?:my|the|our)?\s*calendar\b"
    )
    object_first = (
        rf"\b{calendar_object}\b"
        r"(?:(?![.!?;]).){0,80}\b"
        r"(?:(?:needs?|has|should|must|can|could|would)\s+to\s+|"
        r"(?:needs?|should|must|can|could|would)\s+be\s+)?"
        r"(?:cancelled|canceled|changed|deleted|edited|modified|moved|"
        r"removed|renamed|rescheduled|shifted|updated)\b"
    )
    return any(
        re.search(pattern, normalized, re.I)
        for pattern in (
            mutate_object,
            create_object,
            calendar_destination,
            object_first,
        )
    )


def _calendar_provider_context_present(text: str) -> bool:
    """Recognize a prior thread as Calendar context without inferring a new action."""

    return bool(
        re.search(
            r"\b(?:calendar|event|meeting|appointment|reminder)\b",
            _without_negated_calendar_action_clauses(text),
            re.I,
        )
    )


def _calendar_thread_followup_mutation_requested(text: str) -> bool:
    """Recognize an imperative mutation in a thread with established Calendar identity."""

    normalized = _without_negated_calendar_action_clauses(text)
    return bool(
        re.search(
            r"^(?:(?:please|then)\s+|"
            r"(?:can|could|would)\s+you\s+|"
            r"i\s+need\s+you\s+to\s+)*"
            r"(?:add|append|cancel|change|delete|edit|make|modify|move|"
            r"remove|rename|reschedule|set|shift|update)\b",
            normalized,
            re.I,
        )
    )


def _without_negated_calendar_action_clauses(text: str) -> str:
    """Remove prohibited actions before evaluating Calendar fast-path evidence."""

    normalized = " ".join(str(text or "").split()).strip()
    return re.sub(
        r"\b(?:do\s+not|don't|dont|never|without)\b[^.;]*",
        " ",
        normalized,
        flags=re.I,
    ).strip()


def _looks_like_implicit_calendar_deadline(text: str) -> bool:
    """Admit a dated deadline/reminder to structured Calendar interpretation."""

    normalized = " ".join(str(text or "").split())
    if not normalized:
        return False
    if re.search(
        r"\b(?:airtable|gmail|google\s+(?:docs?|drive|sheets?)|zotero|slack)\b",
        normalized,
        re.I,
    ):
        return False
    has_action = bool(
        re.search(r"\b(?:add|put|schedule|remember|remind)\b", normalized, re.I)
    )
    has_deadline_shape = bool(
        re.search(
            r"\b(?:due|deadline|reminder|appointment|application)\b",
            normalized,
            re.I,
        )
    )
    has_date = bool(
        re.search(
            r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
            r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
            r"nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?"
            r"(?:\s*,?\s*\d{4})?\b|\b\d{4}-\d{2}-\d{2}\b",
            normalized,
            re.I,
        )
    )
    return has_action and has_deadline_shape and has_date


def _looks_like_implicit_calendar_change(text: str) -> bool:
    """Admit a named item moved between two explicit dates as a Calendar update."""

    normalized = " ".join(str(text or "").split())
    if not normalized or re.search(
        r"\b(?:airtable|gmail|google\s+(?:docs?|drive|sheets?)|zotero|slack)\b",
        normalized,
        re.I,
    ):
        return False
    if not re.search(r"\b(?:move|reschedule|shift)\b", normalized, re.I):
        return False
    return len(_event_dates(normalized, today=current_calendar_date())) >= 2


def calendar_thread_event_context(
    request_text: str,
    *,
    today: date | None = None,
) -> dict[str, str]:
    """Return bounded prior-event identity for an LLM-interpreted thread action."""

    text = " ".join(str(request_text or "").split()).strip()
    previous_request, _latest_followup = _slack_thread_action_context(text)
    return {
        "event_id": _event_id(text),
        "event_reference": (
            _title(previous_request, operation="create") if previous_request else ""
        ),
        "event_reference_date": (
            _prior_thread_event_date(previous_request, today=today)
            if previous_request
            else ""
        ),
    }


def _operation(lower: str) -> str:
    if re.search(r"\b(?:delete|remove|cancel)\b", lower):
        return "delete"
    if re.search(
        r"\b(?:update|modify|change|rename|move|reschedule|shift|edit|"
        r"add\s+(?:a\s+)?note)\b",
        lower,
    ):
        return "update"
    if re.search(r"\b(?:add|create|schedule|put)\b", lower):
        return "create"
    return ""


def _event_date(text: str, *, today: date | None) -> str:
    dates = _event_dates(text, today=today)
    return dates[0] if dates else ""


def _prior_thread_event_date(text: str, *, today: date | None) -> str:
    """Resolve a prior request's date without rolling a just-created event forward."""

    reference = today or current_calendar_date()
    return _event_date(text, today=date(reference.year, 1, 1))


def _event_dates(text: str, *, today: date | None) -> list[str]:
    found: list[str] = []
    for iso_value in re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", text):
        try:
            normalized = date.fromisoformat(iso_value).isoformat()
        except ValueError:
            continue
        if normalized not in found:
            found.append(normalized)
    for match in re.finditer(
        r"\b(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>20\d{2})\b",
        text,
    ):
        try:
            normalized = date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            ).isoformat()
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


def calendar_lookup_date(
    request_text: str,
    *,
    today: date | None = None,
) -> str:
    """Return the newest explicit date that can bound a Calendar read."""

    text = " ".join(str(request_text or "").split()).strip()
    previous_request, latest_followup = _slack_thread_action_context(text)
    for candidate in (latest_followup, previous_request, text):
        dates = _event_dates(candidate, today=today) if candidate else []
        if dates:
            return dates[-1]
    return ""


def _has_calendar_date_replacement(text: str, event_dates: list[str]) -> bool:
    """Recognize an old-date to new-date update without choosing a route."""

    if len(event_dates) < 2:
        return False
    return bool(
        re.search(
            r"\b(?:move|reschedule|shift|change|update|modify)\b"
            r".*?\b(?:from|on)\b.*?\b(?:to|for)\b",
            text,
            re.I,
        )
    )


def _event_times(text: str) -> tuple[str, str]:
    clock = (
        r"(?P<{name}_hour>\d{{1,2}})(?::(?P<{name}_minute>\d{{2}}))?\s*"
        r"(?P<{name}_ampm>a\.?m\.?|p\.?m\.?)?"
    )
    labeled_range = re.search(
        rf"\btime\s*:\s*{clock.format(name='start')}\s*(?:to|[-–])\s*"
        rf"{clock.format(name='end')}\b",
        text,
        re.I,
    )
    if labeled_range:
        end_ampm = labeled_range.group("end_ampm") or ""
        start_ampm = labeled_range.group("start_ampm") or end_ampm
        return (
            _normalize_clock(
                labeled_range.group("start_hour"),
                labeled_range.group("start_minute"),
                start_ampm,
            ),
            _normalize_clock(
                labeled_range.group("end_hour"),
                labeled_range.group("end_minute"),
                end_ampm,
            ),
        )
    replacement_range = re.search(
        rf"\bto\s+{clock.format(name='start')}\s*[-–]\s*"
        rf"{clock.format(name='end')}\b",
        text,
        re.I,
    )
    if replacement_range:
        end_ampm = replacement_range.group("end_ampm") or ""
        start_ampm = replacement_range.group("start_ampm") or end_ampm
        return (
            _normalize_clock(
                replacement_range.group("start_hour"),
                replacement_range.group("start_minute"),
                start_ampm,
            ),
            _normalize_clock(
                replacement_range.group("end_hour"),
                replacement_range.group("end_minute"),
                end_ampm,
            ),
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
        rf"\b(?:at|from|around)\s+{clock.format(name='start')}\b",
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
    labeled = re.search(
        r"\bevent\s*:\s*(?P<title>.+?)(?=\s+(?:dates?|time|notes?)\s*:|$)",
        text,
        re.I,
    )
    if labeled:
        return _clean_title(labeled.group("title"))
    quoted = re.search(
        r"\b(?:titled|called|named)\s+[\"“](?P<title>.+?)[\"”]",
        text,
        re.I,
    )
    if quoted:
        return _clean_title(quoted.group("title"))
    explicit = re.search(
        r"\b(?:titled|called|named)\s+(?P<title>.+?)"
        r"(?=\s+(?:with\s+(?:a\s+)?notes?\b|notes?:|on\s+(?:"
        r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
        r"dec(?:ember)?)\b|\d{4}-\d{2}-\d{2}\b|calendar\b)|"
        r"(?:from|at)\s+\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?\b)|$)",
        text,
        re.I,
    )
    if explicit:
        return _clean_title(explicit.group("title"))
    if operation == "create":
        quoted_action = re.search(
            r"\b(?:add|put|schedule|remember|remind)\s+"
            r"[\"“](?P<title>.+?)[\"”]",
            text,
            re.I,
        )
        if quoted_action:
            return _clean_title(quoted_action.group("title"))
        prose_subject = re.search(
            r"(?:^|[:.!?]\s+)(?P<title>[A-Z][^:;.!?]{1,120}?)\s+"
            r"(?:is\s+(?:scheduled|planned|expected|set)\s+to|will)\s+"
            r"(?:begin|start|occur)\b",
            text,
        )
        if prose_subject:
            return _clean_title(prose_subject.group("title"))
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
    cleaned = re.split(
        r"\s+(?:with\s+(?:a\s+)?notes?\b|notes?:)\s*",
        value,
        maxsplit=1,
        flags=re.I,
    )[0]
    return cleaned.strip(" .,:;\"'“”‘’*")[:240]


def _description(text: str) -> str:
    quoted = re.search(
        r"\b(?:with\s+(?:a\s+)?notes?\s*:?\s*|notes?\s*:\s*)"
        r"[\"“](?P<note>.+?)[\"”]",
        text,
        re.I,
    )
    if quoted:
        return _clean_description_payload(quoted.group("note"))
    match = re.search(
        r"\b(?:with\s+(?:a\s+)?notes?\s*:?\s*|notes?\s*:\s*)"
        r"[\"“]?(?P<note>.+)$",
        text,
        re.I,
    )
    if match:
        return _clean_description_payload(match.group("note"))
    direct_change = re.search(
        r"\b(?:change|update|edit|add)\s+"
        r"(?:(?:its|the|this|that)\s+)?notes?\s+(?:to|as)\s+"
        r"[\"“](?P<note>.+?)[\"”]",
        text,
        re.I,
    )
    if direct_change:
        return _clean_description_payload(direct_change.group("note"))
    change = re.search(
        r"\b(?:change|update|edit|add)\s+(?:the\s+|a\s+)?note\s+"
        r"(?:on|for)\s+(?:the\s+)?(?:.+?)\s+(?:calendar\s+)?event\s+"
        r"(?:to|as)\s+[\"“]?(?P<note>.+)$",
        text,
        re.I,
    )
    return _clean_description_payload(change.group("note")) if change else ""


def _clean_description_payload(value: str) -> str:
    """Remove only wrapping whitespace/quotes; preserve operator punctuation."""

    cleaned = str(value or "").strip()
    if len(cleaned) >= 2 and (cleaned[0], cleaned[-1]) in {
        ('"', '"'),
        ('“', '”'),
        ("'", "'"),
        ('‘', '’'),
    }:
        return cleaned[1:-1].strip()
    return cleaned


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
    after_event = re.search(
        r"\b(?:update|modify|change|edit|move|reschedule|shift|rename|"
        r"delete|remove|cancel)\s+"
        r"(?:the\s+)?(?:(?:all[- ]day|calendar)\s+)?event\s+"
        r"(?P<reference>.+?)(?=\s+(?:from|on|to|for|with)\b|$)",
        text,
        re.I,
    )
    if after_event:
        return _clean_event_reference(after_event.group("reference"))
    action = re.search(
        r"\b(?:update|modify|change|edit|move|reschedule|shift|rename|"
        r"delete|remove|cancel)\s+"
        r"(?:the\s+)?(?P<reference>.+?)\s+(?:calendar\s+)?event"
        r"(?=\s+(?:from|on|to|for|with)\b|$)",
        text,
        re.I,
    )
    if action:
        return _clean_event_reference(action.group("reference"))
    dated_move = re.search(
        r"\b(?:move|reschedule|shift)\s+(?:the\s+)?(?P<reference>.+?)"
        r"(?=\s+(?:(?:one|a)\s+day\s+later\s*[—–-]?\s*)?"
        r"(?:from|on)\b)",
        text,
        re.I,
    )
    return _clean_event_reference(dated_move.group("reference")) if dated_move else ""


def _clean_event_reference(value: str) -> str:
    cleaned = re.sub(r"^(?:the\s+)", "", str(value or "").strip(), flags=re.I)
    cleaned = cleaned.strip(" .,:;\"“”")[:240]
    if re.fullmatch(r"(?:(?:this|that)\s+)?same|this|that", cleaned, re.I):
        return ""
    return cleaned


def _event_id(text: str) -> str:
    match = re.search(
        r"\b(?:event[_ -]?id|event)\s*[:#]?\s*(?P<id>kba[0-9a-f]{16,64})\b",
        text,
        re.I,
    )
    return match.group("id").lower() if match else ""
