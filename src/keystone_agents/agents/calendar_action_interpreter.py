"""Bounded LLM-first interpretation for natural-language Calendar actions."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

from keystone_agents.calendar_actions import (
    CalendarActionPlan,
    calendar_interpretation_context,
    calendar_lookup_date,
    calendar_thread_event_context,
    current_calendar_date,
    default_calendar_end_time,
    infer_calendar_action_plan,
    is_calendar_action_candidate,
)
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.calendar_action import (
    CalendarActionInterpretation,
    CalendarActionInterpretationInput,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.sdk import Agent, build_model_settings, build_sdk_agent, compose_instructions
from keystone_agents.semantic_execution import ExecutionIntentAuthority
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
    manual_plan: ManualRequestPlan | dict[str, object] | None = None,
    semantic_candidate: bool = False,
    live: bool = False,
    run_config: Any | None = None,
    model: str | None = None,
    today: date | None = None,
) -> CalendarActionResolution:
    """Use at most one model turn, then reapply deterministic Calendar gates."""

    authority = ExecutionIntentAuthority.from_value(manual_plan)
    allowed_operations: frozenset[str] | None = None
    canonical_read_scope = "unspecified"
    if authority.invalid:
        return CalendarActionResolution(
            plan=None,
            warnings=(
                "Supplied canonical plan was invalid; Calendar execution did not "
                "fall back to request keywords.",
            ),
        )
    if authority.canonical:
        assert authority.plan is not None
        if not authority.authorizes_provider(
            "google_calendar",
            allowed_agents={"chief_of_staff"},
            allowed_intents={"business_system_write", "context_lookup"},
        ):
            return CalendarActionResolution(
                plan=None,
                warnings=(
                    "Canonical plan did not authorize a Google Calendar action.",
                ),
            )
        allowed_operations = frozenset(
            operation
            for operation in authority.effective_provider_operations(
                "google_calendar"
            )
            if operation in {"read", "create", "update", "delete"}
        )
        canonical_read_scope = authority.plan.provider_read_scope
        if not allowed_operations:
            return CalendarActionResolution(
                plan=None,
                warnings=(
                    "Canonical plan did not specify an executable Calendar operation.",
                ),
            )
        fallback = _calendar_fallback_for_authorized_operations(
            fallback,
            allowed_operations=allowed_operations,
        )

    requires_interpretation = bool(
        allowed_operations
        or semantic_candidate
        or (fallback and fallback.operation in {"read", "create", "update", "delete"})
        or (authority.fallback_allowed and is_calendar_action_candidate(request_text))
    )
    if not requires_interpretation or (not live and run_config is None):
        return CalendarActionResolution(plan=fallback)
    interpretation_context = calendar_interpretation_context(request_text)
    deterministic_plan = {
        key: list(value) if isinstance(value, tuple) else value
        for key, value in (fallback.__dict__.items() if fallback else [])
        if key != "description"
    }
    if canonical_read_scope != "unspecified":
        deterministic_plan["provider_read_scope"] = canonical_read_scope
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
    interpretation = result.output
    if allowed_operations is not None:
        if len(allowed_operations) == 1:
            authorized_operation = next(iter(allowed_operations))
            interpretation = interpretation.model_copy(
                update={"operation": authorized_operation}
            )
        elif interpretation.operation not in allowed_operations:
            return CalendarActionResolution(
                plan=(
                    _blocked_plan(
                        fallback,
                        "calendar operation was not authorized by the canonical plan",
                    )
                    if fallback is not None
                    else None
                ),
                interpreter_used=True,
                openai_requests=1,
                warnings=(
                    "Calendar interpreter proposed an operation outside the canonical plan.",
                ),
            )
    if interpretation.operation == "read":
        if canonical_read_scope == "bounded_collection":
            interpretation = _refine_bounded_calendar_read(
                request_text,
                interpretation,
            )
        elif canonical_read_scope == "single_item":
            interpretation = interpretation.model_copy(
                update={"read_scope": "single_event"}
            )
    if interpretation.operation == "none":
        plan, warnings = None, ()
    elif fallback is None:
        plan, warnings = _plan_from_model_interpretation(
            request_text,
            interpretation,
            today=today,
        )
    else:
        plan, warnings = _validated_interpretation_plan(
            request_text,
            fallback,
            interpretation,
            today=today,
        )
    plan, semantic_warnings = _apply_canonical_calendar_lookup_target(
        request_text,
        plan,
        manual_plan=manual_plan,
        today=today,
    )
    return CalendarActionResolution(
        plan=plan,
        interpreter_used=True,
        openai_requests=1,
        warnings=tuple((*warnings, *semantic_warnings)),
    )


def calendar_lookup_target_from_plan(
    manual_plan: ManualRequestPlan | dict[str, object] | None,
) -> str:
    """Return the event identity already selected by a typed semantic plan."""

    if manual_plan is None:
        return ""
    if isinstance(manual_plan, dict):
        try:
            plan = ManualRequestPlan.model_validate(manual_plan)
        except ValueError:
            return ""
    else:
        plan = manual_plan
    generic_targets = {"calendar", "google calendar", "my calendar"}
    for candidate in (
        plan.primary_target,
        *plan.required_terms,
        *plan.required_entities,
    ):
        clean = _clean_source_value(str(candidate or ""))
        if clean and _normalize_source_text(clean) not in generic_targets:
            return clean
    return ""


def _apply_canonical_calendar_lookup_target(
    request_text: str,
    plan: CalendarActionPlan | None,
    *,
    manual_plan: ManualRequestPlan | dict[str, object] | None,
    today: date | None,
) -> tuple[CalendarActionPlan | None, tuple[str, ...]]:
    """Keep the semantic plan authoritative over a stale thread event reference."""

    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if (
        not authority.canonical
        or not authority.authorizes_provider(
            "google_calendar",
            allowed_agents={"chief_of_staff"},
            allowed_intents={"context_lookup"},
        )
        or authority.plan is None
        or "read"
        not in authority.effective_provider_operations("google_calendar")
    ):
        return plan, ()
    if authority.plan.provider_read_scope == "bounded_collection":
        # A bounded collection/window is not an event identity. In particular,
        # planner prose such as "today's next event" must never be handed to
        # the exact-title resolver or inherit an unrelated prior event date.
        return plan, ()
    target = calendar_lookup_target_from_plan(manual_plan)
    if not target or plan is None or plan.operation != "read":
        return plan, ()
    identity_blockers = {
        "event name or exact event id",
        "LLM interpretation did not verify the event name",
        "LLM and deterministic event references disagree",
    }
    blockers = tuple(
        blocker for blocker in plan.blockers if blocker not in identity_blockers
    )
    lookup_date = plan.event_reference_date or plan.start_date or calendar_lookup_date(
        request_text,
        today=today,
    )
    changed = not _titles_compatible(plan.event_reference, target)
    return (
        replace(
            plan,
            event_reference=target,
            event_reference_date=lookup_date,
            complete=not blockers,
            blockers=blockers,
        ),
        (
            (
                "Canonical Calendar target replaced a conflicting continuation "
                "reference before the provider read."
            ),
        )
        if changed
        else (),
    )


def _calendar_fallback_for_authorized_operations(
    fallback: CalendarActionPlan | None,
    *,
    allowed_operations: frozenset[str],
) -> CalendarActionPlan | None:
    """Keep prose-derived fields while making the canonical operation authoritative."""

    if fallback is None or fallback.operation in allowed_operations:
        return fallback
    if len(allowed_operations) != 1:
        return _blocked_plan(
            fallback,
            "calendar operation was ambiguous in the canonical plan",
        )
    return replace(fallback, operation=next(iter(allowed_operations)))


def _plan_from_model_interpretation(
    request_text: str,
    interpretation: CalendarActionInterpretation,
    *,
    today: date | None,
) -> tuple[CalendarActionPlan, tuple[str, ...]]:
    """Build a bounded plan when deterministic parsing could not understand the wording."""

    if interpretation.operation == "read" and interpretation.read_scope in {
        "time_window",
        "filtered_window",
    }:
        return _calendar_time_window_plan_from_interpretation(
            request_text,
            interpretation,
            today=today,
        )

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


def _refine_bounded_calendar_read(
    request_text: str,
    interpretation: CalendarActionInterpretation,
) -> CalendarActionInterpretation:
    """Refine a route-level bounded read without inheriting prior semantics."""

    directive = calendar_interpretation_context(request_text).directive_text
    query = _current_calendar_query(interpretation, directive)
    if query:
        return interpretation.model_copy(
            update={
                "read_scope": "filtered_window",
                "query": query,
                "read_selection": _current_calendar_read_selection(
                    interpretation,
                    directive,
                ),
            }
        )
    return interpretation.model_copy(
        update={
            "read_scope": "time_window",
            "read_selection": _current_calendar_read_selection(
                interpretation,
                directive,
            ),
        }
    )


def _current_calendar_query(
    interpretation: CalendarActionInterpretation,
    directive: str,
) -> str:
    """Return only a provider query grounded in the current operator directive."""

    candidates = [(interpretation.query, interpretation.query_source_text)]
    # Compatibility for older interpreter outputs: an exact current event
    # reference can refine a bounded read into a filter. A typed next/first
    # agenda is never a query, even if an older model populated event_reference.
    if interpretation.read_selection != "next":
        candidates.extend(
            (
                (interpretation.event_reference, interpretation.event_reference_source_text),
                (interpretation.title, interpretation.title_source_text),
            )
        )
    for value, evidence in candidates:
        clean = _clean_source_value(value)
        if (
            _calendar_query_is_discriminating(clean)
            and _field_is_current(clean, evidence, directive)
        ):
            return clean
    return ""


def _calendar_query_is_discriminating(value: str) -> bool:
    """Reject generic Calendar-object words that cannot narrow a provider read."""

    tokens = set(re.findall(r"[a-z0-9]+", _normalize_source_text(value)))
    generic_tokens = {
        "all",
        "any",
        "calendar",
        "calendars",
        "event",
        "events",
        "my",
        "our",
        "the",
    }
    return bool(tokens - generic_tokens)


def _current_calendar_scope(
    interpretation: CalendarActionInterpretation,
    directive: str,
) -> str:
    """Broaden Calendar account scope only from explicit current-turn evidence."""

    if interpretation.calendar_scope != "selected_readable":
        return "configured"
    evidence = _normalize_source_text(interpretation.calendar_scope_source_text)
    normalized_directive = _normalize_source_text(directive)
    if not evidence or evidence not in normalized_directive:
        return "configured"
    explicitly_names_calendar_set = bool(
        re.search(
            r"\b(?:all|every|readable|selected|shared)\b[^.!?;]{0,80}"
            r"\bcalendars?\b|\bcalendars?\b[^.!?;]{0,80}"
            r"\b(?:all|every|readable|selected|shared|i\s+can\s+read)\b",
            evidence,
        )
    )
    return "selected_readable" if explicitly_names_calendar_set else "configured"


def _current_calendar_read_selection(
    interpretation: CalendarActionInterpretation,
    directive: str,
) -> str:
    """Discard a prior first/next selection unless the current turn supports it."""

    if interpretation.read_selection != "next":
        return "all"
    return (
        "next"
        if _field_is_current(
            interpretation.read_selection,
            interpretation.read_selection_source_text,
            directive,
        )
        else "all"
    )


def _calendar_time_window_plan_from_interpretation(
    request_text: str,
    interpretation: CalendarActionInterpretation,
    *,
    today: date | None,
) -> tuple[CalendarActionPlan, tuple[str, ...]]:
    """Normalize one model-selected Calendar window into exact provider bounds."""

    directive = calendar_interpretation_context(request_text).directive_text
    reference_date = today or current_calendar_date()
    normalized_directive = _normalize_source_text(directive)
    window_date = ""
    blockers: list[str] = []
    if interpretation.date_scope == "today":
        if re.search(r"\btoday\b", normalized_directive):
            window_date = reference_date.isoformat()
        else:
            blockers.append("calendar window date was not source verified")
    elif interpretation.date_scope == "tomorrow":
        if re.search(r"\btomorrow\b", normalized_directive):
            window_date = (reference_date + timedelta(days=1)).isoformat()
        else:
            blockers.append("calendar window date was not source verified")
    elif interpretation.date_scope == "specific_date":
        parsed_date = calendar_lookup_date(directive, today=reference_date)
        if parsed_date and parsed_date == interpretation.start_date:
            window_date = parsed_date
        else:
            blockers.append("calendar window date was not source verified")
    else:
        blockers.append("calendar window date")

    query = ""
    if interpretation.read_scope == "filtered_window":
        query = _current_calendar_query(interpretation, directive)
        if not query:
            blockers.append("calendar window query was not source verified")
    read_selection = _current_calendar_read_selection(interpretation, directive)
    calendar_scope = _current_calendar_scope(interpretation, directive)
    if (
        calendar_scope == "configured"
        and interpretation.read_scope == "filtered_window"
    ):
        # Preserve the established cross-calendar default for subject-filtered
        # reads. Generic agenda reads remain on the configured calendar unless
        # the current turn explicitly requests a broader Calendar set.
        calendar_scope = "selected_readable"
    warnings = tuple(
        f"Calendar interpretation note: {item}" for item in interpretation.ambiguities
    )
    return (
        CalendarActionPlan(
            operation="read",
            read_scope=interpretation.read_scope,
            read_selection=read_selection,
            date_scope=interpretation.date_scope,
            query=query,
            start_date=window_date,
            calendar_id=(
                os.getenv(GOOGLE_CALENDAR_ID_ENV, DEFAULT_CALENDAR_ID).strip()
                or DEFAULT_CALENDAR_ID
            ),
            calendar_scope=calendar_scope,
            timezone=(
                interpretation.timezone
                or os.getenv(
                    GOOGLE_CALENDAR_TIMEZONE_ENV,
                    DEFAULT_CALENDAR_TIMEZONE,
                ).strip()
                or DEFAULT_CALENDAR_TIMEZONE
            ),
            all_day=False,
            complete=not blockers,
            blockers=tuple(blockers),
        ),
        warnings,
    )


def _validated_interpretation_plan(
    request_text: str,
    fallback: CalendarActionPlan,
    interpretation: CalendarActionInterpretation,
    *,
    today: date | None,
) -> tuple[CalendarActionPlan, tuple[str, ...]]:
    warnings: list[str] = []
    if interpretation.operation == "read" and interpretation.read_scope in {
        "time_window",
        "filtered_window",
    }:
        return _calendar_time_window_plan_from_interpretation(
            request_text,
            interpretation,
            today=today,
        )
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
