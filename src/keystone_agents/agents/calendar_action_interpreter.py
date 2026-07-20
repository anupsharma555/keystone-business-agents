"""Bounded LLM-first interpretation for natural-language Calendar actions."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from keystone_agents.calendar_actions import (
    CalendarActionPlan,
    calendar_interpretation_context,
    calendar_thread_event_context,
    default_calendar_end_time,
    infer_calendar_action_plan,
    is_calendar_action_candidate,
)
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.calendar_action import (
    CalendarActionInterpretation,
    CalendarActionInterpretationInput,
)
from keystone_agents.sdk import Agent, build_model_settings, build_sdk_agent, compose_instructions
from keystone_agents.tools.google_calendar_tool import (
    DEFAULT_CALENDAR_ID,
    DEFAULT_CALENDAR_TIMEZONE,
    GOOGLE_CALENDAR_ID_ENV,
    GOOGLE_CALENDAR_TIMEZONE_ENV,
)


@dataclass(frozen=True)
class CalendarActionResolution:
    """Validated Calendar plan plus bounded model-use diagnostics."""

    plan: CalendarActionPlan | None
    interpreter_used: bool = False
    openai_requests: int = 0
    warnings: tuple[str, ...] = ()


def build_calendar_action_interpreter_agent(model: str | None = None) -> Agent:
    """Build a tool-less structured interpreter for Calendar action candidates."""

    return build_sdk_agent(
        name="calendar_action_interpreter",
        instructions=compose_instructions(
            "keystone_profile.md",
            "safety_policy.md",
            "calendar_action_interpreter.md",
        ),
        output_type=CalendarActionInterpretation,
        tools=[],
        model=model,
        model_settings=build_model_settings(
            reasoning_effort="none",
            verbosity="low",
            max_tokens=1200,
        ),
        handoff_description=(
            "Interpret one Calendar action before deterministic write validation."
        ),
        enforce_tool_policy=False,
    )


def resolve_calendar_action_plan(
    request_text: str,
    fallback: CalendarActionPlan | None,
    *,
    live: bool = False,
    run_config: Any | None = None,
    model: str | None = None,
    today: date | None = None,
) -> CalendarActionResolution:
    """Use at most one model turn, then reapply deterministic Calendar gates."""

    requires_interpretation = bool(
        (fallback and fallback.operation in {"create", "update", "delete"})
        or is_calendar_action_candidate(request_text)
    )
    if not requires_interpretation or (not live and run_config is None):
        return CalendarActionResolution(plan=fallback)
    interpretation_context = calendar_interpretation_context(request_text)
    deterministic_plan = {
        key: list(value) if isinstance(value, tuple) else value
        for key, value in (fallback.__dict__.items() if fallback else [])
        if key != "description"
    }
    if interpretation_context.description_payload:
        deterministic_plan["description"] = {
            "source": "operator_payload",
            "characters": len(interpretation_context.description_payload),
            "sha256": interpretation_context.description_payload_sha256,
        }
    try:
        result = run_typed_sdk_agent(
            agent=build_calendar_action_interpreter_agent(model=model),
            typed_input=CalendarActionInterpretationInput(
                request_text=interpretation_context.directive_text,
                thread_context=interpretation_context.thread_context,
                event_fact_text=interpretation_context.event_fact_text,
                description_payload_present=bool(
                    interpretation_context.description_payload
                ),
                description_payload_kind=(
                    interpretation_context.description_payload_kind
                ),
                description_payload_length=len(
                    interpretation_context.description_payload
                ),
                description_payload_sha256=(
                    interpretation_context.description_payload_sha256
                ),
                deterministic_plan=deterministic_plan,
            ),
            output_type=CalendarActionInterpretation,
            run_config=run_config,
            live=live,
            workflow_name="Keystone Calendar action interpretation",
            tracing_disabled=True,
            max_turns=1,
        )
    except Exception as exc:
        warning_detail = " ".join(str(exc).split())[:240]
        blocked = _blocked_plan(
            fallback,
            "LLM calendar interpretation unavailable",
        ) if fallback is not None else CalendarActionPlan(
            operation="create",
            complete=False,
            blockers=("LLM calendar interpretation unavailable",),
        )
        return CalendarActionResolution(
            plan=blocked,
            interpreter_used=True,
            openai_requests=1 if type(exc).__name__ == "ModelBehaviorError" else 0,
            warnings=(
                f"Calendar interpretation unavailable: {type(exc).__name__}"
                + (f": {warning_detail}" if warning_detail else ""),
            ),
        )
    if result.output.operation == "none":
        plan, warnings = None, ()
    elif fallback is None:
        plan, warnings = _plan_from_model_interpretation(
            request_text,
            result.output,
            today=today,
        )
    else:
        plan, warnings = _validated_interpretation_plan(
            request_text,
            fallback,
            result.output,
            today=today,
        )
    return CalendarActionResolution(
        plan=plan,
        interpreter_used=True,
        openai_requests=1,
        warnings=warnings,
    )


def _plan_from_model_interpretation(
    request_text: str,
    interpretation: CalendarActionInterpretation,
    *,
    today: date | None,
) -> tuple[CalendarActionPlan, tuple[str, ...]]:
    """Build a bounded plan when deterministic parsing could not understand the wording."""

    request_context = calendar_interpretation_context(request_text)
    current_request = request_text
    context = calendar_thread_event_context(request_text, today=today)
    operation = interpretation.operation
    title = (
        _clean_source_value(interpretation.title)
        if _field_is_current(
            interpretation.title,
            interpretation.title_source_text,
            current_request,
        )
        else ""
    )
    start_date = (
        interpretation.start_date
        if _field_is_current(
            interpretation.start_date,
            interpretation.date_source_text,
            current_request,
        )
        else ""
    )
    start_time = (
        interpretation.start_time
        if _field_is_current(
            interpretation.start_time,
            interpretation.time_source_text,
            current_request,
        )
        else ""
    )
    end_date = (
        interpretation.end_date
        if _field_is_current(
            interpretation.end_date,
            interpretation.end_date_source_text or interpretation.date_source_text,
            current_request,
        )
        else ""
    )
    end_time = interpretation.end_time if start_time else ""
    if start_time and not end_time:
        end_time = default_calendar_end_time(start_time)
    description = _resolved_interpreted_description(
        request_context.description_payload,
        interpretation,
        request_text,
    )
    event_id = interpretation.event_id or context["event_id"]
    event_reference = (
        _clean_source_value(interpretation.event_reference)
        or context["event_reference"]
    )
    blockers: list[str] = []
    if operation == "create":
        if not title:
            blockers.append("event title")
        if not start_date:
            blockers.append("event date")
    else:
        if not event_id and not event_reference:
            blockers.append("event name or exact event id")
        if operation == "update" and not any(
            (title, start_date, start_time, description, interpretation.all_day is True)
        ):
            blockers.append("updated title, date, time, or note")
    plan = CalendarActionPlan(
        operation=operation,
        title=title,
        start_date=start_date,
        end_date=end_date,
        start_time=start_time,
        end_time=end_time,
        repeat_each_day=bool(
            interpretation.repeat_each_day and end_date
        ),
        description=description,
        append_description=interpretation.description_mode == "append",
        event_id=event_id,
        event_reference=event_reference,
        event_reference_date=context["event_reference_date"],
        calendar_id=(
            os.getenv(GOOGLE_CALENDAR_ID_ENV, DEFAULT_CALENDAR_ID).strip()
            or DEFAULT_CALENDAR_ID
        ),
        timezone=(
            interpretation.timezone
            or os.getenv(GOOGLE_CALENDAR_TIMEZONE_ENV, DEFAULT_CALENDAR_TIMEZONE).strip()
            or DEFAULT_CALENDAR_TIMEZONE
        ),
        all_day=(
            interpretation.all_day
            if interpretation.all_day is not None
            else not bool(start_time)
        ),
        complete=not blockers,
        blockers=tuple(blockers),
    )
    warnings = tuple(
        f"Calendar interpretation note: {item}" for item in interpretation.ambiguities
    )
    return plan, warnings


def _field_is_current(value: str, evidence: str, current_request: str) -> bool:
    normalized_request = _normalize_source_text(current_request)
    normalized_value = _normalize_source_text(value)
    normalized_evidence = _normalize_source_text(evidence)
    return bool(
        normalized_value
        and (
            normalized_value in normalized_request
            or (normalized_evidence and normalized_evidence in normalized_request)
        )
    )


def _validated_interpretation_plan(
    request_text: str,
    fallback: CalendarActionPlan,
    interpretation: CalendarActionInterpretation,
    *,
    today: date | None,
) -> tuple[CalendarActionPlan, tuple[str, ...]]:
    warnings: list[str] = []
    if interpretation.operation != fallback.operation:
        if not _operation_source_anchored(request_text, interpretation):
            return (
                _blocked_plan(fallback, "calendar operation was not source verified"),
                ("Calendar operation lacked directive evidence.",),
            )
        return _plan_from_model_interpretation(
            request_text,
            interpretation,
            today=today,
        )
    if fallback.complete:
        return _validated_complete_deterministic_plan(
            request_text,
            fallback,
            interpretation,
        )
    warnings.extend(
        f"Calendar interpretation note: {item}"
        for item in interpretation.ambiguities
    )

    if fallback.operation != "create":
        return _validated_existing_event_plan(
            request_text,
            fallback,
            interpretation,
            today=today,
        )

    title = fallback.title
    interpreted_title = _clean_source_value(interpretation.title)
    if not interpreted_title:
        return (
            _blocked_plan(fallback, "LLM interpretation did not verify the event title"),
            ("Calendar title was missing from the model interpretation.",),
        )
    if title and not _titles_compatible(title, interpreted_title):
        return (
            _blocked_plan(fallback, "LLM and deterministic event titles disagree"),
            ("Calendar title interpretations disagreed.",),
        )
    if not title and not _title_source_anchored(
        interpreted_title,
        interpretation.title_source_text,
        request_text,
    ):
        return (
            _blocked_plan(fallback, "LLM interpretation did not verify the event title"),
            ("Calendar title was not anchored to the operator request.",),
        )
    title = interpreted_title

    start_date = fallback.start_date
    if start_date:
        if interpretation.start_date != start_date:
            return (
                _blocked_plan(fallback, "LLM interpretation did not verify the event date"),
                ("Calendar date interpretations disagreed.",),
            )
    elif interpretation.start_date:
        if _field_matches_deterministic_parse(
            request_text,
            evidence=interpretation.date_source_text,
            expected=interpretation.start_date,
            field="start_date",
            operation=fallback.operation,
            today=today,
        ):
            start_date = interpretation.start_date
        else:
            warnings.append("Calendar date could not be deterministically verified.")
    if not start_date:
        return (
            _blocked_plan(fallback, "LLM interpretation did not verify the event date"),
            tuple(warnings or ["Calendar date remained unverified."]),
        )

    end_date = fallback.end_date
    if end_date:
        if interpretation.end_date and interpretation.end_date != end_date:
            return (
                _blocked_plan(fallback, "LLM interpretation did not verify the event end date"),
                ("Calendar end-date interpretations disagreed.",),
            )
    elif interpretation.end_date:
        if _field_matches_deterministic_parse(
            request_text,
            evidence=(
                interpretation.end_date_source_text
                or interpretation.date_source_text
            ),
            expected=interpretation.end_date,
            field="end_date",
            operation=fallback.operation,
            today=today,
        ):
            end_date = interpretation.end_date
        else:
            warnings.append("Calendar end date could not be deterministically verified.")
    repeat_each_day = bool(
        (fallback.repeat_each_day or interpretation.repeat_each_day) and end_date
    )

    start_time = fallback.start_time
    end_time = fallback.end_time
    if start_time:
        if interpretation.start_time != start_time:
            return (
                _blocked_plan(fallback, "LLM interpretation did not verify the event time"),
                ("Calendar time interpretations disagreed.",),
            )
        defaulted_end_time = bool(
            start_time
            and end_time == default_calendar_end_time(start_time)
            and not interpretation.end_time
        )
        if end_time and interpretation.end_time != end_time and not defaulted_end_time:
            return (
                _blocked_plan(fallback, "LLM and deterministic event end times disagree"),
                ("Calendar end-time interpretations disagreed.",),
            )
    elif interpretation.start_time:
        if _field_matches_deterministic_parse(
            request_text,
            evidence=interpretation.time_source_text,
            expected=interpretation.start_time,
            field="start_time",
            operation=fallback.operation,
            today=today,
        ):
            start_time = interpretation.start_time
            end_time = interpretation.end_time
        else:
            return (
                _blocked_plan(fallback, "LLM interpretation proposed an unverified event time"),
                ("Calendar time could not be deterministically verified.",),
            )
    if start_time and not end_time:
        end_time = default_calendar_end_time(start_time)

    timezone = _resolved_timezone(
        request_text,
        interpretation.timezone_source_text,
        deterministic_timezone=fallback.timezone,
        interpreted_timezone=interpretation.timezone,
    )
    if timezone is None:
        return (
            _blocked_plan(fallback, "LLM interpretation did not verify the event timezone"),
            ("Calendar timezone interpretation or source evidence disagreed.",),
        )

    request_context = calendar_interpretation_context(request_text)
    description = fallback.description or request_context.description_payload
    if not description and interpretation.description:
        if _source_anchored(
            interpretation.description,
            interpretation.description_source_text,
            request_text,
        ):
            description = interpretation.description
        else:
            return (
                _blocked_plan(fallback, "LLM interpretation proposed an unverified event note"),
                ("Calendar description was not anchored to the operator request.",),
            )

    blockers = tuple(
        blocker
        for blocker in fallback.blockers
        if not (
            (blocker == "event title" and title)
            or (blocker == "event date" and start_date)
            or (blocker == "updated title, date, or note" and (title or start_date or description))
        )
    )
    return (
        replace(
            fallback,
            title=title,
            start_date=start_date,
            end_date=end_date,
            start_time=start_time,
            end_time=end_time,
            repeat_each_day=repeat_each_day,
            description=description,
            timezone=timezone,
            complete=not blockers,
            blockers=blockers,
        ),
        tuple(warnings),
    )


def _validated_complete_deterministic_plan(
    request_text: str,
    fallback: CalendarActionPlan,
    interpretation: CalendarActionInterpretation,
) -> tuple[CalendarActionPlan, tuple[str, ...]]:
    """Keep verified Python fields and merge source-anchored model-only fields."""

    comparable_values = (
        ("event title", fallback.title, _clean_source_value(interpretation.title), True),
        ("event date", fallback.start_date, interpretation.start_date, False),
        ("event start time", fallback.start_time, interpretation.start_time, False),
        ("event end time", fallback.end_time, interpretation.end_time, False),
        ("event id", fallback.event_id, interpretation.event_id, False),
        (
            "event reference",
            fallback.event_reference,
            _clean_source_value(interpretation.event_reference),
            True,
        ),
    )
    resolved_title = fallback.title
    resolved_reference = fallback.event_reference
    request_context = calendar_interpretation_context(request_text)
    resolved_description = fallback.description or request_context.description_payload
    append_description = fallback.append_description
    for label, deterministic_value, interpreted_value, title_like in comparable_values:
        if not deterministic_value or not interpreted_value:
            continue
        agrees = (
            _titles_compatible(deterministic_value, interpreted_value)
            if title_like
            else deterministic_value.lower() == interpreted_value.lower()
        )
        if not agrees:
            return (
                _blocked_plan(fallback, f"LLM and deterministic {label} values disagree"),
                (f"Calendar {label} interpretations disagreed.",),
            )
        if label == "event title" and title_like:
            resolved_title = interpreted_value
        elif label == "event reference" and title_like:
            resolved_reference = interpreted_value
    if not resolved_description and interpretation.description:
        if not _source_anchored(
            interpretation.description,
            interpretation.description_source_text,
            request_text,
        ):
            return (
                _blocked_plan(
                    fallback,
                    "LLM interpretation proposed an unverified event note",
                ),
                ("Calendar description was not anchored to the operator request.",),
            )
        resolved_description = interpretation.description
        append_description = interpretation.description_mode == "append"
    warnings = tuple(
        f"Calendar interpretation note: {item}" for item in interpretation.ambiguities
    )
    return (
        replace(
            fallback,
            title=resolved_title,
            event_reference=resolved_reference,
            description=resolved_description,
            append_description=append_description,
        ),
        warnings,
    )


def _blocked_plan(fallback: CalendarActionPlan, blocker: str) -> CalendarActionPlan:
    blockers = tuple(dict.fromkeys((*fallback.blockers, blocker)))
    return replace(fallback, complete=False, blockers=blockers)


def _validated_existing_event_plan(
    request_text: str,
    fallback: CalendarActionPlan,
    interpretation: CalendarActionInterpretation,
    *,
    today: date | None,
) -> tuple[CalendarActionPlan, tuple[str, ...]]:
    event_id = fallback.event_id
    event_reference = fallback.event_reference
    if event_id:
        if interpretation.event_id.lower() != event_id.lower():
            return (
                _blocked_plan(fallback, "LLM interpretation did not verify the event id"),
                ("Calendar event id interpretations disagreed.",),
            )
    else:
        interpreted_reference = _clean_source_value(interpretation.event_reference)
        source_anchored = _title_source_anchored(
            interpreted_reference,
            interpretation.event_reference_source_text,
            request_text,
        )
        deterministic_reference_agrees = bool(
            event_reference
            and interpreted_reference
            and _titles_compatible(event_reference, interpreted_reference)
        )
        if not interpreted_reference or not (source_anchored or deterministic_reference_agrees):
            return (
                _blocked_plan(fallback, "LLM interpretation did not verify the event name"),
                ("Calendar event reference was not anchored to the operator request.",),
            )
        if event_reference and not _titles_compatible(event_reference, interpreted_reference):
            return (
                _blocked_plan(fallback, "LLM and deterministic event references disagree"),
                ("Calendar event reference interpretations disagreed.",),
            )
        event_reference = interpreted_reference

    title = fallback.title
    if title:
        interpreted_title = _clean_source_value(interpretation.title)
        if not interpreted_title or not _titles_compatible(title, interpreted_title):
            return (
                _blocked_plan(fallback, "LLM interpretation did not verify the updated title"),
                ("Calendar updated-title interpretation or source evidence disagreed.",),
            )
        title = interpreted_title

    start_date = fallback.start_date
    if start_date and interpretation.start_date != start_date:
        return (
            _blocked_plan(fallback, "LLM interpretation did not verify the updated date"),
            ("Calendar updated-date interpretations disagreed.",),
        )

    start_time = fallback.start_time
    end_time = fallback.end_time
    if start_time and interpretation.start_time != start_time:
        return (
            _blocked_plan(fallback, "LLM interpretation did not verify the updated time"),
            ("Calendar updated-time interpretations disagreed.",),
        )
    if end_time and interpretation.end_time != end_time:
        return (
            _blocked_plan(fallback, "LLM interpretation did not verify the updated end time"),
            ("Calendar updated end-time interpretations disagreed.",),
        )

    request_context = calendar_interpretation_context(request_text)
    description = fallback.description or request_context.description_payload

    timezone = fallback.timezone
    if start_date or start_time:
        resolved_timezone = _resolved_timezone(
            request_text,
            interpretation.timezone_source_text,
            deterministic_timezone=fallback.timezone,
            interpreted_timezone=interpretation.timezone,
        )
        if resolved_timezone is None:
            return (
                _blocked_plan(fallback, "LLM interpretation did not verify the updated timezone"),
                ("Calendar updated-timezone interpretation or source evidence disagreed.",),
            )
        timezone = resolved_timezone

    blockers = tuple(
        blocker
        for blocker in fallback.blockers
        if not (
            blocker == "event name or exact event id" and (event_id or event_reference)
        )
    )
    return (
        replace(
            fallback,
            title=title,
            start_date=start_date,
            start_time=start_time,
            end_time=end_time,
            description=description,
            event_id=event_id,
            event_reference=event_reference,
            timezone=timezone,
            complete=not blockers,
            blockers=blockers,
        ),
        (),
    )


def _clean_source_value(value: str) -> str:
    return value.strip(" .,:;\"'\u201c\u201d\u2018\u2019*")[:240]


def _source_anchored(value: str, evidence: str, request_text: str) -> bool:
    normalized_value = _normalize_source_text(value)
    normalized_evidence = _normalize_source_text(evidence)
    normalized_request = _normalize_source_text(request_text)
    return bool(
        normalized_value
        and normalized_evidence
        and normalized_value in normalized_evidence
        and normalized_evidence in normalized_request
    )


def _operation_source_anchored(
    request_text: str,
    interpretation: CalendarActionInterpretation,
) -> bool:
    evidence = _normalize_source_text(interpretation.operation_source_text)
    directive = _normalize_source_text(
        calendar_interpretation_context(request_text).directive_text
    )
    return bool(evidence and evidence in directive)


def _resolved_interpreted_description(
    payload: str,
    interpretation: CalendarActionInterpretation,
    request_text: str,
) -> str:
    if payload and interpretation.description_from_payload:
        return payload
    if interpretation.description and _source_anchored(
        interpretation.description,
        interpretation.description_source_text,
        request_text,
    ):
        return interpretation.description
    return ""


def _title_source_anchored(value: str, evidence: str, request_text: str) -> bool:
    normalized_value = _normalize_source_text(value)
    normalized_evidence = _normalize_source_text(evidence)
    normalized_request = _normalize_source_text(request_text)
    return bool(
        normalized_value
        and normalized_value == normalized_evidence
        and normalized_evidence in normalized_request
    )


def _titles_compatible(deterministic_title: str, interpreted_title: str) -> bool:
    deterministic = _normalize_source_text(deterministic_title)
    interpreted = _normalize_source_text(interpreted_title)
    return bool(
        deterministic
        and interpreted
        and (deterministic == interpreted or deterministic.startswith(f"{interpreted} "))
    )


def _resolved_timezone(
    request_text: str,
    evidence: str,
    *,
    deterministic_timezone: str,
    interpreted_timezone: str,
) -> str | None:
    normalized_request = _normalize_source_text(request_text)
    normalized_evidence = _normalize_source_text(evidence)
    explicit_markers = {
        "edt": "America/New_York",
        "est": "America/New_York",
        "et": "America/New_York",
        "eastern": "America/New_York",
        "eastern time": "America/New_York",
    }
    present_markers = [
        marker
        for marker in explicit_markers
        if re.search(rf"\b{re.escape(marker)}\b", normalized_request)
    ]
    if not present_markers:
        return (
            deterministic_timezone
            if not normalized_evidence and interpreted_timezone == deterministic_timezone
            else None
        )
    verified = bool(
        normalized_evidence
        and normalized_evidence in normalized_request
        and any(
            re.search(rf"\b{re.escape(marker)}\b", normalized_evidence)
            and explicit_markers[marker] == interpreted_timezone
            for marker in present_markers
        )
    )
    return interpreted_timezone if verified else None


def _normalize_source_text(value: str) -> str:
    return " ".join(re.sub(r"[^\w:+-]", " ", str(value or "").lower()).split())


def _field_matches_deterministic_parse(
    request_text: str,
    *,
    evidence: str,
    expected: str,
    field: str,
    operation: str,
    today: date | None,
) -> bool:
    if _normalize_source_text(evidence) not in _normalize_source_text(request_text):
        return False
    if field in {"start_date", "end_date"}:
        dates = infer_calendar_action_plan(
            "create calendar event. Event: Verification Placeholder "
            f"Dates: {evidence} each day",
            today=today,
        )
        if dates is None:
            return False
        parsed_dates = {
            value for value in (dates.start_date, dates.end_date) if value
        }
        if expected in parsed_dates:
            return True
        return expected in {
            value
            for value in re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", evidence)
        }
    verb = {"create": "create", "update": "update", "delete": "delete"}[operation]
    preposition = "to" if field in {"start_time", "end_time"} else "on"
    probe = infer_calendar_action_plan(
        f"{verb} calendar event that Verification Placeholder {preposition} {evidence}",
        today=today,
    )
    return probe is not None and str(getattr(probe, field, "")) == expected
