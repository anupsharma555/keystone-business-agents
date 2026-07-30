"""Bounded LLM-first interpretation for natural-language Calendar actions."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
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
    calendar_times_from_evidence,
    current_calendar_date,
    default_calendar_end_time,
    infer_calendar_action_plan,
    is_calendar_action_candidate,
    is_contextual_calendar_event_reference,
)
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.calendar_action import (
    CalendarActionInterpretation,
    CalendarActionInterpretationInput,
    CalendarLookupSynthesis,
    CalendarLookupSynthesisInput,
)
from keystone_agents.schemas.execution_request import ContinuationObjectReference
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
    verified_objects: Sequence[ContinuationObjectReference] = (),
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
        executable_operations = frozenset(
            operation
            for operation in authority.effective_provider_operations(
                "google_calendar"
            )
            if operation in {"read", "create", "update", "delete"}
        )
        primary_operation = _canonical_calendar_primary_operation(authority)
        allowed_operations = (
            frozenset({primary_operation}) if primary_operation else executable_operations
        )
        canonical_read_scope = authority.plan.provider_read_scope
        if not allowed_operations:
            return CalendarActionResolution(
                plan=None,
                warnings=(
                    "Canonical plan did not specify an executable Calendar operation.",
                ),
            )
        if primary_operation is None and len(
            executable_operations & {"create", "update", "delete"}
        ) > 1:
            return CalendarActionResolution(
                plan=None,
                warnings=(
                    "Canonical plan requested a multi-stage Calendar mutation; "
                    "the single-action executor did not choose one stage.",
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
    interpretation_context = calendar_interpretation_context(
        request_text,
        verified_objects=verified_objects,
    )
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
    semantic_update_supplied = _semantic_update_is_source_grounded(
        request_text,
        interpretation,
        fallback=fallback,
        verified_objects=verified_objects,
        today=today,
    )
    if allowed_operations is not None:
        if interpretation.operation not in allowed_operations:
            specialist_operation_is_authorized = bool(
                authority.canonical
                and authority.plan is not None
                and authority.plan.intent == "business_system_write"
                and authority.plan.ask_shape.permission_state != "read_only"
                and interpretation.operation in {"create", "update", "delete"}
                and _operation_source_anchored(request_text, interpretation)
            )
            if not specialist_operation_is_authorized:
                proposed_operation = interpretation.operation
                authorized = ", ".join(sorted(allowed_operations))
                return CalendarActionResolution(
                    plan=(
                        _blocked_plan(
                            fallback,
                            "Calendar planner and interpreter operations disagreed",
                        )
                        if fallback is not None
                        else None
                    ),
                    interpreter_used=True,
                    openai_requests=1,
                    warnings=(
                        "Calendar planner authorized "
                        f"{authorized}; Calendar interpreter proposed "
                        f"{proposed_operation}. No operation was selected or executed.",
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
            verified_objects=verified_objects,
        )
    else:
        plan, warnings = _validated_interpretation_plan(
            request_text,
            fallback,
            interpretation,
            today=today,
            verified_objects=verified_objects,
        )
    if fallback is not None and plan is not None:
        warnings = tuple(
            dict.fromkeys(
                (
                    *warnings,
                    *_calendar_parser_hint_disagreement_warnings(
                        fallback,
                        plan,
                    ),
                )
            )
        )
    plan, semantic_warnings = _apply_canonical_calendar_lookup_target(
        request_text,
        plan,
        manual_plan=manual_plan,
        today=today,
    )
    plan = _apply_canonical_calendar_mutation_scope(
        request_text,
        plan,
        manual_plan=manual_plan,
    )
    plan = _finalize_calendar_semantic_plan(
        plan,
        semantic_update_supplied=semantic_update_supplied,
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
    """Reuse a complete canonical read plan without a second model interpretation.

    This deliberately handles only a read-only, bounded collection whose
    semantic planner output already specifies an item result and read-only
    permission. Ambiguous windows or weaker plans continue through the Calendar
    interpreter.
    """

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

    request_context = calendar_interpretation_context(request_text)
    directive = request_context.directive_text
    if re.search(
        r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b",
        directive,
        re.I,
    ):
        # Clock-bounded reads need the Calendar interpreter to preserve whether
        # the time is an exact target, lower/upper bound, or bounded range.
        return None
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


def canonical_calendar_mutation_plan(
    request_text: str,
    *,
    manual_plan: ManualRequestPlan | dict[str, object] | None,
    today: date | None = None,
) -> CalendarActionPlan | None:
    """Reuse one exact create/delete after planner and current fields agree.

    The Orchestrator remains the semantic authority. This helper only avoids a
    redundant Calendar interpretation turn when the current directive itself
    supplies complete fields and the canonical plan independently authorizes
    the same provider, owner, operation, and target. The deterministic parser
    extracts fields; it does not select the operation. Natural updates and
    contextual references retain the Calendar interpreter.
    """

    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if not authority.canonical or authority.plan is None:
        return None
    manual = authority.plan
    if not (
        authority.authorizes_provider(
            "google_calendar",
            allowed_agents={"chief_of_staff"},
            allowed_intents={"business_system_write"},
        )
    ):
        return None
    operation = _canonical_calendar_primary_operation(authority)
    if operation not in {"create", "delete"}:
        return None

    request_context = calendar_interpretation_context(request_text)
    directive = request_context.directive_text
    parsed = infer_calendar_action_plan(directive, today=today)
    if parsed is None:
        return None
    parsed = _calendar_fallback_for_authorized_operations(
        parsed,
        allowed_operations=frozenset({operation}),
    )
    if (
        parsed is None
        or parsed.operation != operation
        or not parsed.complete
        or parsed.blockers
        or parsed.event_id
    ):
        return None
    event_reference = _clean_source_value(
        parsed.title if operation == "create" else parsed.event_reference
    )
    planner_target = calendar_lookup_target_from_plan(manual)
    normalized_reference = _normalize_source_text(event_reference)
    if (
        not event_reference
        or normalized_reference not in _normalize_source_text(directive)
        or normalized_reference != _normalize_source_text(planner_target)
    ):
        return None
    if operation == "delete" and (
        is_contextual_calendar_event_reference(event_reference)
        or normalized_reference != _normalize_source_text(parsed.title)
    ):
        return None

    target_count = manual.desired_count if manual.desired_count_explicit else 1
    return replace(
        parsed,
        title=event_reference,
        description=(
            request_context.description_payload
            if operation == "create" and request_context.description_payload
            else parsed.description
        ),
        event_reference=event_reference if operation == "delete" else "",
        target_count=target_count,
        calendar_scope=(
            "all_readable" if operation == "delete" else parsed.calendar_scope
        ),
        complete=True,
        blockers=(),
    )


def _canonical_calendar_primary_operation(
    authority: ExecutionIntentAuthority,
) -> str | None:
    """Resolve one typed Calendar action without interpreting request prose.

    Read/search/verify/attach steps are supporting operations. A planner may
    also represent fields applied during creation as a same-object ``update``;
    that remains one create action. Destructive or otherwise distinct mutation
    sequences are not collapsed and retain the Calendar interpreter.
    """

    mutations = [
        operation
        for operation in authority.effective_provider_operations("google_calendar")
        if operation in {"create", "update", "delete"}
    ]
    provider_steps = authority.provider_action_steps("google_calendar")
    step_mutations = [
        step.operation
        for step in provider_steps
        if step.operation in {"create", "update", "delete"}
        and step.resource_type in {"calendar_event", "unspecified"}
    ]
    ordered = list(dict.fromkeys(step_mutations or mutations))
    if ordered and ordered[0] == "create" and set(ordered) <= {"create", "update"}:
        return "create"
    if len(ordered) == 1:
        return ordered[0]
    if not ordered and "read" in authority.effective_provider_operations(
        "google_calendar"
    ):
        return "read"
    return None


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


def _apply_canonical_calendar_mutation_scope(
    request_text: str,
    plan: CalendarActionPlan | None,
    *,
    manual_plan: ManualRequestPlan | dict[str, object] | None,
) -> CalendarActionPlan | None:
    """Apply the planner-owned exact delete target and count before execution.

    Exact provider IDs from verified continuation objects remain authoritative.
    For older Slack receipts that predate typed run provenance, a source-visible
    canonical target may replace a stale root-thread title; the provider still
    has to resolve that title/date scope uniquely before deleting anything.
    """

    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if (
        plan is None
        or plan.operation != "delete"
        or not authority.canonical
        or authority.plan is None
        or not authority.authorizes_provider(
            "google_calendar",
            allowed_agents={"chief_of_staff"},
            allowed_intents={"business_system_write"},
        )
        or "delete"
        not in authority.effective_provider_operations("google_calendar")
    ):
        return plan
    manual = authority.plan
    target_count = (
        manual.desired_count
        if manual.desired_count_explicit
        else 1
    )
    event_reference = plan.event_reference
    if not plan.event_id:
        canonical_target = calendar_lookup_target_from_plan(manual)
        interpretation_context = calendar_interpretation_context(request_text)
        source_text = " ".join(
            (
                interpretation_context.directive_text,
                interpretation_context.thread_context,
            )
        )
        if (
            canonical_target
            and not is_contextual_calendar_event_reference(canonical_target)
            and _normalize_source_text(canonical_target)
            in _normalize_source_text(source_text)
        ):
            event_reference = canonical_target
    return replace(
        plan,
        event_reference=event_reference,
        target_count=target_count,
        calendar_scope="all_readable",
    )


def _calendar_fallback_for_authorized_operations(
    fallback: CalendarActionPlan | None,
    *,
    allowed_operations: frozenset[str],
) -> CalendarActionPlan | None:
    """Keep parser hints while making the semantic operation authoritative."""

    if fallback is None or fallback.operation in allowed_operations:
        return fallback
    if len(allowed_operations) != 1:
        return _blocked_plan(
            fallback,
            "calendar operation was ambiguous in the canonical plan",
        )
    operation = next(iter(allowed_operations))
    return _recompute_calendar_required_field_blockers(
        replace(fallback, operation=operation)
    )


def _plan_from_model_interpretation(
    request_text: str,
    interpretation: CalendarActionInterpretation,
    *,
    today: date | None,
    verified_objects: Sequence[ContinuationObjectReference] = (),
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

    request_context = calendar_interpretation_context(
        request_text,
        verified_objects=verified_objects,
    )
    current_request = request_context.directive_text or request_text
    context = calendar_thread_event_context(
        request_text,
        today=today,
        verified_objects=verified_objects,
    )
    operation = interpretation.operation
    title = (
        _clean_source_value(interpretation.title)
        if (
            _source_anchored(
                interpretation.title,
                interpretation.title_source_text,
                current_request,
            )
            or _source_anchored(
                interpretation.title,
                interpretation.title_source_text,
                request_context.event_fact_text,
            )
        )
        else ""
    )
    start_date = (
        interpretation.start_date
        if _interpreted_field_is_source_verified(
            interpretation.start_date,
            interpretation.date_source_text,
            field="start_date",
            operation=operation,
            current_request=current_request,
            event_fact_text=request_context.event_fact_text,
            today=today,
        )
        else ""
    )
    start_time = (
        interpretation.start_time
        if _interpreted_field_is_source_verified(
            interpretation.start_time,
            interpretation.time_source_text,
            field="start_time",
            operation=operation,
            current_request=current_request,
            event_fact_text=request_context.event_fact_text,
            today=today,
        )
        else ""
    )
    end_date_evidence = (
        interpretation.end_date_source_text or interpretation.date_source_text
    )
    end_date = (
        interpretation.end_date
        if _interpreted_field_is_source_verified(
            interpretation.end_date,
            end_date_evidence,
            field="end_date",
            operation=operation,
            current_request=current_request,
            event_fact_text=request_context.event_fact_text,
            today=today,
        )
        else ""
    )
    end_time = (
        interpretation.end_time
        if start_time
        and _interpreted_field_is_source_verified(
            interpretation.end_time,
            interpretation.time_source_text,
            field="end_time",
            operation=operation,
            current_request=current_request,
            event_fact_text=request_context.event_fact_text,
            today=today,
        )
        else ""
    )
    if start_time and not end_time:
        end_time = default_calendar_end_time(start_time)
    description = _resolved_interpreted_description(
        request_context.description_payload,
        interpretation,
        request_text,
    )
    interpreted_event_id = (
        interpretation.event_id
        if _field_is_current(
            interpretation.event_id,
            interpretation.event_id_source_text,
            current_request,
        )
        else ""
    )
    interpreted_reference = _clean_source_value(interpretation.event_reference)
    model_selected_verified_thread_identity = bool(
        interpretation.event_reference_from_thread_context
        and context.get("source") == "verified_prior_result"
        and context.get("event_reference")
    )
    if model_selected_verified_thread_identity:
        interpreted_reference = ""
    contextual_reference = is_contextual_calendar_event_reference(
        interpreted_reference
    )
    current_explicit_reference = bool(
        interpreted_reference
        and not contextual_reference
        and _field_is_current(
            interpreted_reference,
            interpretation.event_reference_source_text,
            current_request,
        )
    )
    deictic_current_request = _calendar_deictic_event_reference_requested(
        current_request
    )
    thread_grounded_reference = bool(
        interpreted_reference
        and not contextual_reference
        and deictic_current_request
        and _source_anchored(
            interpreted_reference,
            interpretation.event_reference_source_text,
            request_context.thread_context,
        )
    )
    if contextual_reference or not (
        current_explicit_reference or thread_grounded_reference
    ):
        interpreted_reference = ""
    if (
        not interpreted_reference
        and operation in {"read", "update", "delete"}
        and title
    ):
        # Existing-event asks often populate the schema's current ``title``
        # field rather than its separate lookup-reference field. A title that
        # was already proven to come from the current directive is the current
        # object identity; do not replace it with an unrelated prior event.
        interpreted_reference = title
        current_explicit_reference = True
    if context.get("source") == "verified_prior_deletion":
        event_reference = (
            interpreted_reference if current_explicit_reference else ""
        )
    elif (
        context.get("source") in {"verified_prior_result", "prior_request"}
        and not interpreted_reference
    ):
        event_reference = context["event_reference"]
    else:
        event_reference = interpreted_reference or context["event_reference"]
    use_prior_identity = bool(
        (
            context.get("source") in {"verified_prior_result", "prior_request"}
            and event_reference == context["event_reference"]
            and not interpreted_reference
        )
        or thread_grounded_reference
        or model_selected_verified_thread_identity
    )
    event_id = (
        interpreted_event_id
        or (context["event_id"] if use_prior_identity else "")
    )
    event_reference_date = (
        context["event_reference_date"]
        if use_prior_identity
        else (
            interpretation.event_reference_date
            if _field_is_current_or_thread_context(
                interpretation.event_reference_date,
                interpretation.event_reference_date_source_text,
                current_request=current_request,
                thread_context=request_context.thread_context,
            )
            else start_date
            if operation in {"delete", "read"}
            else ""
        )
    )
    event_reference_time = (
        interpretation.event_reference_time
        if _field_is_current_or_thread_context(
            interpretation.event_reference_time,
            interpretation.event_reference_time_source_text,
            current_request=current_request,
            thread_context=request_context.thread_context,
        )
        else ""
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
    plan = _recompute_calendar_required_field_blockers(CalendarActionPlan(
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
        event_reference_date=event_reference_date,
        event_reference_time=event_reference_time,
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
    ))
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


def _field_is_current_or_thread_context(
    value: str,
    evidence: str,
    *,
    current_request: str,
    thread_context: str,
) -> bool:
    """Accept existing-object identity only from current or bounded thread evidence."""

    return _field_is_current(value, evidence, current_request) or _source_anchored(
        value,
        evidence,
        thread_context,
    )


def _field_is_current_or_event_fact(
    value: str,
    evidence: str,
    *,
    current_request: str,
    event_fact_text: str,
) -> bool:
    """Accept action fields from the directive or bounded current-request facts."""

    return _field_is_current(value, evidence, current_request) or _field_is_current(
        value,
        evidence,
        event_fact_text,
    )


def _interpreted_field_is_source_verified(
    value: str,
    evidence: str,
    *,
    field: str,
    operation: str,
    current_request: str,
    event_fact_text: str = "",
    today: date | None,
) -> bool:
    """Validate a model field against its cited operator evidence.

    The broad parser may offer hints, but it is not a competing action
    authority. This narrow verifier checks only whether the model's own cited
    span resolves to the proposed typed value.
    """

    if not value or not evidence:
        return False
    validation_operation = operation if operation in {"create", "update", "delete"} else "create"
    return any(
        source
        and _field_matches_deterministic_parse(
            source,
            evidence=evidence,
            expected=value,
            field=field,
            operation=validation_operation,
            today=today,
        )
        for source in (current_request, event_fact_text)
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
    start_time = (
        interpretation.start_time
        if _field_is_current(
            interpretation.start_time,
            interpretation.time_source_text,
            directive,
        )
        else ""
    )
    end_time = (
        interpretation.end_time
        if start_time
        and _field_is_current(
            interpretation.end_time,
            interpretation.time_source_text,
            directive,
        )
        else ""
    )
    if bool(interpretation.start_time) != bool(start_time):
        blockers.append("calendar window start time was not source verified")
    if bool(interpretation.end_time) != bool(end_time):
        blockers.append("calendar window end time was not source verified")
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
            start_time=start_time,
            end_time=end_time,
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
    verified_objects: Sequence[ContinuationObjectReference] = (),
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
            verified_objects=verified_objects,
        )
    if interpretation.operation == "create":
        return _plan_from_model_interpretation(
            request_text,
            interpretation,
            today=today,
            verified_objects=verified_objects,
        )
    if fallback.complete:
        return _validated_complete_deterministic_plan(
            request_text,
            fallback,
            interpretation,
            today=today,
            verified_objects=verified_objects,
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
            verified_objects=verified_objects,
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

    request_context = calendar_interpretation_context(
        request_text,
        verified_objects=verified_objects,
    )
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
    *,
    today: date | None,
    verified_objects: Sequence[ContinuationObjectReference] = (),
) -> tuple[CalendarActionPlan, tuple[str, ...]]:
    """Keep verified Python fields and merge source-anchored model-only fields."""

    if fallback.operation in {"read", "update", "delete"}:
        return _validated_existing_event_plan(
            request_text,
            fallback,
            interpretation,
            today=today,
            verified_objects=verified_objects,
        )

    semantic_plan, semantic_warnings = _plan_from_model_interpretation(
        request_text,
        interpretation,
        today=today,
        verified_objects=verified_objects,
    )
    parser_warnings = _calendar_parser_hint_disagreement_warnings(
        fallback,
        semantic_plan,
    )
    return (
        replace(
            semantic_plan,
            calendar_id=fallback.calendar_id,
            calendar_scope=fallback.calendar_scope,
        ),
        tuple((*semantic_warnings, *parser_warnings)),
    )


def _calendar_parser_hint_disagreement_warnings(
    fallback: CalendarActionPlan,
    semantic_plan: CalendarActionPlan,
) -> tuple[str, ...]:
    """Report parser differences without granting them blocker authority."""

    warnings: list[str] = []
    for label, parser_value, semantic_value, title_like in (
        ("title", fallback.title, semantic_plan.title, True),
        (
            "event reference",
            fallback.event_reference,
            semantic_plan.event_reference,
            True,
        ),
        ("start date", fallback.start_date, semantic_plan.start_date, False),
        ("end date", fallback.end_date, semantic_plan.end_date, False),
        ("start time", fallback.start_time, semantic_plan.start_time, False),
        ("end time", fallback.end_time, semantic_plan.end_time, False),
    ):
        if not parser_value or not semantic_value:
            continue
        agrees = (
            _titles_compatible(parser_value, semantic_value)
            if title_like
            else parser_value.lower() == semantic_value.lower()
        )
        if not agrees:
            warnings.append(
                f"Calendar parser hint for {label} differed; "
                "the source-grounded semantic value was used."
            )
    return tuple(warnings)


def _blocked_plan(fallback: CalendarActionPlan, blocker: str) -> CalendarActionPlan:
    blockers = tuple(dict.fromkeys((*fallback.blockers, blocker)))
    return replace(fallback, complete=False, blockers=blockers)


_CALENDAR_REQUIRED_FIELD_BLOCKERS = frozenset(
    {
        "event title",
        "event date",
        "event name or exact event id",
        "updated title, date, or note",
        "updated title, date, time, or note",
    }
)


def _finalize_calendar_semantic_plan(
    plan: CalendarActionPlan | None,
    *,
    semantic_update_supplied: bool | None = None,
) -> CalendarActionPlan | None:
    """Apply required-field validation once, after every semantic merge.

    Parser output may contribute hints, but it cannot retain an earlier
    missing-field verdict after the source-grounded interpretation and
    verified-object binding have produced the final typed mutation.
    """

    if plan is None or plan.operation not in {"create", "update", "delete"}:
        return plan
    return _recompute_calendar_required_field_blockers(
        plan,
        semantic_update_supplied=semantic_update_supplied,
    )


def _semantic_update_is_source_grounded(
    request_text: str,
    interpretation: CalendarActionInterpretation,
    *,
    fallback: CalendarActionPlan | None = None,
    verified_objects: Sequence[ContinuationObjectReference] = (),
    today: date | None = None,
) -> bool:
    """Return whether the model supplied a change grounded in the current turn."""

    if interpretation.operation != "update":
        return False
    request_context = calendar_interpretation_context(
        request_text,
        verified_objects=verified_objects,
    )
    current_request = request_context.directive_text or request_text
    current_or_fact = {
        "current_request": current_request,
        "event_fact_text": request_context.event_fact_text,
        "today": today,
    }
    return any(
        (
            _title_source_anchored(
                interpretation.title,
                interpretation.title_source_text,
                current_request,
            ),
            _interpreted_field_is_source_verified(
                interpretation.start_date,
                interpretation.date_source_text,
                field="start_date",
                operation="update",
                **current_or_fact,
            ),
            _interpreted_field_is_source_verified(
                interpretation.end_date,
                interpretation.end_date_source_text
                or interpretation.date_source_text,
                field="end_date",
                operation="update",
                **current_or_fact,
            ),
            _interpreted_field_is_source_verified(
                interpretation.start_time,
                interpretation.time_source_text,
                field="start_time",
                operation="update",
                **current_or_fact,
            ),
            _interpreted_field_is_source_verified(
                interpretation.end_time,
                interpretation.time_source_text,
                field="end_time",
                operation="update",
                **current_or_fact,
            ),
            bool(
                interpretation.description
                and (
                    (
                        request_context.description_payload
                        and _normalize_source_text(interpretation.description)
                        == _normalize_source_text(
                            request_context.description_payload
                        )
                    )
                    or _source_anchored(
                        interpretation.description,
                        interpretation.description_source_text,
                        current_request,
                    )
                    or _source_anchored(
                        interpretation.description,
                        interpretation.description_source_text,
                        request_text,
                    )
                )
            ),
            bool(
                interpretation.all_day is not None
                and re.search(r"\ball[\s-]?day\b", current_request, re.I)
            ),
            bool(
                fallback
                and fallback.complete
                and fallback.operation == "update"
                and any(
                    (
                        interpretation.title
                        and interpretation.title == fallback.title,
                        interpretation.start_date
                        and interpretation.start_date == fallback.start_date,
                        interpretation.end_date
                        and interpretation.end_date == fallback.end_date,
                        interpretation.start_time
                        and interpretation.start_time == fallback.start_time,
                        interpretation.end_time
                        and interpretation.end_time == fallback.end_time,
                        interpretation.description
                        and interpretation.description == fallback.description,
                        interpretation.all_day is not None
                        and interpretation.all_day == fallback.all_day,
                    )
                )
            ),
        )
    )


def _recompute_calendar_required_field_blockers(
    plan: CalendarActionPlan,
    *,
    semantic_update_supplied: bool | None = None,
) -> CalendarActionPlan:
    """Recompute provider-required fields from the final semantic action.

    Deterministic extraction is advisory. Its earlier missing-field guesses
    must not survive after a source-grounded model interpretation supplies the
    typed action fields. Safety, approval, identity, and source-verification
    blockers are retained.
    """

    blockers = [
        blocker
        for blocker in plan.blockers
        if blocker not in _CALENDAR_REQUIRED_FIELD_BLOCKERS
    ]
    if plan.operation == "create":
        if not plan.title:
            blockers.append("event title")
        if not plan.start_date:
            blockers.append("event date")
    elif plan.operation in {"read", "update", "delete"}:
        if not plan.event_id and not plan.event_reference:
            blockers.append("event name or exact event id")
        update_supplied = (
            semantic_update_supplied
            if semantic_update_supplied is not None
            else any((plan.title, plan.start_date, plan.start_time, plan.description))
        )
        if plan.operation == "update" and not update_supplied:
            blockers.append("updated title, date, time, or note")
    unique_blockers = tuple(dict.fromkeys(blockers))
    return replace(
        plan,
        complete=not unique_blockers,
        blockers=unique_blockers,
    )


def _shifted_end_time(
    new_start_time: str,
    prior_start_time: str,
    prior_end_time: str,
) -> str:
    """Shift a verified event while preserving its prior duration."""

    try:
        new_start = datetime.strptime(new_start_time, "%H:%M")
        prior_start = datetime.strptime(prior_start_time, "%H:%M")
        prior_end = datetime.strptime(prior_end_time, "%H:%M")
    except ValueError:
        return ""
    if prior_end <= prior_start:
        prior_end += timedelta(days=1)
    duration = prior_end - prior_start
    if duration <= timedelta(0) or duration > timedelta(days=1):
        return ""
    return (new_start + duration).strftime("%H:%M")


def _validated_existing_event_plan(
    request_text: str,
    fallback: CalendarActionPlan,
    interpretation: CalendarActionInterpretation,
    *,
    today: date | None,
    verified_objects: Sequence[ContinuationObjectReference] = (),
) -> tuple[CalendarActionPlan, tuple[str, ...]]:
    warnings: list[str] = []
    request_context = calendar_interpretation_context(
        request_text,
        verified_objects=verified_objects,
    )
    current_request = request_context.directive_text or request_text
    thread_event = calendar_thread_event_context(
        request_text,
        today=today,
        verified_objects=verified_objects,
    )
    interpreted_reference = _clean_source_value(interpretation.event_reference)
    contextual_interpretation = is_contextual_calendar_event_reference(
        interpreted_reference
    )
    current_explicit_reference = bool(
        interpreted_reference
        and not contextual_interpretation
        and not interpretation.event_reference_from_thread_context
        and _source_anchored(
            interpreted_reference,
            interpretation.event_reference_source_text,
            current_request,
        )
    )
    verified_thread_event_id = (
        str(thread_event.get("event_id") or "")
        if thread_event.get("source") == "verified_prior_result"
        else ""
    )
    verified_thread_event_reference = (
        str(thread_event.get("event_reference") or "")
        if thread_event.get("source") == "verified_prior_result"
        else ""
    )
    explicit_reference_matches_verified_object = bool(
        current_explicit_reference
        and verified_thread_event_reference
        and _titles_compatible(
            interpreted_reference,
            verified_thread_event_reference,
        )
    )
    use_verified_thread_id = bool(
        verified_thread_event_id
        and (
            interpretation.event_reference_from_thread_context
            or contextual_interpretation
            or explicit_reference_matches_verified_object
            or _calendar_deictic_event_reference_requested(current_request)
        )
    )
    stale_verified_thread_id = bool(
        verified_thread_event_id
        and current_explicit_reference
        and not explicit_reference_matches_verified_object
    )
    event_id = (
        verified_thread_event_id
        if use_verified_thread_id
        else ""
        if stale_verified_thread_id
        else fallback.event_id
    )
    event_reference = (
        str(thread_event.get("event_reference") or "")
        if use_verified_thread_id
        else fallback.event_reference
    )
    if event_id:
        if (
            not use_verified_thread_id
            and (
                not interpretation.event_id
                or interpretation.event_id.lower() != event_id.lower()
                or not _field_is_current(
                    interpretation.event_id,
                    interpretation.event_id_source_text,
                    current_request,
                )
            )
        ):
            return (
                _blocked_plan(fallback, "LLM interpretation did not verify the event id"),
                ("Calendar event id interpretations disagreed.",),
            )
    else:
        verified_thread_reference = bool(
            verified_thread_event_reference
            and event_reference
            and _titles_compatible(
                event_reference,
                verified_thread_event_reference,
            )
        )
        model_selected_verified_thread_identity = bool(
            interpretation.event_reference_from_thread_context
            and verified_thread_event_reference
        )
        if (
            model_selected_verified_thread_identity
            or (
                verified_thread_reference
                and (not interpreted_reference or contextual_interpretation)
            )
        ):
            interpreted_reference = verified_thread_event_reference
            event_reference = verified_thread_event_reference
            verified_thread_reference = True
        source_anchored = _source_anchored(
            interpreted_reference,
            interpretation.event_reference_source_text,
            current_request,
        )
        deterministic_reference_agrees = bool(
            event_reference
            and interpreted_reference
            and _titles_compatible(event_reference, interpreted_reference)
        )
        if not interpreted_reference or not (
            source_anchored
            or verified_thread_reference
            or deterministic_reference_agrees
        ):
            return (
                _blocked_plan(fallback, "LLM interpretation did not verify the event name"),
                ("Calendar event reference was not anchored to the operator request.",),
            )
        if (
            event_reference
            and not _titles_compatible(event_reference, interpreted_reference)
            and source_anchored
        ):
            warnings.append(
                "Calendar parser hint for event reference differed; "
                "the source-grounded semantic value was used."
            )
        event_reference = interpreted_reference

    title = (
        _clean_source_value(interpretation.title)
        if _source_anchored(
            interpretation.title,
            interpretation.title_source_text,
            current_request,
        )
        else ""
    )
    start_date = (
        interpretation.start_date
        if (
            _interpreted_field_is_source_verified(
                interpretation.start_date,
                interpretation.date_source_text,
                field="start_date",
                operation=interpretation.operation,
                current_request=current_request,
                event_fact_text=request_context.event_fact_text,
                today=today,
            )
            or bool(
                fallback.complete
                and fallback.start_date
                and interpretation.start_date == fallback.start_date
            )
        )
        else ""
    )
    start_time = (
        interpretation.start_time
        if (
            _interpreted_field_is_source_verified(
                interpretation.start_time,
                interpretation.time_source_text,
                field="start_time",
                operation=interpretation.operation,
                current_request=current_request,
                event_fact_text=request_context.event_fact_text,
                today=today,
            )
            or bool(
                fallback.complete
                and fallback.start_time
                and interpretation.start_time == fallback.start_time
            )
        )
        else ""
    )
    end_time = ""
    if start_time:
        explicit_end_time = bool(
            _interpreted_field_is_source_verified(
                interpretation.end_time,
                interpretation.time_source_text,
                field="end_time",
                operation=interpretation.operation,
                current_request=current_request,
                event_fact_text=request_context.event_fact_text,
                today=today,
            )
        )
        end_matches_complete_hint = bool(
            fallback.complete
            and interpretation.end_time
            and interpretation.end_time == fallback.end_time
        )
        if explicit_end_time:
            end_time = interpretation.end_time
        elif end_matches_complete_hint:
            end_time = fallback.end_time
        elif (
            fallback.complete
            and start_time == fallback.start_time
            and fallback.end_time
        ):
            end_time = fallback.end_time
        if not end_time and thread_event.get("source") == "verified_prior_result":
            end_time = _shifted_end_time(
                start_time,
                thread_event.get("event_start_time", ""),
                thread_event.get("event_end_time", ""),
            )
        if not end_time and thread_event.get("source") != "verified_prior_result":
            end_time = default_calendar_end_time(start_time)
        if (
            interpretation.end_time
            and not explicit_end_time
            and not end_matches_complete_hint
        ):
            warnings.append(
                "Calendar end time lacked matching source evidence; "
                "the verified duration or configured default was used."
            )
    event_reference_date = fallback.event_reference_date
    if _field_is_current_or_thread_context(
        interpretation.event_reference_date,
        interpretation.event_reference_date_source_text,
        current_request=current_request,
        thread_context=request_context.thread_context,
    ):
        event_reference_date = interpretation.event_reference_date
    event_reference_time = fallback.event_reference_time
    if _field_is_current_or_thread_context(
        interpretation.event_reference_time,
        interpretation.event_reference_time_source_text,
        current_request=current_request,
        thread_context=request_context.thread_context,
    ):
        event_reference_time = interpretation.event_reference_time

    description = fallback.description or request_context.description_payload
    if not description and interpretation.description:
        if not _source_anchored(
            interpretation.description,
            interpretation.description_source_text,
            current_request,
        ):
            return (
                _blocked_plan(
                    fallback,
                    "LLM interpretation proposed an unverified event note",
                ),
                ("Calendar description was not anchored to the operator request.",),
            )
        description = interpretation.description
    append_description = bool(
        description and interpretation.description_mode == "append"
    )

    timezone = fallback.timezone
    if (start_date or start_time) and interpretation.timezone:
        timezone_is_current = _field_is_current(
            interpretation.timezone,
            interpretation.timezone_source_text,
            current_request,
        )
        if timezone_is_current:
            resolved_timezone = _resolved_timezone(
                current_request,
                interpretation.timezone_source_text,
                deterministic_timezone=fallback.timezone,
                interpreted_timezone=interpretation.timezone,
            )
            if resolved_timezone is None:
                return (
                    _blocked_plan(
                        fallback,
                        "LLM interpretation did not verify the updated timezone",
                    ),
                    (
                        "Calendar updated-timezone interpretation or source evidence "
                        "disagreed.",
                    ),
                )
            timezone = resolved_timezone

    resolved_plan = _recompute_calendar_required_field_blockers(
        replace(
            fallback,
            title=title,
            start_date=start_date,
            start_time=start_time,
            end_time=end_time,
            description=description,
            append_description=append_description,
            event_id=event_id,
            event_reference=event_reference,
            event_reference_date=event_reference_date,
            event_reference_time=event_reference_time,
            timezone=timezone,
            all_day=False if start_time else fallback.all_day,
        )
    )
    return (
        resolved_plan,
        tuple(
            [
                *warnings,
                *(
                    f"Calendar interpretation note: {item}"
                    for item in interpretation.ambiguities
                ),
            ]
        ),
    )


def _clean_source_value(value: str) -> str:
    return value.strip(" .,:;\"'\u201c\u201d\u2018\u2019*")[:240]


def _calendar_deictic_event_reference_requested(value: str) -> bool:
    """Recognize event pronouns without treating ``this note`` as event identity."""

    return bool(
        re.search(
            r"\b(?:it|this\s+event|that\s+event|same\s+(?:one|event)|"
            r"the\s+one|above\s+event|event\s+above|"
            r"(?:calendar\s+)?event\s+(?:you\s+)?(?:just\s+)?created|"
            r"(?:calendar\s+)?event\s+created\s+(?:earlier|previously))\b",
            _normalize_source_text(value),
        )
    )


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
    operation_patterns = {
        "create": r"\b(?:add|book|create|schedule)\b",
        "update": (
            r"\b(?:append|change|edit|modify|move|rename|reschedule|update)\b"
        ),
        "delete": r"\b(?:cancel|delete|remove)\b",
    }
    pattern = operation_patterns.get(interpretation.operation)
    return bool(
        evidence
        and evidence in directive
        and pattern
        and re.search(pattern, evidence)
    )


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
            if interpreted_timezone == deterministic_timezone
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
    if field in {"start_time", "end_time"}:
        parsed_start, parsed_end = calendar_times_from_evidence(evidence)
        parsed_value = parsed_start if field == "start_time" else parsed_end
        return parsed_value == expected
    verb = {"create": "create", "update": "update", "delete": "delete"}[operation]
    preposition = "to" if field in {"start_time", "end_time"} else "on"
    probe = infer_calendar_action_plan(
        f"{verb} calendar event that Verification Placeholder {preposition} {evidence}",
        today=today,
    )
    return probe is not None and str(getattr(probe, field, "")) == expected
