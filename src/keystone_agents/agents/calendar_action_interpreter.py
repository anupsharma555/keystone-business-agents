"""Bounded LLM-first interpretation for natural-language Calendar actions."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, Literal

from keystone_agents.authority.semantic import ExecutionIntentAuthority
from keystone_agents.calendar_actions import (
    MONTHS,
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
    CalendarLookupSynthesis,
    CalendarLookupSynthesisInput,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
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


@dataclass(frozen=True)
class CalendarLookupSynthesisResolution:
    """Validated semantic selection over provider-verified Calendar events."""

    synthesis: CalendarLookupSynthesis | None
    openai_requests: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CalendarLookupAnswerResolution:
    """Reader-ready answer over a bounded Calendar provider receipt."""

    text: str
    response_scope: Literal[
        "focused",
        "full_window",
        "full_window_with_focus",
    ]
    status: Literal["matched", "ambiguous", "no_match", "fallback"]
    selected_event_indexes: tuple[int, ...] = ()
    related_event_groups: tuple[tuple[int, ...], ...] = ()
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


def build_calendar_lookup_synthesizer_agent(model: str | None = None) -> Agent:
    """Build a tool-less selector over bounded Calendar event metadata."""

    return build_sdk_agent(
        name="calendar_lookup_synthesizer",
        instructions=compose_instructions(
            "keystone_profile.md",
            "safety_policy.md",
            "calendar_lookup_synthesizer.md",
        ),
        output_type=CalendarLookupSynthesis,
        tools=[],
        model=model,
        model_settings=build_model_settings(
            reasoning_effort="none",
            verbosity="low",
            max_tokens=500,
        ),
        handoff_description=(
            "Select the provider-verified Calendar event that answers one read-only lookup."
        ),
        enforce_tool_policy=False,
    )


def resolve_calendar_lookup_synthesis(
    request_text: str,
    events: list[dict[str, object]],
    *,
    lookup_target: str = "",
    response_scope: str = "focused",
    live: bool = False,
    run_config: Any | None = None,
    model: str | None = None,
) -> CalendarLookupSynthesisResolution:
    """Select relevant events without granting tools or expanding provider scope."""

    bounded_events = [
        {
            "index": index,
            "title": " ".join(str(event.get("title") or "").split())[:240],
            "start_date": str(
                event.get("display_start_date") or event.get("start_date") or ""
            )[:10],
            "start_time": str(
                event.get("display_start_time") or event.get("start_time") or ""
            )[:5],
            "end_date": str(
                event.get("display_end_date") or event.get("end_date") or ""
            )[:10],
            "end_time": str(
                event.get("display_end_time") or event.get("end_time") or ""
            )[:5],
            "all_day": bool(event.get("all_day")),
            "display_timezone": " ".join(
                str(event.get("display_timezone") or "").split()
            )[:100],
            "location": " ".join(str(event.get("location") or "").split())[:500],
            "source_calendar_name": " ".join(
                str(event.get("source_calendar_name") or "").split()
            )[:240],
        }
        for index, event in enumerate(events[:100])
    ]
    if not bounded_events or (not live and run_config is None):
        return CalendarLookupSynthesisResolution(synthesis=None)
    try:
        result = run_typed_sdk_agent(
            agent=build_calendar_lookup_synthesizer_agent(model=model),
            typed_input=CalendarLookupSynthesisInput(
                request_text=request_text,
                lookup_target=lookup_target,
                response_scope=response_scope,
                events=bounded_events,
            ),
            output_type=CalendarLookupSynthesis,
            run_config=run_config,
            live=live,
            workflow_name="Keystone Calendar lookup synthesis",
            tracing_disabled=True,
            max_turns=1,
        )
    except Exception as exc:
        warning_detail = " ".join(str(exc).split())[:240]
        return CalendarLookupSynthesisResolution(
            synthesis=None,
            openai_requests=1 if type(exc).__name__ == "ModelBehaviorError" else 0,
            warnings=(
                f"Calendar lookup synthesis unavailable: {type(exc).__name__}"
                + (f": {warning_detail}" if warning_detail else ""),
            ),
        )
    synthesis = result.output
    valid_indexes = [
        index
        for index in synthesis.selected_event_indexes
        if 0 <= index < len(bounded_events)
    ]
    if valid_indexes != synthesis.selected_event_indexes:
        return CalendarLookupSynthesisResolution(
            synthesis=None,
            openai_requests=1,
            warnings=("Calendar lookup synthesis selected an invalid event index.",),
        )
    if synthesis.status == "matched" and not valid_indexes:
        return CalendarLookupSynthesisResolution(
            synthesis=None,
            openai_requests=1,
            warnings=("Calendar lookup synthesis returned a match without an event.",),
        )
    selected_index_set = set(valid_indexes)
    related_event_groups: list[list[int]] = []
    grouped_indexes: set[int] = set()
    group_warnings: list[str] = []
    for group in synthesis.related_event_groups:
        if (
            len(group) < 2
            or any(index not in selected_index_set for index in group)
            or grouped_indexes.intersection(group)
        ):
            group_warnings.append(
                "Calendar lookup synthesis returned an invalid related-event group; "
                "the event selection was retained without that relationship."
            )
            continue
        related_event_groups.append(group)
        grouped_indexes.update(group)
    if synthesis.status == "no_match" and valid_indexes:
        synthesis = synthesis.model_copy(
            update={
                "selected_event_indexes": [],
                "related_event_groups": [],
            }
        )
    elif related_event_groups != synthesis.related_event_groups:
        synthesis = synthesis.model_copy(
            update={"related_event_groups": related_event_groups}
        )
    return CalendarLookupSynthesisResolution(
        synthesis=synthesis,
        openai_requests=1,
        warnings=tuple(group_warnings),
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
        canonical_read_plan = canonical_calendar_read_plan(
            request_text,
            manual_plan=authority.plan,
            today=today,
        )
        if canonical_read_plan is not None:
            return CalendarActionResolution(plan=canonical_read_plan)

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


def canonical_calendar_read_plan(
    request_text: str,
    *,
    manual_plan: ManualRequestPlan | dict[str, object] | None,
    today: date | None = None,
) -> CalendarActionPlan | None:
    """Reuse a complete canonical read plan without a second model interpretation."""

    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if not authority.canonical or authority.plan is None:
        return None
    plan = authority.plan
    if not (
        authority.authorizes_provider(
            "google_calendar",
            allowed_agents={"chief_of_staff"},
            allowed_intents={"context_lookup"},
        )
        and set(authority.effective_provider_operations("google_calendar")) == {"read"}
        and plan.provider_read_scope == "bounded_collection"
        and plan.provider_result_mode == "items"
        and plan.ask_shape.permission_state == "read_only"
    ):
        return None

    directive = calendar_interpretation_context(request_text).directive_text
    window = _canonical_calendar_window(directive, today=today)
    if window is None:
        return None
    date_scope, start_date = window
    lookup_target = calendar_lookup_target_from_plan(plan)
    query = _canonical_calendar_query(lookup_target)
    explicit_scope = _explicit_calendar_scope(directive)
    calendar_scope = explicit_scope
    if explicit_scope == "configured" and not re.search(
        r"\b(?:primary|configured|default)\s+(?:google\s+)?calendar\b",
        _normalize_source_text(directive),
    ):
        calendar_scope = "all_readable"
    return CalendarActionPlan(
        operation="read",
        read_scope="filtered_window" if query else "time_window",
        read_selection="all",
        date_scope=date_scope,
        query=query,
        start_date=start_date,
        calendar_id=(
            os.getenv(GOOGLE_CALENDAR_ID_ENV, DEFAULT_CALENDAR_ID).strip()
            or DEFAULT_CALENDAR_ID
        ),
        calendar_scope=calendar_scope,
        timezone=(
            os.getenv(
                GOOGLE_CALENDAR_TIMEZONE_ENV,
                DEFAULT_CALENDAR_TIMEZONE,
            ).strip()
            or DEFAULT_CALENDAR_TIMEZONE
        ),
        all_day=False,
        complete=True,
    )


def _canonical_calendar_window(
    directive: str,
    *,
    today: date | None,
) -> tuple[str, str] | None:
    """Resolve one explicit day window without inferring an unstated date."""

    normalized = _normalize_source_text(directive)
    relative_scopes = {
        scope
        for scope, pattern in (
            ("today", r"\btoday\b"),
            ("tomorrow", r"\btomorrow\b"),
        )
        if re.search(pattern, normalized)
    }
    if len(relative_scopes) > 1:
        return None
    reference = today or current_calendar_date()
    if relative_scopes == {"today"}:
        return "today", reference.isoformat()
    if relative_scopes == {"tomorrow"}:
        return "tomorrow", (reference + timedelta(days=1)).isoformat()
    explicit_date = calendar_lookup_date(directive, today=reference)
    return ("specific_date", explicit_date) if explicit_date else None


def _canonical_calendar_query(lookup_target: str) -> str:
    """Remove only generic Calendar/window words from a planner-owned target."""

    clean = _clean_source_value(lookup_target)
    if not clean:
        return ""
    clean = re.sub(r"\b20\d{2}-\d{2}-\d{2}\b", " ", clean)
    clean = re.sub(r"\b\d{1,2}/\d{1,2}/20\d{2}\b", " ", clean)
    month_names = "|".join(MONTHS)
    clean = re.sub(
        rf"\b(?:{month_names})\s+\d{{1,2}}(?:st|nd|rd|th)?"
        rf"(?:,?\s+20\d{{2}})?\b",
        " ",
        clean,
        flags=re.I,
    )
    generic = {
        "a",
        "all",
        "an",
        "any",
        "calendar",
        "calendars",
        "earliest",
        "event",
        "events",
        "first",
        "for",
        "from",
        "google",
        "in",
        "last",
        "latest",
        "my",
        "next",
        "on",
        "our",
        "the",
        "today",
        "tomorrow",
    }
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9'_-]*", clean)
    query = " ".join(token for token in tokens if token.lower() not in generic)
    return query if _calendar_query_is_discriminating(query) else ""


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


def calendar_lookup_response_scope(
    manual_plan: ManualRequestPlan | dict[str, object] | None,
    plan: CalendarActionPlan,
) -> Literal["focused", "full_window", "full_window_with_focus"]:
    """Map the canonical ask shape to one stable Calendar response scope."""

    validated_plan: ManualRequestPlan | None
    if isinstance(manual_plan, dict):
        try:
            validated_plan = ManualRequestPlan.model_validate(manual_plan)
        except ValueError:
            validated_plan = None
    else:
        validated_plan = manual_plan
    ask_shape = getattr(validated_plan, "ask_shape", None)
    ask_breadth = str(getattr(ask_shape, "ask_breadth", "") or "")
    has_lookup_target = bool(calendar_lookup_target_from_plan(validated_plan))
    has_calendar_filter = bool(
        str(getattr(plan, "query", "") or "").strip()
        or str(getattr(plan, "read_scope", "") or "") == "filtered_window"
    )
    if ask_breadth == "narrow":
        return "focused"
    if ask_breadth in {"bounded", "broad"}:
        return "full_window_with_focus" if has_calendar_filter else "full_window"
    return "focused" if (has_lookup_target or has_calendar_filter) else "full_window"


def resolve_calendar_lookup_answer(
    request_text: str,
    events: list[dict[str, object]],
    *,
    lookup_target: str,
    response_scope: Literal[
        "focused",
        "full_window",
        "full_window_with_focus",
    ],
    fallback: str,
    live: bool,
    model: str | None = None,
) -> CalendarLookupAnswerResolution:
    """Select relevant events and render only provider-verified Calendar facts."""

    if not events or response_scope == "full_window":
        return CalendarLookupAnswerResolution(
            text=fallback or "Google Calendar read completed.",
            response_scope=response_scope,
            status="fallback",
        )
    resolution = resolve_calendar_lookup_synthesis(
        request_text,
        events,
        lookup_target=lookup_target,
        response_scope=response_scope,
        live=live,
        model=model,
    )
    synthesis = resolution.synthesis
    if synthesis is None:
        text = (
            fallback
            if response_scope == "full_window_with_focus"
            else "I found Calendar events in the requested window, but I could not "
            "confidently identify one matching event."
        )
        return CalendarLookupAnswerResolution(
            text=text,
            response_scope=response_scope,
            status="fallback",
            openai_requests=resolution.openai_requests,
            warnings=resolution.warnings,
        )

    selected_pairs = [
        (index, events[index])
        for index in synthesis.selected_event_indexes
        if 0 <= index < len(events)
    ]
    selected_indexes = tuple(index for index, _event in selected_pairs)
    selected = [event for _index, event in selected_pairs]
    related_event_groups = tuple(
        tuple(group) for group in synthesis.related_event_groups
    )
    if synthesis.status == "matched" and len(selected) == 1:
        detail = _calendar_event_answer_detail(selected[0])
        location = " ".join(str(selected[0].get("location") or "").split())
        location_text = (
            f" Location: {location}."
            if location
            else " No location is listed in the Calendar event."
        )
        focused = f"I found one matching event: {detail}.{location_text}"
        if response_scope == "full_window_with_focus" and fallback:
            focused = f"{focused}\n\n{fallback}"
        return CalendarLookupAnswerResolution(
            text=focused,
            response_scope=response_scope,
            status="matched",
            selected_event_indexes=selected_indexes,
            related_event_groups=related_event_groups,
            openai_requests=resolution.openai_requests,
            warnings=resolution.warnings,
        )
    if related_event_groups:
        grouped_indexes = set(related_event_groups[0])
        grouped_events = [
            events[index]
            for index in related_event_groups[0]
            if 0 <= index < len(events)
        ]
        lines = [
            (
                "I found one likely event represented by "
                f"{len(grouped_events)} Calendar entries:"
            )
        ]
        lines.extend(
            f"- {_calendar_event_answer_detail(event)}. "
            f"{_calendar_event_location_detail(event)}"
            for event in grouped_events
        )
        conflict_note = _calendar_related_event_conflict_note(grouped_events)
        if conflict_note:
            lines.append(conflict_note)
        remaining_events = [
            event
            for index, event in selected_pairs
            if index not in grouped_indexes
        ]
        if remaining_events:
            lines.append("Other matching Calendar entries:")
            lines.extend(
                f"- {_calendar_event_answer_detail(event)}. "
                f"{_calendar_event_location_detail(event)}"
                for event in remaining_events
            )
        if response_scope == "full_window_with_focus" and fallback:
            lines.extend(["", fallback])
        return CalendarLookupAnswerResolution(
            text="\n".join(lines),
            response_scope=response_scope,
            status=synthesis.status,
            selected_event_indexes=selected_indexes,
            related_event_groups=related_event_groups,
            openai_requests=resolution.openai_requests,
            warnings=resolution.warnings,
        )
    if synthesis.status in {"matched", "ambiguous"} and selected:
        lines = [
            (
                "I found multiple possible matches:"
                if synthesis.status == "ambiguous"
                else f"I found {len(selected)} matching events:"
            )
        ]
        lines.extend(
            f"- {_calendar_event_answer_detail(event)}. "
            f"{_calendar_event_location_detail(event)}"
            for event in selected
        )
        if synthesis.status == "ambiguous":
            lines.append("The Calendar details do not identify one best match.")
        if response_scope == "full_window_with_focus" and fallback:
            lines.extend(["", fallback])
        return CalendarLookupAnswerResolution(
            text="\n".join(lines),
            response_scope=response_scope,
            status=synthesis.status,
            selected_event_indexes=selected_indexes,
            related_event_groups=related_event_groups,
            openai_requests=resolution.openai_requests,
            warnings=resolution.warnings,
        )

    no_match_text = "I could not identify a matching Calendar event."
    if response_scope == "full_window_with_focus" and fallback:
        no_match_text = f"{no_match_text}\n\n{fallback}"
    return CalendarLookupAnswerResolution(
        text=no_match_text,
        response_scope=response_scope,
        status="no_match",
        openai_requests=resolution.openai_requests,
        warnings=resolution.warnings,
    )


def _calendar_event_answer_detail(event: dict[str, object]) -> str:
    """Render one selected event without provider IDs or workflow metadata."""

    title = " ".join(str(event.get("title") or "Untitled event").split())
    source_calendar = " ".join(
        str(event.get("source_calendar_name") or "").split()
    )
    source_suffix = (
        f" on the {source_calendar} calendar"
        if (
            source_calendar
            and "@" not in source_calendar
            and not event.get("source_calendar_primary")
        )
        else ""
    )
    date_text = str(
        event.get("display_start_date") or event.get("start_date") or ""
    ).strip()
    date_suffix = f" on {date_text}" if date_text else ""
    start_time = str(
        event.get("display_start_time") or event.get("start_time") or ""
    ).strip()
    end_time = str(
        event.get("display_end_time") or event.get("end_time") or ""
    ).strip()
    display_timezone = " ".join(
        str(event.get("display_timezone") or "").split()
    )
    time_suffix = ""
    if start_time:
        try:
            start_display = datetime.strptime(start_time[:5], "%H:%M").strftime(
                "%-I:%M %p"
            )
        except ValueError:
            start_display = start_time
        if end_time:
            try:
                end_display = datetime.strptime(end_time[:5], "%H:%M").strftime(
                    "%-I:%M %p"
                )
            except ValueError:
                end_display = end_time
            time_suffix = f", from {start_display} to {end_display}"
        else:
            time_suffix = f", at {start_display}"
        if display_timezone:
            time_suffix += f" ({display_timezone})"
    return f'"{title}"{source_suffix}{date_suffix}{time_suffix}'


def _calendar_event_location_detail(event: dict[str, object]) -> str:
    """Render location availability for one selected event."""

    location = " ".join(str(event.get("location") or "").split())
    return f"Location: {location}." if location else "No location is listed."


def _calendar_related_event_conflict_note(
    events: list[dict[str, object]],
) -> str:
    """Describe factual conflicts across records likely representing one event."""

    time_ranges = {
        (
            str(
                event.get("display_start_date")
                or event.get("start_date")
                or ""
            ),
            str(
                event.get("display_start_time") or event.get("start_time") or ""
            ),
            str(event.get("display_end_date") or event.get("end_date") or ""),
            str(event.get("display_end_time") or event.get("end_time") or ""),
        )
        for event in events
    }
    locations = {
        " ".join(str(event.get("location") or "").split())
        for event in events
        if " ".join(str(event.get("location") or "").split())
    }
    conflicts: list[str] = []
    if len(time_ranges) > 1:
        conflicts.append("times")
    if len(locations) > 1:
        conflicts.append("locations")
    if not conflicts:
        return ""
    conflict_text = " and ".join(conflicts)
    return (
        f"The Calendar entries list different {conflict_text}, so confirm the "
        "intended appointment details."
    )


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
    """Use every readable calendar for reads unless the operator narrows scope."""

    explicit_scope = _explicit_calendar_scope(directive)
    if explicit_scope != "configured":
        return explicit_scope
    if re.search(
        r"\b(?:primary|configured|default)\s+(?:google\s+)?calendar\b",
        _normalize_source_text(directive),
    ):
        return "configured"
    if interpretation.calendar_scope not in {"selected_readable", "all_readable"}:
        return "all_readable"
    evidence = _normalize_source_text(interpretation.calendar_scope_source_text)
    normalized_directive = _normalize_source_text(directive)
    if not evidence or evidence not in normalized_directive:
        return "all_readable"
    selected_scope = _explicit_calendar_scope(evidence)
    return selected_scope if selected_scope != "configured" else "all_readable"


def _explicit_calendar_scope(directive: str) -> str:
    """Return only a Calendar account scope explicitly named by the operator."""

    evidence = _normalize_source_text(directive)
    if not evidence:
        return "configured"
    all_readable = bool(
        re.search(
            r"\b(?:all|every)\s+(?:(?:of\s+)?(?:my|the)\s+)?"
            r"(?:(?:available|accessible|readable)\s+)?"
            r"(?:google\s+)?calendars?\b"
            r"(?:\s+(?:that\s+)?i\s+can\s+read\b)?"
            r"|\b(?:all|every)\s+(?:google\s+)?calendars?\s+"
            r"(?:available|accessible|readable)\b",
            evidence,
        )
    )
    if all_readable:
        return "all_readable"
    selected_or_shared = bool(
        re.search(
            r"\b(?:selected|shared)\b[^.!?;]{0,80}"
            r"\bcalendars?\b|\bcalendars?\b[^.!?;]{0,80}"
            r"\b(?:selected|shared)\b",
            evidence,
        )
    )
    return "selected_readable" if selected_or_shared else "configured"


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
