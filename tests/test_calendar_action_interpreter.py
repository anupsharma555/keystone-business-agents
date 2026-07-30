from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from keystone_agents.agents import calendar_action_interpreter as interpreter
from keystone_agents.calendar_actions import (
    CalendarActionPlan,
    calendar_interpretation_context,
    calendar_lookup_date,
    calendar_thread_event_context,
    compact_calendar_interpretation_request,
    infer_calendar_action_plan,
    is_calendar_action_candidate,
)
from keystone_agents.schemas.calendar_action import (
    CalendarActionInterpretation,
    CalendarLookupSynthesis,
)
from keystone_agents.schemas.execution_request import ContinuationObjectReference
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.semantic_execution import ExecutionIntentAuthority

REQUEST = (
    "add this event to the calendar: "
    "\u201cLivestream with Corey Ching and Peter Steinberger\u201d July 14 at 2pm EDT"
)


def _verified_calendar_object(
    *,
    object_id: str = "event_exact_123",
    title: str = "KBA_TEST_CALENDAR Context Binding",
    event_date: str = "2026-08-04",
    start_time: str = "",
    end_time: str = "",
    lifecycle_state: str = "active",
) -> ContinuationObjectReference:
    return ContinuationObjectReference(
        provider_system="google_calendar",
        object_type="calendar_event",
        object_id=object_id,
        display_name=title,
        effective_date=event_date,
        lifecycle_state=lifecycle_state,
        verification_status="verified",
        provider_scope={
            key: value
            for key, value in {
                "calendar_id": "primary",
                "start_time": start_time,
                "end_time": end_time,
            }.items()
            if value
        },
    )


def _interpretation(**updates: object) -> CalendarActionInterpretation:
    values: dict[str, object] = {
        "operation": "create",
        "title": "Livestream with Corey Ching and Peter Steinberger",
        "title_source_text": "\u201cLivestream with Corey Ching and Peter Steinberger\u201d",
        "start_date": "2026-07-14",
        "date_source_text": "July 14",
        "start_time": "14:00",
        "time_source_text": "at 2pm",
        "timezone": "America/New_York",
        "timezone_source_text": "EDT",
        "ambiguities": [],
    }
    values.update(updates)
    return CalendarActionInterpretation.model_validate(values)


def test_dated_deadline_without_calendar_noun_is_admitted_and_fully_parsed() -> None:
    request = "CoS add “UT AI Agents application due on July 23rd”"

    plan = infer_calendar_action_plan(request, today=date(2026, 7, 18))

    assert is_calendar_action_candidate(request) is True
    assert plan is not None
    assert plan.operation == "create"
    assert plan.title == "UT AI Agents application due on July 23rd"
    assert plan.start_date == "2026-07-23"
    assert plan.all_day is True
    assert plan.complete is True


@pytest.mark.parametrize(
    "request_text",
    [
        (
            "I have 30 seconds before a meeting. Using only these three facts, "
            "give me three bullets: what changed; why it matters; what still needs "
            "proof. Negative constraints remove forbidden capabilities. Do not "
            "search, draft messages, create records, or include routing metadata."
        ),
        "Explain what changed in the Calendar architecture.",
        "Summarize why we should remove forbidden Calendar capabilities.",
        "Meeting notes: remove blockers from the draft and give me three bullets.",
        "Do not delete the event; summarize these supplied notes.",
    ],
)
def test_incidental_calendar_language_does_not_enter_calendar_fast_path(
    request_text: str,
) -> None:
    assert is_calendar_action_candidate(request_text) is False
    assert (
        infer_calendar_action_plan(request_text, today=date(2026, 7, 19)) is None
    )


@pytest.mark.parametrize(
    "request_text",
    [
        "Schedule a project kickoff meeting for July 30.",
        "Please delete the event titled KBA_TEST_CALENDAR_SAMPLE.",
        "Move my meeting from July 20 to July 21.",
        'Add "application deadline" to my calendar on July 30.',
    ],
)
def test_action_bound_calendar_language_remains_fast_path_eligible(
    request_text: str,
) -> None:
    assert is_calendar_action_candidate(request_text) is True


def test_thread_style_calendar_update_preserves_time_reference_and_quoted_note() -> None:
    request = (
        "move that same KBA_TEST_CALENDAR_COS_0718 event to 3:00-3:30 PM Eastern "
        "and change its note to "
        "\u201crevised through the same natural Slack thread\u201d. "
        "Verify the same event and do not create another one."
    )

    plan = infer_calendar_action_plan(request, today=date(2026, 7, 18))

    assert plan is not None
    assert plan.operation == "update"
    assert plan.event_reference == "that same KBA_TEST_CALENDAR_COS_0718"
    assert plan.start_time == "15:00"
    assert plan.end_time == "15:30"
    assert plan.description == "revised through the same natural Slack thread"
    assert plan.complete is True


@pytest.mark.parametrize(
    "time_field",
    [
        "September 15, 2026 at 2:35 PM to 3:05 PM Eastern Time",
        "September 15, 2026 2:35 PM-3:05 PM Eastern Time",
        "September 15, 2026 between 2:35 PM and 3:05 PM Eastern Time",
        "September 15, 2026 from 2:35 PM until 3:05 PM Eastern Time",
    ],
)
def test_topic_time_calendar_create_preserves_date_prefixed_end_time(
    time_field: str,
) -> None:
    request = (
        "CoS, add this event to Google Calendar: "
        "Topic: KBA_TEST_CALENDAR Fresh Thread Continuity 20260915. "
        f"Time: {time_field}. "
        "Add this note: Synthetic KBA fresh-thread continuity validation. "
        "No external attendees."
    )

    plan = infer_calendar_action_plan(request, today=date(2026, 7, 30))

    assert plan is not None
    assert plan.operation == "create"
    assert plan.title == "KBA_TEST_CALENDAR Fresh Thread Continuity 20260915"
    assert plan.start_date == "2026-09-15"
    assert plan.start_time == "14:35"
    assert plan.end_time == "15:05"
    assert plan.complete is True


def test_labeled_calendar_update_preserves_dash_separated_time_range() -> None:
    request = (
        'Move the calendar event titled "KBA_TEST_CALENDAR Range Update" '
        "on September 15, 2026. "
        "Time: move it to 2:35 PM-3:05 PM Eastern Time."
    )

    plan = infer_calendar_action_plan(request, today=date(2026, 7, 30))

    assert plan is not None
    assert plan.operation == "update"
    assert plan.start_time == "14:35"
    assert plan.end_time == "15:05"
    assert plan.complete is True


@pytest.mark.parametrize(
    "request_text",
    [
        (
            "move the all-day event KBA_TEST_CALENDAR_COS_ACCEPT_0719 "
            "from July 24 to July 25 and keep its note"
        ),
        (
            "reschedule event KBA_TEST_CALENDAR_COS_ACCEPT_0719 "
            "on July 24 for July 25"
        ),
        (
            "shift KBA_TEST_CALENDAR_COS_ACCEPT_0719 calendar event "
            "from July 24 to July 25"
        ),
    ],
)
def test_calendar_date_change_separates_event_identity_old_date_and_new_date(
    request_text: str,
) -> None:
    plan = infer_calendar_action_plan(request_text, today=date(2026, 7, 19))

    assert plan is not None
    assert plan.operation == "update"
    assert plan.event_reference == "KBA_TEST_CALENDAR_COS_ACCEPT_0719"
    assert plan.event_reference_date == "2026-07-24"
    assert plan.start_date == "2026-07-25"
    assert plan.complete is True


def test_calendar_date_change_allows_natural_dated_move_without_event_noun() -> None:
    request = (
        "please move KBA_TEST_CALENDAR_COS_ACCEPT_0719_R2 one day later—"
        "from Monday, July 27 to Tuesday, July 28—"
        "without changing its title or note"
    )

    assert is_calendar_action_candidate(request) is True
    plan = infer_calendar_action_plan(request, today=date(2026, 7, 19))

    assert plan is not None
    assert plan.operation == "update"
    assert plan.event_reference == "KBA_TEST_CALENDAR_COS_ACCEPT_0719_R2"
    assert plan.event_reference_date == "2026-07-27"
    assert plan.start_date == "2026-07-28"
    assert plan.description == ""
    assert plan.complete is True


def test_live_calendar_date_change_accepts_matching_structured_interpretation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "move the all-day event KBA_TEST_CALENDAR_COS_ACCEPT_0719 "
        "from July 24 to July 25 and keep its note"
    )
    fallback = infer_calendar_action_plan(request, today=date(2026, 7, 19))
    assert fallback is not None
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="update",
                operation_source_text="move the all-day event",
                event_reference="KBA_TEST_CALENDAR_COS_ACCEPT_0719",
                event_reference_source_text="KBA_TEST_CALENDAR_COS_ACCEPT_0719",
                start_date="2026-07-25",
                date_source_text="July 25",
                all_day=True,
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        live=True,
        today=date(2026, 7, 19),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is True
    assert resolution.plan.event_reference == "KBA_TEST_CALENDAR_COS_ACCEPT_0719"
    assert resolution.plan.event_reference_date == "2026-07-24"
    assert resolution.plan.start_date == "2026-07-25"


def test_live_calendar_create_requires_one_structured_interpretation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback = infer_calendar_action_plan(REQUEST, today=date(2026, 7, 13))
    assert fallback is not None
    assert fallback.complete is False

    calls: list[dict[str, object]] = []

    def fake_run_typed_sdk_agent(**kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(output=_interpretation())

    monkeypatch.setattr(interpreter, "run_typed_sdk_agent", fake_run_typed_sdk_agent)
    resolution = interpreter.resolve_calendar_action_plan(
        REQUEST,
        fallback,
        live=True,
        today=date(2026, 7, 13),
    )

    assert resolution.plan.complete is True
    assert resolution.plan.title == "Livestream with Corey Ching and Peter Steinberger"
    assert resolution.plan.start_date == "2026-07-14"
    assert resolution.plan.start_time == "14:00"
    assert resolution.plan.end_time == "15:00"
    assert resolution.openai_requests == 1
    assert len(calls) == 1
    assert calls[0]["max_turns"] == 1
    assert calls[0]["live"] is True


def test_live_calendar_create_blocks_llm_and_deterministic_disagreement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "schedule a calendar meeting on July 14th at 2pm titled "
        "\u201cLivestream with Corey Ching and Peter Steinberger\u201d"
    )
    fallback = infer_calendar_action_plan(request, today=date(2026, 7, 13))
    assert fallback is not None
    assert fallback.complete is True

    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **kwargs: SimpleNamespace(
            output=_interpretation(
                title="Different invented title",
                title_source_text="\u201cLivestream with Corey Ching and Peter Steinberger\u201d",
                date_source_text="July 14th",
                timezone_source_text="",
            )
        ),
    )
    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        live=True,
        today=date(2026, 7, 13),
    )

    assert resolution.plan.complete is False
    assert "event title" in " ".join(resolution.plan.blockers).lower()
    assert resolution.openai_requests == 1


def test_source_grounded_create_fields_override_conflicting_parser_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        'Create the calendar event titled "Operator Selected Event" '
        "on August 12, 2026 from 2:15 PM to 2:45 PM Eastern."
    )
    fallback = CalendarActionPlan(
        operation="create",
        title="Parser Selected Event",
        start_date="2026-08-13",
        start_time="15:15",
        end_time="15:45",
        complete=True,
    )
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="create",
                operation_source_text="Create",
                title="Operator Selected Event",
                title_source_text='"Operator Selected Event"',
                start_date="2026-08-12",
                date_source_text="August 12, 2026",
                start_time="14:15",
                end_time="14:45",
                time_source_text="from 2:15 PM to 2:45 PM",
                timezone="America/New_York",
                timezone_source_text="Eastern",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        live=True,
        today=date(2026, 7, 30),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is True
    assert resolution.plan.title == "Operator Selected Event"
    assert resolution.plan.start_date == "2026-08-12"
    assert resolution.plan.start_time == "14:15"
    assert resolution.plan.end_time == "14:45"
    assert len(
        [
            warning
            for warning in resolution.warnings
            if "parser hint" in warning.lower()
        ]
    ) == 4


def test_source_evidence_gate_blocks_model_date_not_supported_by_its_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        'Create the calendar event titled "Evidence Check" '
        "on August 12, 2026 at 2:15 PM Eastern."
    )
    fallback = infer_calendar_action_plan(request, today=date(2026, 7, 30))
    assert fallback is not None and fallback.complete
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="create",
                operation_source_text="Create",
                title="Evidence Check",
                title_source_text='"Evidence Check"',
                start_date="2026-08-13",
                date_source_text="August 12, 2026",
                start_time="14:15",
                time_source_text="2:15 PM",
                timezone="America/New_York",
                timezone_source_text="Eastern",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        live=True,
        today=date(2026, 7, 30),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is False
    assert resolution.plan.start_date == ""
    assert resolution.plan.blockers == ("event date",)


def test_complete_time_update_merges_source_anchored_model_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "change the KBA_TEST_CALENDAR event on July 22, 2026 to "
        "3:00pm-3:45pm Eastern and add the note "
        "Revised during direct-agent validation."
    )
    fallback = infer_calendar_action_plan(request, today=date(2026, 7, 13))
    assert fallback is not None
    assert fallback.complete is True
    assert fallback.description == ""
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="update",
                event_reference="KBA_TEST_CALENDAR",
                event_reference_source_text="KBA_TEST_CALENDAR",
                start_time="15:00",
                time_source_text="3:00pm-3:45pm",
                end_time="15:45",
                timezone="America/New_York",
                timezone_source_text="Eastern",
                description="Revised during direct-agent validation",
                description_source_text=(
                    "Revised during direct-agent validation"
                ),
                description_mode="append",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        live=True,
        today=date(2026, 7, 13),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is True
    assert resolution.plan.start_time == "15:00"
    assert resolution.plan.end_time == "15:45"
    assert resolution.plan.description == "Revised during direct-agent validation"
    assert resolution.plan.append_description is True
    assert resolution.openai_requests == 1


def test_complete_thread_update_preserves_model_owned_time_and_append_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "business agents continue this prior Slack thread. "
        "Previous request: Add KBA_TEST_CALENDAR Compact Delete Latency Check "
        "to Google Calendar on August 11, 2026 at 2:35 PM Eastern. "
        'Previous result: Google Calendar event created and verified: '
        '"KBA_TEST_CALENDAR Compact Delete Latency Check" on 2026-08-11. '
        "User follow-up: Move the calendar event you just created in this thread "
        "to 3:05 PM on the same day and append this note: "
        "Updated through a thread follow-up."
    )
    fallback = infer_calendar_action_plan(request, today=date(2026, 7, 29))
    assert fallback is not None
    assert fallback.complete is False
    assert fallback.operation == "update"
    assert fallback.start_time == ""
    verified_object = _verified_calendar_object(
        object_id="event_compact_latency",
        title="KBA_TEST_CALENDAR Compact Delete Latency Check",
        event_date="2026-08-11",
    )
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="update",
                operation_source_text="Move",
                event_reference="KBA_TEST_CALENDAR Compact Delete Latency Check",
                event_reference_source_text=(
                    "KBA_TEST_CALENDAR Compact Delete Latency Check"
                ),
                start_time="15:05",
                time_source_text="3:05 PM",
                timezone="America/New_York",
                timezone_source_text="Eastern",
                description="Updated through a thread follow-up.",
                description_source_text="Updated through a thread follow-up.",
                description_from_payload=True,
                description_mode="append",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        live=True,
        today=date(2026, 7, 29),
        verified_objects=(verified_object,),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is True
    assert resolution.plan.operation == "update"
    assert resolution.plan.start_time == "15:05"
    # An older continuation receipt may not contain the prior duration. Leave
    # the end open so the provider-resolved event duration is preserved later.
    assert resolution.plan.end_time == ""
    assert resolution.plan.description == "Updated through a thread follow-up."
    assert resolution.plan.append_description is True
    assert resolution.plan.all_day is False
    assert resolution.plan.timezone == "America/New_York"
    assert resolution.openai_requests == 1


def test_update_keeps_existing_identity_separate_from_requested_new_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        'Move the calendar event titled "Review" from August 3, 2026 at 10:15 AM '
        "to August 4, 2026 at 11:30 AM."
    )
    fallback = CalendarActionPlan(
        operation="update",
        event_reference="Review",
        complete=True,
    )
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="update",
                operation_source_text="Move",
                event_reference="Review",
                event_reference_source_text='event titled "Review"',
                event_reference_date="2026-08-03",
                event_reference_date_source_text="August 3, 2026",
                event_reference_time="10:15",
                event_reference_time_source_text="10:15 AM",
                start_date="2026-08-04",
                date_source_text="August 4, 2026",
                start_time="11:30",
                time_source_text="11:30 AM",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        live=True,
        today=date(2026, 7, 29),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is True
    assert resolution.plan.event_reference == "Review"
    assert resolution.plan.event_reference_date == "2026-08-03"
    assert resolution.plan.event_reference_time == "10:15"
    assert resolution.plan.start_date == "2026-08-04"
    assert resolution.plan.start_time == "11:30"


def test_planner_interpreter_operation_disagreement_blocks_without_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = "Create Review on August 4, 2026 at 11:30 AM."
    fallback = CalendarActionPlan(
        operation="create",
        title="Review",
        start_date="2026-08-04",
        start_time="11:30",
        end_time="12:30",
        all_day=False,
        complete=True,
    )
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="delete",
                operation_source_text="Create",
                event_reference="Review",
                event_reference_source_text="Review",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="business_system_write",
            provider_system="google_calendar",
            provider_operations=["create"],
            primary_target="Review",
            ask_shape={"permission_state": "approval_required"},
        ),
        live=True,
        today=date(2026, 7, 29),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is False
    assert resolution.plan.operation == "create"
    assert "operations disagreed" in " ".join(resolution.plan.blockers)
    assert "proposed delete" in " ".join(resolution.warnings)


def test_current_directive_authorizes_source_anchored_calendar_specialist_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "continue this prior Slack thread. "
        "Current user request (authoritative): Move the calendar event created "
        "earlier in this thread to 2:10 PM on the same day, rename it to "
        "KBA_TEST_CALENDAR Semantic Contract Check Updated, and append this note: "
        "Thread change verified. "
        "Previous request: Add KBA_TEST_CALENDAR Semantic Contract Check to "
        "Google Calendar on August 19, 2026 at 1:40 PM Eastern. "
        'Previous result: Google Calendar event created and verified: '
        '"KBA_TEST_CALENDAR Semantic Contract Check" on 2026-08-19. '
        "User follow-up: Move the calendar event created earlier in this thread "
        "to 2:10 PM on the same day, rename it to "
        "KBA_TEST_CALENDAR Semantic Contract Check Updated, and append this note: "
        "Thread change verified."
    )
    fallback = infer_calendar_action_plan(request, today=date(2026, 7, 29))
    assert fallback is not None
    verified_object = _verified_calendar_object(
        object_id="event_semantic_contract",
        title="KBA_TEST_CALENDAR Semantic Contract Check",
        event_date="2026-08-19",
    )
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="update",
                operation_source_text="Move",
                event_reference="KBA_TEST_CALENDAR Semantic Contract Check",
                event_reference_source_text=(
                    "KBA_TEST_CALENDAR Semantic Contract Check"
                ),
                title="KBA_TEST_CALENDAR Semantic Contract Check Updated",
                title_source_text=(
                    "KBA_TEST_CALENDAR Semantic Contract Check Updated"
                ),
                start_time="14:10",
                time_source_text="2:10 PM",
                timezone="America/New_York",
                timezone_source_text="Eastern",
                description="Thread change verified.",
                description_source_text="Thread change verified.",
                description_mode="append",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="business_system_write",
            provider_system="google_calendar",
            provider_operations=["create"],
            primary_target="KBA_TEST_CALENDAR Semantic Contract Check",
            ask_shape={"permission_state": "approval_required"},
        ),
        live=True,
        today=date(2026, 7, 29),
        verified_objects=(verified_object,),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is True
    assert resolution.plan.operation == "update"
    assert resolution.plan.event_reference == (
        "KBA_TEST_CALENDAR Semantic Contract Check"
    )
    assert resolution.plan.title == (
        "KBA_TEST_CALENDAR Semantic Contract Check Updated"
    )
    assert resolution.plan.start_time == "14:10"
    assert resolution.plan.description == "Thread change verified."
    assert resolution.plan.append_description is True


def test_model_owned_thread_identity_resolves_relational_reference_from_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "continue this prior Slack thread. "
        "Current user request (authoritative): Delete the calendar event created "
        "earlier in this thread. "
        "Previous request: Add KBA_TEST_CALENDAR Semantic Contract Check to "
        "Google Calendar on August 19, 2026 at 1:40 PM Eastern. "
        'Previous result: Google Calendar event created and verified: '
        '"KBA_TEST_CALENDAR Semantic Contract Check" on 2026-08-19. '
        "User follow-up: Delete the calendar event created earlier in this thread."
    )
    fallback = infer_calendar_action_plan(request, today=date(2026, 7, 29))
    assert fallback is not None
    verified_object = _verified_calendar_object(
        object_id="event_semantic_contract",
        title="KBA_TEST_CALENDAR Semantic Contract Check",
        event_date="2026-08-19",
    )
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="delete",
                operation_source_text="Delete",
                event_reference="created earlier in this thread",
                event_reference_source_text="created earlier in this thread",
                event_reference_from_thread_context=True,
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="business_system_write",
            provider_system="google_calendar",
            provider_operations=["create"],
            primary_target="KBA_TEST_CALENDAR Semantic Contract Check",
            ask_shape={"permission_state": "approval_required"},
        ),
        live=True,
        today=date(2026, 7, 29),
        verified_objects=(verified_object,),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is True
    assert resolution.plan.operation == "delete"
    assert resolution.plan.event_reference == (
        "KBA_TEST_CALENDAR Semantic Contract Check"
    )
    assert resolution.plan.event_reference_date == "2026-08-19"


def test_dry_run_calendar_create_remains_api_free(monkeypatch: pytest.MonkeyPatch) -> None:
    fallback = infer_calendar_action_plan(REQUEST, today=date(2026, 7, 13))
    assert fallback is not None

    def unexpected_call(**kwargs: object) -> None:
        raise AssertionError(f"unexpected model call: {kwargs}")

    monkeypatch.setattr(interpreter, "run_typed_sdk_agent", unexpected_call)
    resolution = interpreter.resolve_calendar_action_plan(REQUEST, fallback, live=False)

    assert resolution.plan == fallback
    assert resolution.openai_requests == 0


@pytest.mark.parametrize(
    ("request_text", "interpretation"),
    [
        (
            "change the note on the Frontiers paper event to submit the final paper",
            CalendarActionInterpretation(
                operation="update",
                event_reference="Frontiers paper",
                event_reference_source_text="Frontiers paper",
                description="submit the final paper",
                description_source_text="submit the final paper",
            ),
        ),
        (
            "delete the Livestream with Corey Ching and Peter Steinberger event",
            CalendarActionInterpretation(
                operation="delete",
                event_reference="Livestream with Corey Ching and Peter Steinberger",
                event_reference_source_text=(
                    "Livestream with Corey Ching and Peter Steinberger"
                ),
            ),
        ),
    ],
)
def test_live_existing_event_changes_require_llm_identity_interpretation(
    request_text: str,
    interpretation: CalendarActionInterpretation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback = infer_calendar_action_plan(request_text, today=date(2026, 7, 13))
    assert fallback is not None
    assert fallback.complete is True

    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **kwargs: SimpleNamespace(output=interpretation),
    )
    resolution = interpreter.resolve_calendar_action_plan(
        request_text,
        fallback,
        live=True,
        today=date(2026, 7, 13),
    )

    assert resolution.plan.complete is True
    assert resolution.plan.operation == interpretation.operation
    assert resolution.openai_requests == 1


def test_calendar_action_interpreter_agent_has_no_tools() -> None:
    agent = interpreter.build_calendar_action_interpreter_agent()

    assert agent.name == "calendar_action_interpreter"
    assert agent.tools == []
    assert agent.output_type is CalendarActionInterpretation
    assert agent.model_settings.reasoning.effort == "none"
    assert agent.model_settings.max_tokens == 1200


def test_live_calendar_blocks_when_structured_interpretation_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback = infer_calendar_action_plan(
        "add this to the calendar. Event: Condo Walkthrough "
        "Dates: August 3 through August 7 Time: 11 AM to 4 PM each day",
        today=date(2026, 7, 16),
    )
    assert fallback is not None
    assert fallback.complete is True

    class ModelBehaviorError(Exception):
        pass

    def malformed_output(**kwargs: object) -> None:
        raise ModelBehaviorError("structured JSON ended before the note field completed")

    monkeypatch.setattr(interpreter, "run_typed_sdk_agent", malformed_output)

    resolution = interpreter.resolve_calendar_action_plan(
        "add this to the calendar. Event: Condo Walkthrough "
        "Dates: August 3 through August 7 Time: 11 AM to 4 PM each day",
        fallback,
        live=True,
        today=date(2026, 7, 16),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is False
    assert resolution.openai_requests == 1
    assert "LLM calendar interpretation unavailable" in resolution.plan.blockers


def test_slack_thread_update_uses_prior_identity_and_latest_time_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "chief of staff continue this prior Slack thread. "
        "Previous request: CoS schedule a calendar meeting on July 14th at 2pm titled "
        "\u201cLivestream with Corey Ching and Peter Steinberger\u201d "
        "Previous result title: Previous result: not available "
        "User follow-up: Move this event from all-day to 2pm-3pm. "
        "Continue the same agent task."
    )
    fallback = infer_calendar_action_plan(request, today=date(2026, 7, 13))
    assert fallback is not None
    assert fallback.operation == "update"
    assert fallback.event_reference == "Livestream with Corey Ching and Peter Steinberger"
    assert fallback.event_reference_date == "2026-07-14"
    assert fallback.title == ""
    assert fallback.start_date == ""
    assert fallback.start_time == "14:00"
    assert fallback.end_time == "15:00"
    assert fallback.all_day is False
    assert fallback.complete is True

    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="update",
                event_reference="Livestream with Corey Ching and Peter Steinberger",
                # Live structured output may omit this redundant span even when it
                # agrees with the deterministic title/date recovered from history.
                event_reference_source_text="",
                start_time="14:00",
                time_source_text="",
                end_time="15:00",
                timezone="America/New_York",
                ambiguities=[
                    "The exact event ID is not repeated in the follow-up.",
                    "The follow-up does not restate the event date.",
                ],
            )
        ),
    )
    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        live=True,
        today=date(2026, 7, 13),
    )

    assert resolution.plan.complete is True
    assert resolution.plan.event_reference == fallback.event_reference
    assert resolution.plan.start_time == "14:00"
    assert resolution.plan.end_time == "15:00"
    assert resolution.plan.all_day is False
    assert resolution.openai_requests == 1
    assert len(resolution.warnings) == 2
    assert all("interpretation note" in item for item in resolution.warnings)


def test_calendar_thread_interpretation_input_drops_accumulated_failures() -> None:
    request = (
        "chief of staff continue this prior Slack thread. "
        "Previous request: schedule a calendar event on July 14 titled Partner review "
        "Previous result title: Business Agents Failed "
        "Previous result: a very long provider failure and validation traceback "
        "User follow-up: Move this event to 2pm-3pm. "
        "Continue the same agent task, treating the current user request as authoritative."
    )

    compact = compact_calendar_interpretation_request(request)

    assert compact == (
        "Current operator request: Move this event to 2pm-3pm.\n"
        "Previous Calendar request: schedule a calendar event on July 14 titled Partner review"
    )
    assert "Business Agents Failed" not in compact
    assert "traceback" not in compact


@pytest.mark.parametrize(
    "followup",
    [
        "Delete it.",
        "Remove that event.",
        "Cancel the one you just created.",
        "Delete the calendar event you just created in this thread.",
    ],
)
def test_calendar_delete_followup_uses_prior_verified_event_identity(
    followup: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "chief of staff continue this prior Slack thread. "
        "Previous request: CoS add KBA_TEST_CALENDAR Context Binding on August 4, "
        "2026 at 3:20 PM to Google Calendar. "
        "Previous result title: Business Agents Result Ready "
        "Previous result: Google Calendar event created and verified: "
        '"KBA_TEST_CALENDAR Context Binding" on 2026-08-04. '
        f"User follow-up: {followup} "
        "Continue the same agent task."
    )

    fallback = infer_calendar_action_plan(request, today=date(2026, 7, 29))
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="delete",
                operation_source_text=followup,
                event_reference=followup,
                event_reference_source_text=followup,
                event_reference_from_thread_context=True,
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="business_system_write",
            provider_system="google_calendar",
            provider_operations=["delete"],
            primary_target="calendar event from this thread",
            ask_shape={"permission_state": "approval_required"},
        ),
        semantic_candidate=True,
        live=True,
        today=date(2026, 7, 29),
        verified_objects=(_verified_calendar_object(),),
    )

    assert resolution.plan is not None
    assert resolution.plan.operation == "delete"
    assert resolution.plan.event_id == "event_exact_123"
    assert resolution.plan.event_reference == "KBA_TEST_CALENDAR Context Binding"
    assert resolution.plan.event_reference_date == "2026-08-04"
    assert resolution.plan.complete is True


def test_calendar_time_followup_uses_semantic_fields_over_parser_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    followup = "Can u move the above event to a 1pm start on that day?"
    request = (
        "business agents continue this prior Slack thread. "
        "Previous request: @ CoS, add this event to Google Calendar: "
        "Topic: KBA_TEST_CALENDAR Fresh Thread Continuity Retry 20260922. "
        "Time: September 22, 2026 at 2:35 PM to 3:05 PM Eastern Time. "
        "Add this note: Synthetic KBA fresh-thread continuity validation. "
        f"User follow-up: {followup} "
        "Continue the same agent task."
    )
    fallback = infer_calendar_action_plan(request, today=date(2026, 7, 30))
    assert fallback is not None
    assert fallback.operation == "update"
    assert fallback.complete is False
    assert "updated title, date, or note" in fallback.blockers
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="update",
                operation_source_text="move",
                event_reference="above event",
                event_reference_source_text="above event",
                event_reference_from_thread_context=True,
                start_time="13:00",
                time_source_text="1pm start",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="business_system_write",
            provider_system="google_calendar",
            provider_operations=["update"],
            primary_target=(
                "KBA_TEST_CALENDAR Fresh Thread Continuity Retry 20260922"
            ),
            ask_shape={"permission_state": "approval_required"},
        ),
        semantic_candidate=True,
        live=True,
        today=date(2026, 7, 30),
        verified_objects=(
            _verified_calendar_object(
                object_id="event_time_followup",
                title="KBA_TEST_CALENDAR Fresh Thread Continuity Retry 20260922",
                event_date="2026-09-22",
                start_time="14:35",
                end_time="15:05",
            ),
        ),
    )

    assert resolution.plan is not None
    assert resolution.plan.operation == "update"
    assert resolution.plan.event_id == "event_time_followup"
    assert resolution.plan.start_time == "13:00"
    assert resolution.plan.end_time == "13:30"
    assert resolution.plan.blockers == ()
    assert resolution.plan.complete is True


def test_unchanged_default_timezone_is_not_treated_as_a_requested_change() -> None:
    assert (
        interpreter._resolved_timezone(
            "Can u move the above event to a 1pm start on that day?",
            "that day",
            deterministic_timezone="America/New_York",
            interpreted_timezone="America/New_York",
        )
        == "America/New_York"
    )
    assert (
        interpreter._resolved_timezone(
            "Can u move the above event to a 1pm start on that day?",
            "that day",
            deterministic_timezone="America/New_York",
            interpreted_timezone="America/Los_Angeles",
        )
        is None
    )


def test_calendar_interpretation_context_includes_verified_prior_event_identity() -> None:
    request = (
        "chief of staff continue this prior Slack thread. "
        "Previous request: CoS add KBA_TEST_CALENDAR Context Binding on August 4, "
        "2026 at 3:20 PM to Google Calendar. "
        "Previous result title: Business Agents Result Ready "
        "Previous result: Google Calendar event created and verified: "
        '"KBA_TEST_CALENDAR Context Binding" on 2026-08-04. '
        "User follow-up: Delete it. "
        "Continue the same agent task."
    )

    compact = compact_calendar_interpretation_request(
        request,
        verified_objects=(_verified_calendar_object(),),
    )

    assert "Current operator request: Delete it." in compact
    assert (
        'Prior verified Calendar event: title="KBA_TEST_CALENDAR Context Binding" '
        'date="2026-08-04"'
    ) in compact
    assert "Previous Calendar request:" in compact


def test_calendar_thread_context_prefers_newest_distinct_verified_event() -> None:
    context = calendar_thread_event_context(
        "Delete the calendar event you just created.",
        verified_objects=(
            _verified_calendar_object(
                object_id="event_newer",
                title="Newest event",
                event_date="2026-08-22",
            ),
            _verified_calendar_object(
                object_id="event_older",
                title="Older event",
                event_date="2026-08-21",
            ),
        ),
    )

    assert context["event_id"] == "event_newer"
    assert context["event_reference"] == "Newest event"
    assert context["event_reference_date"] == "2026-08-22"


def test_newest_verified_deletion_is_not_replaced_by_older_active_event() -> None:
    context = calendar_thread_event_context(
        "What happened to the event I just deleted?",
        verified_objects=(
            _verified_calendar_object(
                object_id="event_newer",
                title="Newest event",
                event_date="2026-08-22",
                lifecycle_state="deleted",
            ),
            _verified_calendar_object(
                object_id="event_older",
                title="Older event",
                event_date="2026-08-21",
            ),
        ),
    )

    assert context["event_id"] == ""
    assert context["event_reference"] == ""
    assert context["event_reference_date"] == ""
    assert context["source"] == "verified_prior_deletion"


def test_calendar_delete_followup_accepts_contextual_model_reference_when_receipt_is_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "chief of staff continue this prior Slack thread. "
        "Previous request: CoS add KBA_TEST_CALENDAR Context Binding on August 4, "
        "2026 at 3:20 PM to Google Calendar. "
        "Previous result title: Business Agents Result Ready "
        "Previous result: Google Calendar event created and verified: "
        '"KBA_TEST_CALENDAR Context Binding" on 2026-08-04. '
        "User follow-up: Delete the calendar event you just created in this thread. "
        "Continue the same agent task."
    )
    fallback = infer_calendar_action_plan(request, today=date(2026, 7, 29))
    assert fallback is not None
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="delete",
                operation_source_text="Delete",
                event_reference="you just created in this thread",
                event_reference_source_text="you just created in this thread",
                event_reference_from_thread_context=True,
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        live=True,
        today=date(2026, 7, 29),
        verified_objects=(_verified_calendar_object(),),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is True
    assert resolution.plan.event_id == "event_exact_123"
    assert resolution.plan.event_reference == "KBA_TEST_CALENDAR Context Binding"
    assert resolution.plan.event_reference_date == "2026-08-04"


def test_contextual_delete_uses_latest_verified_identity_after_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "chief of staff continue this prior Slack thread. "
        "Previous request: CoS add KBA_TEST_CALENDAR Typed Continuity Validation "
        "on August 21, 2026 at 2:20 PM to Google Calendar. "
        "Previous result: Google Calendar event updated and verified: "
        '"KBA_TEST_CALENDAR Typed Continuity Validation Updated" on 2026-08-21. '
        "User follow-up: Delete the calendar event you just updated in this thread. "
        "Continue the same agent task."
    )
    fallback = infer_calendar_action_plan(request, today=date(2026, 7, 29))
    assert fallback is not None
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="delete",
                operation_source_text="Delete",
                event_reference="you just updated in this thread",
                event_reference_source_text="you just updated in this thread",
                event_reference_from_thread_context=True,
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        live=True,
        today=date(2026, 7, 29),
        verified_objects=(
            _verified_calendar_object(
                object_id="event_renamed_456",
                title="KBA_TEST_CALENDAR Typed Continuity Validation Updated",
                event_date="2026-08-21",
            ),
        ),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is True
    assert resolution.plan.event_id == "event_renamed_456"
    assert (
        resolution.plan.event_reference
        == "KBA_TEST_CALENDAR Typed Continuity Validation Updated"
    )
    assert resolution.plan.event_reference_date == "2026-08-21"


def test_contextual_delete_uses_source_visible_canonical_target_for_legacy_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "business agents continue this prior Slack thread. "
        "Previous request: CoS add KBA_TEST_CALENDAR Typed Continuity Validation "
        "on August 21, 2026 at 2:20 PM to Google Calendar. "
        "Most recent operator turn: Rename that event to "
        '"KBA_TEST_CALENDAR Typed Continuity Validation Updated". '
        "User follow-up: Delete the calendar event you just updated in this thread. "
        "Continue the same agent task."
    )
    fallback = infer_calendar_action_plan(request, today=date(2026, 7, 29))
    assert fallback is not None
    assert fallback.event_reference != (
        "KBA_TEST_CALENDAR Typed Continuity Validation Updated"
    )
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="delete",
                operation_source_text="Delete",
                event_reference="you just updated in this thread",
                event_reference_source_text="you just updated in this thread",
                event_reference_from_thread_context=True,
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="business_system_write",
            task_objective="business_system_write",
            provider_system="google_calendar",
            provider_operations=["delete"],
            primary_target="KBA_TEST_CALENDAR Typed Continuity Validation Updated",
            required_terms=[
                "KBA_TEST_CALENDAR Typed Continuity Validation Updated",
            ],
            ask_shape={"permission_state": "approval_required"},
        ),
        live=True,
        today=date(2026, 7, 29),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is True
    assert resolution.plan.event_id == ""
    assert (
        resolution.plan.event_reference
        == "KBA_TEST_CALENDAR Typed Continuity Validation Updated"
    )
    assert resolution.plan.event_reference_date == "2026-08-21"
    assert resolution.plan.calendar_scope == "all_readable"


def test_contextual_delete_rejects_canonical_target_visible_only_in_bot_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "business agents continue this prior Slack thread. "
        "Previous request: CoS add KBA_TEST_CALENDAR Original Title "
        "on August 21, 2026 at 2:20 PM to Google Calendar. "
        "Previous result: Google Calendar event updated and verified: "
        '"KBA_TEST_CALENDAR Bot Only Title" on 2026-08-21. '
        "User follow-up: Delete the calendar event you just updated in this thread. "
        "Continue the same agent task."
    )
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="delete",
                operation_source_text="Delete",
                event_reference="you just updated in this thread",
                event_reference_source_text="you just updated in this thread",
                event_reference_from_thread_context=True,
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        infer_calendar_action_plan(request, today=date(2026, 7, 29)),
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="business_system_write",
            task_objective="business_system_write",
            provider_system="google_calendar",
            provider_operations=["delete"],
            primary_target="KBA_TEST_CALENDAR Bot Only Title",
            required_terms=["KBA_TEST_CALENDAR Bot Only Title"],
            ask_shape={"permission_state": "approval_required"},
        ),
        live=True,
        today=date(2026, 7, 29),
    )

    assert resolution.plan is not None
    assert resolution.plan.event_id == ""
    assert resolution.plan.event_reference != "KBA_TEST_CALENDAR Bot Only Title"


def test_explicit_calendar_target_overrides_unrelated_verified_thread_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "chief of staff continue this prior Slack thread. "
        "Previous request: CoS add KBA_TEST_CALENDAR Context Binding on August 4, "
        "2026 at 3:20 PM to Google Calendar. "
        "Previous result: Google Calendar event created and verified: "
        '"KBA_TEST_CALENDAR Context Binding" on 2026-08-04. '
        "User follow-up: Delete both calendar events titled "
        '"Expert Initial Interview: Anup Sharma" on August 3, 2026 at 10:15 AM. '
        "Continue the same agent task."
    )
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="delete",
                operation_source_text="Delete both calendar events",
                title="Expert Initial Interview: Anup Sharma",
                title_source_text=(
                    'events titled "Expert Initial Interview: Anup Sharma"'
                ),
                start_date="2026-08-03",
                date_source_text="August 3, 2026",
                start_time="10:15",
                time_source_text="10:15 AM",
                all_day=False,
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        None,
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="business_system_write",
            task_objective="business_system_write",
            provider_system="google_calendar",
            provider_operations=["delete"],
            desired_count=2,
            desired_count_explicit=True,
            ask_shape={"permission_state": "approval_required"},
        ),
        live=True,
        today=date(2026, 7, 29),
    )

    assert resolution.plan is not None
    assert resolution.plan.event_reference == "Expert Initial Interview: Anup Sharma"
    assert resolution.plan.event_reference_date == "2026-08-03"
    assert resolution.plan.start_time == "10:15"
    assert resolution.plan.target_count == 2
    assert resolution.plan.calendar_scope == "all_readable"
    assert resolution.plan.event_id == ""


def test_matching_explicit_reference_binds_verified_id_without_model_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        'Update the calendar event titled "KBA_TEST_CALENDAR Exact Binding" '
        "and append this note: Verified identity retained."
    )
    fallback = CalendarActionPlan(
        operation="update",
        event_id="event_exact_binding",
        event_reference="KBA_TEST_CALENDAR Exact Binding",
        complete=False,
        blockers=("updated title, date, or note",),
    )
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="update",
                operation_source_text="Update",
                event_reference="KBA_TEST_CALENDAR Exact Binding",
                event_reference_source_text=(
                    'event titled "KBA_TEST_CALENDAR Exact Binding"'
                ),
                description="Verified identity retained.",
                description_source_text="Verified identity retained.",
                description_mode="append",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        live=True,
        today=date(2026, 7, 30),
        verified_objects=(
            _verified_calendar_object(
                object_id="event_exact_binding",
                title="KBA_TEST_CALENDAR Exact Binding",
                event_date="2026-08-12",
            ),
        ),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is True
    assert resolution.plan.event_id == "event_exact_binding"
    assert resolution.plan.description == "Verified identity retained."


def test_new_explicit_reference_drops_unrelated_verified_prior_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        'Update the calendar event titled "KBA_TEST_CALENDAR New Target" '
        "and append this note: Apply only to the named event."
    )
    fallback = CalendarActionPlan(
        operation="update",
        event_id="event_old_target",
        event_reference="KBA_TEST_CALENDAR Old Target",
        complete=False,
        blockers=("updated title, date, or note",),
    )
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="update",
                operation_source_text="Update",
                event_reference="KBA_TEST_CALENDAR New Target",
                event_reference_source_text=(
                    'event titled "KBA_TEST_CALENDAR New Target"'
                ),
                description="Apply only to the named event.",
                description_source_text="Apply only to the named event.",
                description_mode="append",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        live=True,
        today=date(2026, 7, 30),
        verified_objects=(
            _verified_calendar_object(
                object_id="event_old_target",
                title="KBA_TEST_CALENDAR Old Target",
                event_date="2026-08-11",
            ),
        ),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is True
    assert resolution.plan.event_id == ""
    assert resolution.plan.event_reference == "KBA_TEST_CALENDAR New Target"
    assert any("parser hint" in warning.lower() for warning in resolution.warnings)


def test_plural_calendar_delete_is_a_complete_current_target() -> None:
    plan = infer_calendar_action_plan(
        "Delete both calendar events titled "
        '"Expert Initial Interview: Anup Sharma" '
        "on August 3, 2026 at 10:15 AM.",
        today=date(2026, 7, 29),
    )

    assert plan is not None
    assert plan.operation == "delete"
    assert plan.event_reference == "Expert Initial Interview: Anup Sharma"
    assert plan.event_reference_date == "2026-08-03"
    assert plan.start_time == "10:15"
    assert plan.complete is True


def test_calendar_delete_followup_without_event_context_blocks_instead_of_guessing() -> None:
    request = "Delete the calendar event you just created in this thread."

    plan = infer_calendar_action_plan(request, today=date(2026, 7, 29))

    assert plan is not None
    assert plan.operation == "delete"
    assert plan.event_reference == ""
    assert plan.complete is False
    assert "event name or exact event id" in plan.blockers


def test_deleted_prior_calendar_result_is_not_reused_as_active_event_context() -> None:
    request = (
        "chief of staff continue this prior Slack thread. "
        "Previous request: Delete the event titled KBA_TEST_CALENDAR Old Event. "
        "Previous result title: Business Agents Result Ready "
        "Previous result: Google Calendar event deleted and verified: "
        '"KBA_TEST_CALENDAR Old Event" on 2026-08-04. '
        "User follow-up: Delete it. "
        "Continue the same agent task."
    )

    plan = infer_calendar_action_plan(request, today=date(2026, 7, 29))

    assert plan is None


def test_model_cannot_reuse_a_provider_verified_deleted_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "chief of staff continue this prior Slack thread. "
        "Previous request: Delete the event titled KBA_TEST_CALENDAR Old Event. "
        "Previous result title: Business Agents Result Ready "
        "Previous result: Google Calendar event deleted and verified: "
        '"KBA_TEST_CALENDAR Old Event" on 2026-08-04. '
        "User follow-up: Delete it. "
        "Continue the same agent task."
    )
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="delete",
                operation_source_text="Delete",
                event_reference="KBA_TEST_CALENDAR Old Event",
                event_reference_source_text="KBA_TEST_CALENDAR Old Event",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        None,
        semantic_candidate=True,
        live=True,
        today=date(2026, 7, 29),
        verified_objects=(
            _verified_calendar_object(
                object_id="event_deleted_123",
                title="KBA_TEST_CALENDAR Old Event",
                event_date="2026-08-04",
                lifecycle_state="deleted",
            ),
        ),
    )

    assert resolution.plan is not None
    assert resolution.plan.operation == "delete"
    assert resolution.plan.event_reference == ""
    assert resolution.plan.complete is False
    assert "event name or exact event id" in resolution.plan.blockers


def test_llm_first_calendar_thread_note_append_does_not_require_phrase_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = (
        "https://discord.com/channels/974519864045756446/"
        "1415384556521132134"
    )
    request = (
        "chief of staff continue this prior Slack thread. "
        "Previous request: CoS schedule a calendar meeting on July 14th at 2pm titled "
        "\u201cLivestream with Corey Ching and Peter Steinberger\u201d "
        "Previous result title: Calendar event updated "
        "Previous result: provider verification passed "
        f"User follow-up: Add this link to the notes: {url} "
        "Continue the same agent task."
    )
    assert infer_calendar_action_plan(request, today=date(2026, 7, 13)) is None
    assert is_calendar_action_candidate(request) is True
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="update",
                event_reference="Livestream with Corey Ching and Peter Steinberger",
                description=url,
                description_source_text=url,
                description_mode="append",
                timezone="America/New_York",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        None,
        live=True,
        today=date(2026, 7, 13),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is True
    assert resolution.plan.operation == "update"
    assert resolution.plan.event_reference == (
        "Livestream with Corey Ching and Peter Steinberger"
    )
    assert resolution.plan.event_reference_date == "2026-07-14"
    assert resolution.plan.description == url
    assert resolution.plan.append_description is True
    assert resolution.openai_requests == 1


def test_semantic_calendar_thread_read_does_not_require_phrase_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "chief of staff continue this prior Slack thread. "
        "Provider affinity: calendar "
        "Previous request: CoS add UT Austin Course Starts on August 15, 2026 "
        "to my Google Calendar. Can add it at 8am-9am. "
        "Previous result title: Business Agents WorkItem Failed "
        "Previous result: No specialist or provider action ran. "
        "User follow-up: Is it on the calendar now? "
        "Continue the same agent task."
    )
    assert infer_calendar_action_plan(request, today=date(2026, 7, 20)) is None
    assert is_calendar_action_candidate(request) is False
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="read",
                operation_source_text="Is it on the calendar now?",
                event_reference="UT Austin Course Starts",
                event_reference_source_text="UT Austin Course Starts",
                event_reference_date="2026-08-15",
                event_reference_date_source_text="August 15, 2026",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        None,
        semantic_candidate=True,
        live=True,
        today=date(2026, 7, 20),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is True
    assert resolution.plan.operation == "read"
    assert resolution.plan.event_reference == "UT Austin Course Starts"
    assert resolution.plan.start_date == ""
    assert resolution.plan.event_reference_date == "2026-08-15"
    assert resolution.openai_requests == 1


@pytest.mark.parametrize(
    (
        "request_text",
        "title",
        "title_source",
        "date_source",
        "start_time",
        "time_source",
        "end_time",
        "timezone_source",
    ),
    [
        (
            REQUEST,
            "Livestream with Corey Ching and Peter Steinberger",
            "\u201cLivestream with Corey Ching and Peter Steinberger\u201d",
            "July 14",
                "14:00",
                "at 2pm",
                "15:00",
                "EDT",
        ),
        (
            "schedule a calendar meeting on July 14th at 2pm titled "
            "*\u201cLivestream with Corey Ching and Peter Steinberger\u201d*",
            "Livestream with Corey Ching and Peter Steinberger",
            "*\u201cLivestream with Corey Ching and Peter Steinberger\u201d*",
            "July 14th",
                "14:00",
                "at 2pm",
                "15:00",
                "",
        ),
        (
            "on July 14 at 2:00 PM create a calendar event called Partner review",
            "Partner review",
            "Partner review",
            "July 14",
                "14:00",
                "at 2:00 PM",
                "15:00",
                "",
        ),
        (
            "put a meeting for Product planning on July 14 from 2 to 3 PM",
            "Product planning",
            "Product planning",
            "July 14",
            "14:00",
            "from 2 to 3 PM",
            "15:00",
            "",
        ),
        (
            "create an all-day calendar event for Board planning on July 14",
            "Board planning",
            "Board planning",
            "July 14",
            "",
            "",
            "",
            "",
        ),
    ],
)
def test_calendar_create_variations_reconcile_to_the_same_safe_contract(
    request_text: str,
    title: str,
    title_source: str,
    date_source: str,
    start_time: str,
    time_source: str,
    end_time: str,
    timezone_source: str,
) -> None:
    fallback = infer_calendar_action_plan(request_text, today=date(2026, 7, 13))
    assert fallback is not None

    interpretation = CalendarActionInterpretation(
        operation="create",
        title=title,
        title_source_text=title_source,
        start_date="2026-07-14",
        date_source_text=date_source,
        start_time=start_time,
        time_source_text=time_source,
        end_time=end_time,
        timezone="America/New_York",
        timezone_source_text=timezone_source,
        ambiguities=[],
    )
    plan, warnings = interpreter._validated_interpretation_plan(
        request_text,
        fallback,
        interpretation,
        today=date(2026, 7, 13),
    )

    assert warnings == ()
    assert plan.complete is True
    assert plan.title == title
    assert plan.start_date == "2026-07-14"
    assert plan.start_time == start_time
    assert plan.end_time == end_time
    assert plan.all_day is (not bool(start_time))


def test_long_calendar_note_is_immutable_payload_not_model_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = (
        "Inspections will begin on the 30th floor on Monday, August 3, with the "
        "team working their way down the building until all balconies have been "
        "inspected, concluding on the 5th floor. These inspections are necessary "
        "to verify that the work was completed properly and to identify any areas "
        "requiring attention while the project remains under warranty."
    )
    request = (
        "CoS add this to the calendar. Event: Condo Walkthrough "
        "Dates: Monday, August 3 through Friday, August 7 "
        f"Time: 11:00 AM to 4:00 PM each day Note: {note}"
    )
    fallback = infer_calendar_action_plan(request, today=date(2026, 7, 16))
    assert fallback is not None
    context = calendar_interpretation_context(request)

    assert context.description_payload == note
    assert note not in context.directive_text
    assert "[event description supplied]" in context.directive_text

    observed_prompt = ""

    def fake_run_typed_sdk_agent(**kwargs: object) -> SimpleNamespace:
        nonlocal observed_prompt
        observed_prompt = kwargs["typed_input"].to_prompt()
        return SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="create",
                operation_source_text="add this to the calendar",
                title="Condo Walkthrough",
                title_source_text="Condo Walkthrough",
                start_date="2026-08-03",
                date_source_text="Monday, August 3 through Friday, August 7",
                end_date="2026-08-07",
                end_date_source_text="Monday, August 3 through Friday, August 7",
                start_time="11:00",
                time_source_text="11:00 AM to 4:00 PM each day",
                end_time="16:00",
                repeat_each_day=True,
                timezone="America/New_York",
                description_from_payload=True,
            )
        )

    monkeypatch.setattr(interpreter, "run_typed_sdk_agent", fake_run_typed_sdk_agent)
    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        live=True,
        today=date(2026, 7, 16),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is True
    assert resolution.plan.operation == "create"
    assert resolution.plan.end_date == "2026-08-07"
    assert resolution.plan.repeat_each_day is True
    assert resolution.plan.description == note
    assert note not in observed_prompt
    assert context.description_payload_sha256 in observed_prompt


def test_calendar_note_payload_excludes_trailing_control_sentence() -> None:
    request = (
        "Move that same event to September 23, 2026 from 1:15 PM to 1:45 PM "
        "Eastern, rename it to "
        "\u201cKBA_TEST_CALENDAR Semantic Authority Final 20260923\u201d, "
        "and append this note: Semantic follow-up fields verified. "
        "Keep everything else unchanged."
    )

    context = calendar_interpretation_context(request)
    plan = infer_calendar_action_plan(request, today=date(2026, 7, 30))

    assert context.description_payload == "Semantic follow-up fields verified."
    assert "Keep everything else unchanged." in context.directive_text
    assert plan is not None
    assert plan.description == "Semantic follow-up fields verified."


def test_meeting_invite_add_this_to_notes_is_immutable_payload() -> None:
    request = (
        "add this event to the Google Calendar: "
        "Topic: Expert Initial Interview: Example Person "
        "Time: Jul 31, 2026 10:15 AM Eastern Time (US and Canada) "
        "Also, add this to the notes "
        "Join Zoom Meeting https://example.zoom.us/j/123456789 "
        "Meeting ID: 123 456 789"
    )

    context = calendar_interpretation_context(request)
    plan = infer_calendar_action_plan(request, today=date(2026, 7, 29))

    assert plan is not None
    assert plan.complete is True
    assert plan.operation == "create"
    assert plan.title == "Expert Initial Interview: Example Person"
    assert plan.start_date == "2026-07-31"
    assert plan.start_time == "10:15"
    assert plan.end_time == "11:15"
    assert context.description_payload == (
        "Join Zoom Meeting https://example.zoom.us/j/123456789 "
        "Meeting ID: 123 456 789"
    )
    assert plan.description == context.description_payload
    assert context.description_payload not in context.directive_text
    assert "[event description supplied]" in context.directive_text


def test_calendar_note_longer_than_old_limit_is_not_truncated() -> None:
    note = "Provider-owned event note. " * 240
    request = (
        "create a calendar event titled Long note test on August 20, 2026 "
        f"at 1 PM Note: {note}"
    )

    plan = infer_calendar_action_plan(request, today=date(2026, 7, 16))
    context = calendar_interpretation_context(request)

    assert plan is not None
    assert len(note) > 2_000
    assert plan.description == note.strip()
    assert context.description_payload == note.strip()


def test_prose_event_facts_after_note_marker_are_interpreted_before_payload_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "CoS can u add this to the calendar for the dates and times specified "
        "here also with a note: LWC Window Washing is scheduled to begin "
        "cleaning the exterior windows on Monday, July 27, and is expected to "
        "complete the project by Friday, July 31, weather permitting. The "
        "window washing crews typically arrive at the building around 8:30 AM "
        "each morning and work throughout the day, generally finishing in the "
        "afternoon."
    )
    fallback = infer_calendar_action_plan(request, today=date(2026, 7, 16))
    context = calendar_interpretation_context(request)

    assert fallback is not None
    assert fallback.complete is True
    assert fallback.title == "LWC Window Washing"
    assert fallback.end_date == "2026-07-31"
    assert fallback.repeat_each_day is True
    assert context.description_payload.startswith("LWC Window Washing")
    assert "LWC Window Washing is scheduled" in context.event_fact_text
    assert "around 8:30 AM each morning" in context.event_fact_text

    observed_prompt = ""

    def fake_run_typed_sdk_agent(**kwargs: object) -> SimpleNamespace:
        nonlocal observed_prompt
        observed_prompt = kwargs["typed_input"].to_prompt()
        return SimpleNamespace(
                output=CalendarActionInterpretation(
                    operation="create",
                    operation_source_text="add this to the calendar",
                    title="LWC Window Washing",
                    title_source_text="LWC Window Washing",
                    start_date="2026-07-27",
                date_source_text=(
                    "Monday, July 27, and is expected to complete the project "
                    "by Friday, July 31"
                ),
                end_date="2026-07-31",
                end_date_source_text=(
                    "Monday, July 27, and is expected to complete the project "
                    "by Friday, July 31"
                ),
                start_time="08:30",
                time_source_text="around 8:30 AM each morning",
                repeat_each_day=True,
                timezone="America/New_York",
                description_from_payload=True,
                ambiguities=["Completion remains weather permitting."],
            )
        )

    monkeypatch.setattr(interpreter, "run_typed_sdk_agent", fake_run_typed_sdk_agent)
    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        live=True,
        today=date(2026, 7, 16),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is True
    assert resolution.plan.title == "LWC Window Washing"
    assert resolution.plan.start_date == "2026-07-27"
    assert resolution.plan.end_date == "2026-07-31"
    assert resolution.plan.repeat_each_day is True
    assert resolution.plan.start_time == "08:30"
    assert resolution.plan.end_time == "09:30"
    assert resolution.plan.description.startswith("LWC Window Washing")
    assert "Event fact evidence: LWC Window Washing" in observed_prompt
    assert "Completion remains weather permitting." in resolution.warnings[0]


@pytest.mark.parametrize(
    ("request_text", "expected_title", "expected_start", "expected_end"),
    [
        (
            "Add this to my calendar with a note: Exterior Painting is planned "
            "to start Monday, August 3 and finish Friday, August 7. The crew "
            "arrives around 7 AM every morning.",
            "Exterior Painting",
            "2026-08-03",
            "2026-08-07",
        ),
        (
            "Create a calendar event with a note: Roof Inspection is scheduled "
            "to begin Tuesday, August 11 and complete Thursday, August 13. "
            "Inspectors arrive at 9:15 AM each day.",
            "Roof Inspection",
            "2026-08-11",
            "2026-08-13",
        ),
    ],
)
def test_prose_subject_and_daily_range_are_source_grounded_fallbacks(
    request_text: str,
    expected_title: str,
    expected_start: str,
    expected_end: str,
) -> None:
    plan = infer_calendar_action_plan(request_text, today=date(2026, 7, 16))

    assert plan is not None
    assert plan.complete is True
    assert plan.title == expected_title
    assert plan.start_date == expected_start
    assert plan.end_date == expected_end
    assert plan.repeat_each_day is True
    assert plan.end_time == interpreter.default_calendar_end_time(plan.start_time)


def test_source_anchored_model_operation_can_repair_wrong_word_based_route() -> None:
    request = (
        "add this to the calendar. Event: Condo Walkthrough Dates: August 3 "
        "through August 7 Time: 11 AM to 4 PM each day "
        "Note: The team will move down the building during inspection."
    )
    wrong_fallback = CalendarActionPlan(
        operation="update",
        title="Condo Walkthrough",
        start_date="2026-08-03",
        start_time="11:00",
        end_time="16:00",
        complete=False,
        blockers=("event name or exact event id",),
    )
    interpretation = CalendarActionInterpretation(
        operation="create",
        operation_source_text="add this to the calendar",
        title="Condo Walkthrough",
        title_source_text="Condo Walkthrough",
        start_date="2026-08-03",
        date_source_text="August 3 through August 7",
        end_date="2026-08-07",
        end_date_source_text="August 3 through August 7",
        start_time="11:00",
        time_source_text="11 AM to 4 PM each day",
        end_time="16:00",
        repeat_each_day=True,
        timezone="America/New_York",
        description_from_payload=True,
    )

    plan, warnings = interpreter._validated_interpretation_plan(
        request,
        wrong_fallback,
        interpretation,
        today=date(2026, 7, 16),
    )

    assert warnings == ()
    assert plan.operation == "create"
    assert plan.complete is True
    assert plan.description == "The team will move down the building during inspection."


def test_canonical_calendar_operation_cannot_be_reopened_by_request_wording() -> None:
    request = "Create a calendar event called Product Review on August 8, 2026."
    fallback = infer_calendar_action_plan(request, today=date(2026, 7, 21))
    assert fallback is not None
    assert fallback.operation == "create"

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        fallback,
        manual_plan=ManualRequestPlan(
            source="canonical",
            target_agent="chief_of_staff",
            intent="context_lookup",
            task_objective="context_lookup",
            provider_system="google_calendar",
            provider_operations=["read"],
        ),
        today=date(2026, 7, 21),
    )

    assert resolution.plan is not None
    assert resolution.plan.operation == "read"


def test_canonical_calendar_target_replaces_stale_thread_event_for_read() -> None:
    request = (
        "business agents continue this prior Slack thread. "
        "Previous request: What about the UT course start? "
        "User follow-up: The course start is 08/15/2026 "
        "Continue the same agent task."
    )
    stale_plan = CalendarActionPlan(
        operation="read",
        event_reference="UT Course Orientation Session",
        complete=True,
    )

    plan, warnings = interpreter._apply_canonical_calendar_lookup_target(
        request,
        stale_plan,
        manual_plan=ManualRequestPlan(
            source="canonical",
            target_agent="chief_of_staff",
            intent="context_lookup",
            task_objective="context_lookup",
            provider_system="google_calendar",
            provider_operations=["read"],
            primary_target="UT course start",
        ),
        today=date(2026, 7, 21),
    )

    assert plan is not None
    assert plan.event_reference == "UT course start"
    assert plan.event_reference_date == "2026-08-15"
    assert plan.complete is True
    assert warnings


def test_numeric_followup_date_can_bound_calendar_lookup() -> None:
    request = (
        "business agents continue this prior Slack thread. "
        "Previous request: What about the UT course start? "
        "User follow-up: The course start is 08/15/2026 "
        "Continue the same agent task."
    )

    assert calendar_lookup_date(request, today=date(2026, 7, 21)) == "2026-08-15"


def test_canonical_bounded_calendar_read_uses_current_window_not_prior_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "business agents continue this prior Slack thread. "
        "Previous request: The course start is 08/15/2026 "
        "Previous result: The course event was found. "
        "User follow-up: What is next calendar event for today? "
        "Continue the same agent task."
    )
    stale_plan = CalendarActionPlan(
        operation="read",
        event_reference="UT course start",
        event_reference_date="2026-08-15",
        complete=True,
    )

    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="read",
                operation_source_text="What is next calendar event for today?",
                read_scope="single_event",
                read_selection="next",
                read_selection_source_text="next",
                date_scope="today",
                event_reference="Google Calendar today's next event",
                event_reference_source_text="next calendar event for today",
                date_source_text="today",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        stale_plan,
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="context_lookup",
            task_objective="context_lookup",
            provider_system="google_calendar",
            provider_operations=["read"],
            provider_read_scope="bounded_collection",
            primary_target="Google Calendar today's next event",
        ),
        live=True,
        today=date(2026, 7, 21),
    )

    assert resolution.plan is not None
    assert resolution.plan.operation == "read"
    assert resolution.plan.read_scope == "time_window"
    assert resolution.plan.read_selection == "next"
    assert resolution.plan.date_scope == "today"
    assert resolution.plan.start_date == "2026-07-21"
    assert resolution.plan.event_reference == ""
    assert resolution.plan.event_reference_date == ""
    assert resolution.plan.complete is True


@pytest.mark.parametrize(
    ("primary_target", "expected_query"),
    [
        ("medical appointment", "medical appointment"),
        ("my medical appointments for tomorrow", "medical appointments"),
        ("Google Calendar events tomorrow", ""),
    ],
)
def test_complete_canonical_calendar_read_reuses_planner_without_second_model_call(
    monkeypatch: pytest.MonkeyPatch,
    primary_target: str,
    expected_query: str,
) -> None:
    request = (
        "Find my medical appointment tomorrow."
        if expected_query
        else "List all Google Calendar events tomorrow."
    )

    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **kwargs: pytest.fail(f"unexpected model call: {kwargs}"),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        None,
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="context_lookup",
            task_objective="context_lookup",
            provider_system="google_calendar",
            provider_operations=["read"],
            provider_read_scope="bounded_collection",
            provider_result_mode="items",
            primary_target=primary_target,
            ask_shape={
                "ask_breadth": "narrow" if expected_query else "broad",
                "permission_state": "read_only",
            },
        ),
        live=True,
        today=date(2026, 7, 27),
    )

    assert resolution.plan is not None
    assert resolution.plan.operation == "read"
    assert resolution.plan.read_scope == (
        "filtered_window" if expected_query else "time_window"
    )
    assert resolution.plan.query == expected_query
    assert resolution.plan.start_date == "2026-07-28"
    assert resolution.plan.date_scope == "tomorrow"
    assert resolution.plan.calendar_scope == "all_readable"
    assert resolution.plan.complete is True
    assert resolution.interpreter_used is False
    assert resolution.openai_requests == 0


def test_canonical_calendar_read_keeps_interpreter_for_incomplete_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_run_typed_sdk_agent(**kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="read",
                read_scope="filtered_window",
                query="medical appointment",
                query_source_text="medical appointment",
                date_scope="tomorrow",
                date_source_text="tomorrow",
            )
        )

    monkeypatch.setattr(interpreter, "run_typed_sdk_agent", fake_run_typed_sdk_agent)
    resolution = interpreter.resolve_calendar_action_plan(
        "Find my medical appointment tomorrow.",
        None,
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="context_lookup",
            task_objective="context_lookup",
            provider_system="google_calendar",
            provider_operations=["read"],
            provider_read_scope="bounded_collection",
            provider_result_mode="items",
            primary_target="medical appointment",
            # Missing read_only authority: retain the Calendar interpreter.
        ),
        live=True,
        today=date(2026, 7, 27),
    )

    assert resolution.plan is not None
    assert resolution.interpreter_used is True
    assert resolution.openai_requests == 1
    assert len(calls) == 1


def test_complete_calendar_delete_uses_model_owned_identity_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        'Delete both calendar events titled "Expert Initial Interview: Anup Sharma" '
        "on August 3, 2026 at 10:15 AM."
    )
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="delete",
                operation_source_text="Delete both calendar events",
                event_reference="Expert Initial Interview: Anup Sharma",
                event_reference_source_text=(
                    'calendar events titled "Expert Initial Interview: Anup Sharma"'
                ),
                event_reference_date="2026-08-03",
                event_reference_date_source_text="August 3, 2026",
                event_reference_time="10:15",
                event_reference_time_source_text="10:15 AM",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        infer_calendar_action_plan(request, today=date(2026, 7, 29)),
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="business_system_write",
            task_objective="business_system_write",
            provider_system="google_calendar",
            provider_operations=["delete"],
            primary_target="Expert Initial Interview: Anup Sharma",
            desired_count=2,
            desired_count_explicit=True,
            ask_shape={"permission_state": "approval_required"},
        ),
        live=True,
        today=date(2026, 7, 29),
    )

    assert resolution.plan is not None
    assert resolution.plan.operation == "delete"
    assert resolution.plan.event_reference == "Expert Initial Interview: Anup Sharma"
    assert resolution.plan.event_reference_date == "2026-08-03"
    assert resolution.plan.start_time == ""
    assert resolution.plan.event_reference_time == "10:15"
    assert resolution.plan.target_count == 2
    assert resolution.plan.calendar_scope == "all_readable"
    assert resolution.plan.complete is True
    assert resolution.interpreter_used is True
    assert resolution.openai_requests == 1


@pytest.mark.parametrize(
    "manual_plan",
    [
        ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="business_system_write",
            provider_system="google_calendar",
            provider_operations=["delete"],
            primary_target="Different event",
        ),
        ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="business_system_write",
            provider_system="google_calendar",
            provider_operations=["update"],
            primary_target="Expert Initial Interview: Anup Sharma",
        ),
        ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="business_system_write",
            provider_system="google_calendar",
            provider_operations=["delete"],
            primary_target="Expert Initial Interview: Anup Sharma",
            ask_shape={"permission_state": "read_only"},
        ),
    ],
)
def test_canonical_calendar_delete_reuse_rejects_planner_disagreement(
    manual_plan: ManualRequestPlan,
) -> None:
    request = (
        'Delete the calendar event titled "Expert Initial Interview: Anup Sharma" '
        "on August 3, 2026 at 10:15 AM."
    )

    assert (
        interpreter.canonical_calendar_mutation_plan(
            request,
            manual_plan=manual_plan,
            today=date(2026, 7, 29),
        )
        is None
    )


def test_canonical_create_recomputes_blockers_after_title_contains_delete() -> None:
    request = (
        "Add this event to Google Calendar: "
        "Topic: KBA_TEST_CALENDAR Compact Delete Latency Check. "
        "Time: Aug 11, 2026 2:35 PM Eastern Time."
    )
    fallback = infer_calendar_action_plan(request, today=date(2026, 7, 29))

    assert fallback is not None
    assert fallback.operation == "delete"
    assert fallback.title == "KBA_TEST_CALENDAR Compact Delete Latency Check"
    assert fallback.blockers == ("event name or exact event id",)

    authorized = interpreter._calendar_fallback_for_authorized_operations(
        fallback,
        allowed_operations=frozenset({"create"}),
    )

    assert authorized is not None
    assert authorized.operation == "create"
    assert authorized.title == "KBA_TEST_CALENDAR Compact Delete Latency Check"
    assert authorized.start_date == "2026-08-11"
    assert authorized.start_time == "14:35"
    assert authorized.complete is True
    assert authorized.blockers == ()


def test_live_create_uses_model_operation_when_title_contains_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "Add this event to Google Calendar: "
        "Topic: KBA_TEST_CALENDAR Compact Delete Latency Check. "
        "Time: Aug 11, 2026 2:35 PM Eastern Time."
    )
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="create",
                operation_source_text="Add this event to Google Calendar",
                title="KBA_TEST_CALENDAR Compact Delete Latency Check",
                title_source_text=(
                    "Topic: KBA_TEST_CALENDAR Compact Delete Latency Check"
                ),
                start_date="2026-08-11",
                date_source_text="Aug 11, 2026",
                start_time="14:35",
                time_source_text="2:35 PM",
                timezone="America/New_York",
                timezone_source_text="Eastern Time",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        infer_calendar_action_plan(request, today=date(2026, 7, 29)),
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="business_system_write",
            task_objective="business_system_write",
            provider_system="google_calendar",
            provider_operations=["create"],
            primary_target="KBA_TEST_CALENDAR Compact Delete Latency Check",
            ask_shape={"permission_state": "approval_required"},
        ),
        live=True,
        today=date(2026, 7, 29),
    )

    assert resolution.plan is not None
    assert resolution.plan.operation == "create"
    assert resolution.plan.title == "KBA_TEST_CALENDAR Compact Delete Latency Check"
    assert resolution.plan.start_date == "2026-08-11"
    assert resolution.plan.start_time == "14:35"
    assert resolution.plan.event_reference == ""
    assert resolution.plan.complete is True
    assert resolution.interpreter_used is True
    assert resolution.openai_requests == 1


def test_live_create_treats_same_event_note_as_one_model_owned_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "Add this event to Google Calendar: "
        "Topic: KBA_TEST_CALENDAR Compact Delete Latency Check. "
        "Time: Aug 11, 2026 2:35 PM Eastern Time. "
        "Also, add this to the notes: Synthetic KBA latency validation event."
    )
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="create",
                operation_source_text="Add this event to Google Calendar",
                title="KBA_TEST_CALENDAR Compact Delete Latency Check",
                title_source_text=(
                    "Topic: KBA_TEST_CALENDAR Compact Delete Latency Check"
                ),
                start_date="2026-08-11",
                date_source_text="Aug 11, 2026",
                start_time="14:35",
                time_source_text="2:35 PM",
                timezone="America/New_York",
                timezone_source_text="Eastern Time",
                description_from_payload=True,
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        infer_calendar_action_plan(request, today=date(2026, 7, 29)),
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="business_system_write",
            task_objective="business_system_write",
            provider_system="google_calendar",
            provider_operations=["create", "update"],
            provider_action_steps=[
                {"operation": "create", "resource_type": "calendar_event"},
                {"operation": "update", "resource_type": "calendar_event"},
            ],
            primary_target="KBA_TEST_CALENDAR Compact Delete Latency Check",
            ask_shape={"permission_state": "approval_required"},
        ),
        live=True,
        today=date(2026, 7, 29),
    )

    assert resolution.plan is not None
    assert resolution.plan.operation == "create"
    assert resolution.plan.title == "KBA_TEST_CALENDAR Compact Delete Latency Check"
    assert resolution.plan.description == "Synthetic KBA latency validation event."
    assert resolution.plan.complete is True
    assert resolution.interpreter_used is True
    assert resolution.openai_requests == 1


@pytest.mark.parametrize(
    ("operations", "expected"),
    [
        (["read"], "read"),
        (["read", "create"], "create"),
        (["create", "update"], "create"),
        (["read", "delete"], "delete"),
        (["update"], "update"),
        (["create", "delete"], None),
        (["delete", "update"], None),
    ],
)
def test_canonical_calendar_primary_operation_uses_typed_plan_sequence(
    operations: list[str],
    expected: str | None,
) -> None:
    authority = ExecutionIntentAuthority.from_value(
        ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="business_system_write",
            provider_system="google_calendar",
            provider_operations=operations,
        )
    )

    assert interpreter._canonical_calendar_primary_operation(authority) == expected


@pytest.mark.parametrize(
    "operations",
    [
        ["create", "delete"],
        ["delete", "update"],
        ["update"],
    ],
)
def test_canonical_calendar_mutation_does_not_collapse_distinct_mutation_sequences(
    operations: list[str],
) -> None:
    request = (
        "Add this event to Google Calendar: "
        "Topic: KBA_TEST_CALENDAR Lifecycle Check. "
        "Time: Aug 11, 2026 2:35 PM Eastern Time."
    )

    assert (
        interpreter.canonical_calendar_mutation_plan(
            request,
            manual_plan=ManualRequestPlan(
                source="llm",
                target_agent="chief_of_staff",
                intent="business_system_write",
                provider_system="google_calendar",
                provider_operations=operations,
                primary_target="KBA_TEST_CALENDAR Lifecycle Check",
            ),
            today=date(2026, 7, 29),
        )
        is None
    )


def test_contextual_calendar_delete_keeps_interpreter() -> None:
    assert (
        interpreter.canonical_calendar_mutation_plan(
            "Delete the calendar event you just created in this thread.",
            manual_plan=ManualRequestPlan(
                source="llm",
                target_agent="chief_of_staff",
                intent="business_system_write",
                provider_system="google_calendar",
                provider_operations=["delete"],
                primary_target="KBA_TEST_CALENDAR Meeting Invite Format Check",
            ),
            today=date(2026, 7, 29),
        )
        is None
    )


def test_current_filtered_calendar_followup_discards_prior_next_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "business agents continue this prior Slack thread. "
        "Previous request: What is first calendar event tomorrow? "
        "Previous result: The first event is KNI Calendar Today. "
        "User follow-up: List the flight event tomorrow "
        "Continue the same agent task."
    )
    stale_plan = CalendarActionPlan(
        operation="read",
        read_scope="time_window",
        read_selection="next",
        date_scope="tomorrow",
        start_date="2026-07-22",
        complete=True,
    )

    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="read",
                operation_source_text="List the flight event tomorrow",
                read_scope="filtered_window",
                read_selection="next",
                read_selection_source_text="first calendar event",
                date_scope="tomorrow",
                date_source_text="tomorrow",
                query="flight",
                query_source_text="flight event",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        stale_plan,
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="context_lookup",
            task_objective="context_lookup",
            provider_system="google_calendar",
            provider_operations=["read"],
            provider_read_scope="bounded_collection",
            primary_target="flight event tomorrow",
        ),
        live=True,
        today=date(2026, 7, 21),
    )

    assert resolution.plan is not None
    assert resolution.plan.read_scope == "filtered_window"
    assert resolution.plan.query == "flight"
    assert resolution.plan.read_selection == "all"
    assert resolution.plan.date_scope == "tomorrow"
    assert resolution.plan.start_date == "2026-07-22"
    assert resolution.plan.calendar_scope == "all_readable"
    assert resolution.plan.event_reference == ""
    assert resolution.plan.complete is True


def test_all_events_uses_all_readable_calendar_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "business agents continue this prior Slack thread. "
        "Previous request: List flight events tomorrow from every Google Calendar I can read. "
        "User follow-up: List all my events tomorrow "
        "Continue the same agent task."
    )
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="read",
                operation_source_text="List all my events tomorrow",
                read_scope="filtered_window",
                read_selection="all",
                calendar_scope="selected_readable",
                calendar_scope_source_text="all",
                query="all my events",
                query_source_text="all my events",
                date_scope="tomorrow",
                date_source_text="tomorrow",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        None,
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="context_lookup",
            task_objective="context_lookup",
            provider_system="google_calendar",
            provider_operations=["read"],
            provider_read_scope="bounded_collection",
            provider_result_mode="items",
            primary_target="events tomorrow",
        ),
        live=True,
        today=date(2026, 7, 21),
    )

    assert resolution.plan is not None
    assert resolution.plan.read_scope == "time_window"
    assert resolution.plan.read_selection == "all"
    assert resolution.plan.query == ""
    assert resolution.plan.calendar_scope == "all_readable"
    assert resolution.plan.start_date == "2026-07-22"
    assert resolution.plan.complete is True


def test_replayed_read_only_calendar_plan_cannot_admit_create_operation() -> None:
    fallback = infer_calendar_action_plan(
        'Create "Architecture review" on my calendar on July 30.',
        today=date(2026, 7, 25),
    )
    assert fallback is not None
    assert fallback.operation == "create"

    resolution = interpreter.resolve_calendar_action_plan(
        'Create "Architecture review" on my calendar on July 30.',
        fallback,
        manual_plan=ManualRequestPlan(
            source="canonical:replay_fixture",
            target_agent="chief_of_staff",
            intent="business_system_write",
            task_objective="business_system_write",
            provider_system="google_calendar",
            provider_operations=["read", "create"],
            ask_shape={"permission_state": "read_only"},
        ),
        live=False,
        today=date(2026, 7, 25),
    )

    assert resolution.plan is not None
    assert resolution.plan.operation == "read"


def test_explicit_every_readable_calendar_scope_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = "List all events tomorrow from every Google Calendar I can read."
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="read",
                operation_source_text=request,
                read_scope="time_window",
                read_selection="all",
                calendar_scope="configured",
                date_scope="tomorrow",
                date_source_text="tomorrow",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        None,
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="context_lookup",
            task_objective="context_lookup",
            provider_system="google_calendar",
            provider_operations=["read"],
            provider_read_scope="bounded_collection",
            provider_result_mode="items",
            primary_target="events tomorrow",
        ),
        live=True,
        today=date(2026, 7, 21),
    )

    assert resolution.plan is not None
    assert resolution.plan.read_scope == "time_window"
    assert resolution.plan.query == ""
    assert resolution.plan.calendar_scope == "all_readable"
    assert resolution.plan.start_date == "2026-07-22"
    assert resolution.plan.complete is True


def test_explicit_calendar_read_time_range_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "Read all Google Calendars and list every event on August 20, 2026 "
        "between 2:00 PM and 4:30 PM Eastern. Do not change anything."
    )
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="read",
                operation_source_text=request,
                read_scope="time_window",
                read_selection="all",
                calendar_scope="all_readable",
                calendar_scope_source_text="all Google Calendars",
                date_scope="specific_date",
                start_date="2026-08-20",
                date_source_text="August 20, 2026",
                start_time="14:00",
                end_time="16:30",
                time_source_text="between 2:00 PM and 4:30 PM",
                timezone="America/New_York",
                timezone_source_text="Eastern",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        None,
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="context_lookup",
            task_objective="context_lookup",
            provider_system="google_calendar",
            provider_operations=["read"],
            provider_read_scope="bounded_collection",
            provider_result_mode="items",
            primary_target="events between 2:00 PM and 4:30 PM",
            ask_shape={"permission_state": "read_only"},
        ),
        live=True,
        today=date(2026, 7, 29),
    )

    assert resolution.plan is not None
    assert resolution.plan.complete is True
    assert resolution.plan.start_date == "2026-08-20"
    assert resolution.plan.start_time == "14:00"
    assert resolution.plan.end_time == "16:30"
    assert resolution.plan.calendar_scope == "all_readable"


def test_explicit_selected_shared_calendar_scope_remains_selected_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = "List tomorrow's events from my selected shared calendars."
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="read",
                operation_source_text=request,
                read_scope="time_window",
                read_selection="all",
                calendar_scope="configured",
                date_scope="tomorrow",
                date_source_text="tomorrow",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        None,
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="context_lookup",
            task_objective="context_lookup",
            provider_system="google_calendar",
            provider_operations=["read"],
            provider_read_scope="bounded_collection",
            provider_result_mode="items",
            primary_target="events tomorrow",
        ),
        live=True,
        today=date(2026, 7, 21),
    )

    assert resolution.plan is not None
    assert resolution.plan.calendar_scope == "selected_readable"
    assert resolution.plan.complete is True


def test_unspecified_calendar_read_scope_defaults_to_all_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = "Find my medical appointment tomorrow."
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarActionInterpretation(
                operation="read",
                operation_source_text=request,
                read_scope="filtered_window",
                query="medical appointment",
                query_source_text="medical appointment",
                calendar_scope="configured",
                date_scope="tomorrow",
                date_source_text="tomorrow",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_action_plan(
        request,
        None,
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="context_lookup",
            task_objective="context_lookup",
            provider_system="google_calendar",
            provider_operations=["read"],
            provider_read_scope="bounded_collection",
            provider_result_mode="items",
            primary_target="medical appointment tomorrow",
        ),
        live=True,
        today=date(2026, 7, 27),
    )

    assert resolution.plan is not None
    assert resolution.plan.calendar_scope == "all_readable"
    assert resolution.plan.query == "medical appointment"
    assert resolution.plan.start_date == "2026-07-28"


def test_calendar_lookup_synthesis_selects_only_bounded_event_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_input: dict[str, object] = {}

    def _run_typed_sdk_agent(**kwargs: object) -> SimpleNamespace:
        typed_input = kwargs["typed_input"]
        captured_input["lookup_target"] = typed_input.lookup_target
        captured_input["response_scope"] = typed_input.response_scope
        captured_input["events"] = typed_input.events
        return SimpleNamespace(
            output=CalendarLookupSynthesis(
                status="matched",
                selected_event_indexes=[1],
                selection_reason=(
                    "The event is on the Care Appointments calendar and is the only "
                    "medical-domain candidate."
                ),
            )
        )

    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        _run_typed_sdk_agent,
    )

    resolution = interpreter.resolve_calendar_lookup_synthesis(
        "Find my medical appointment tomorrow.",
        [
            {
                "title": "Window Washing",
                "start_date": "2026-07-28",
                "start_time": "08:30",
                "end_time": "09:30",
                "source_calendar_name": "",
            },
            {
                "title": "Example Medical Clinic",
                "start_date": "2026-07-28",
                "start_time": "14:40",
                "end_time": "15:40",
                "display_start_date": "2026-07-28",
                "display_start_time": "10:40",
                "display_end_date": "2026-07-28",
                "display_end_time": "11:40",
                "display_timezone": "America/New_York",
                "location": "Example Medical Center",
                "source_calendar_name": "Care Appointments",
                "description": "Private event notes must not reach synthesis.",
                "attendees": [{"email": "private@example.test"}],
                "event_id": "provider-private-id",
            },
        ],
        lookup_target="medical appointment tomorrow",
        response_scope="focused",
        live=True,
    )

    assert resolution.synthesis is not None
    assert resolution.synthesis.selected_event_indexes == [1]
    assert resolution.openai_requests == 1
    assert captured_input["lookup_target"] == "medical appointment tomorrow"
    assert captured_input["response_scope"] == "focused"
    bounded_events = captured_input["events"]
    assert isinstance(bounded_events, list)
    assert bounded_events[1]["location"] == "Example Medical Center"
    assert bounded_events[1]["source_calendar_name"] == "Care Appointments"
    assert bounded_events[1]["start_time"] == "10:40"
    assert bounded_events[1]["end_time"] == "11:40"
    assert bounded_events[1]["display_timezone"] == "America/New_York"
    assert "description" not in bounded_events[1]
    assert "attendees" not in bounded_events[1]
    assert "event_id" not in bounded_events[1]


def test_calendar_lookup_answer_renders_one_model_selected_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        interpreter,
        "resolve_calendar_lookup_synthesis",
        lambda *_args, **_kwargs: interpreter.CalendarLookupSynthesisResolution(
            synthesis=CalendarLookupSynthesis(
                status="matched",
                selected_event_indexes=[1],
                selection_reason=(
                    "The Care Appointments calendar supplies the strongest match."
                ),
            ),
            openai_requests=1,
        ),
    )
    events = [
        {
            "title": "Window Washing",
            "start_date": "2026-07-28",
            "start_time": "08:30",
            "end_time": "09:30",
        },
        {
            "title": "Example Family Medicine",
            "start_date": "2026-07-28",
            "start_time": "10:40",
            "end_time": "11:40",
            "source_calendar_name": "Care Appointments",
            "source_calendar_primary": False,
        },
    ]

    resolution = interpreter.resolve_calendar_lookup_answer(
        "Find my medical appointment tomorrow.",
        events,
        lookup_target="medical appointment tomorrow",
        response_scope="focused",
        fallback="Raw full-day agenda",
        live=True,
    )

    assert resolution.text == (
        'I found one matching event: "Example Family Medicine" on the Care Appointments '
        "calendar on 2026-07-28, from 10:40 AM to 11:40 AM. "
        "No location is listed in the Calendar event."
    )
    assert resolution.status == "matched"
    assert resolution.selected_event_indexes == (1,)
    assert resolution.openai_requests == 1
    assert "Window Washing" not in resolution.text


def test_focused_calendar_lookup_does_not_fall_back_to_raw_agenda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        interpreter,
        "resolve_calendar_lookup_synthesis",
        lambda *_args, **_kwargs: interpreter.CalendarLookupSynthesisResolution(
            synthesis=None,
            openai_requests=0,
            warnings=("Synthetic selector unavailable.",),
        ),
    )

    resolution = interpreter.resolve_calendar_lookup_answer(
        "Find one appointment.",
        [{"title": "Unrelated event"}],
        lookup_target="appointment",
        response_scope="focused",
        fallback="Raw agenda that must not be returned",
        live=True,
    )

    assert resolution.status == "fallback"
    assert "could not confidently identify one matching event" in resolution.text
    assert "Raw agenda" not in resolution.text
    assert resolution.warnings == ("Synthetic selector unavailable.",)


def test_full_window_calendar_lookup_skips_model_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        interpreter,
        "resolve_calendar_lookup_synthesis",
        lambda *_args, **_kwargs: pytest.fail(
            "A full-window Calendar request must not spend a selector call."
        ),
    )

    resolution = interpreter.resolve_calendar_lookup_answer(
        "List every event tomorrow.",
        [{"title": "Morning review"}],
        lookup_target="events tomorrow",
        response_scope="full_window",
        fallback="Complete provider agenda",
        live=True,
    )

    assert resolution.text == "Complete provider agenda"
    assert resolution.openai_requests == 0


def test_calendar_lookup_answer_groups_two_records_for_one_appointment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        interpreter,
        "resolve_calendar_lookup_synthesis",
        lambda *_args, **_kwargs: interpreter.CalendarLookupSynthesisResolution(
            synthesis=CalendarLookupSynthesis(
                status="matched",
                selected_event_indexes=[0, 1],
                related_event_groups=[[0, 1]],
                selection_reason=(
                    "The overlapping medical records appear to describe one visit."
                ),
            ),
            openai_requests=1,
        ),
    )
    events = [
        {
            "title": "Example Medical Clinic",
            "display_start_date": "2026-07-28",
            "display_start_time": "10:40",
            "display_end_date": "2026-07-28",
            "display_end_time": "11:40",
            "display_timezone": "America/New_York",
            "source_calendar_name": "Care Appointments",
            "source_calendar_primary": False,
        },
        {
            "title": "Established Client Visit",
            "start_date": "2026-07-28",
            "start_time": "14:25",
            "end_time": "15:00",
            "display_start_date": "2026-07-28",
            "display_start_time": "10:25",
            "display_end_date": "2026-07-28",
            "display_end_time": "11:00",
            "display_timezone": "America/New_York",
            "location": "100 Example Avenue, Exampleville, PA 19000",
            "source_calendar_name": "Appointments",
            "source_calendar_primary": False,
        },
        {
            "title": "Window Washing",
            "display_start_date": "2026-07-28",
            "display_start_time": "08:30",
            "display_end_time": "09:30",
        },
    ]

    resolution = interpreter.resolve_calendar_lookup_answer(
        "Find my medical appointment tomorrow. What is its time and location?",
        events,
        lookup_target="medical appointment tomorrow",
        response_scope="focused",
        fallback="Raw agenda",
        live=True,
    )

    assert resolution.status == "matched"
    assert resolution.selected_event_indexes == (0, 1)
    assert resolution.related_event_groups == ((0, 1),)
    assert "one likely event represented by 2 Calendar entries" in resolution.text
    assert "10:40 AM to 11:40 AM" in resolution.text
    assert "10:25 AM to 11:00 AM" in resolution.text
    assert "100 Example Avenue" in resolution.text
    assert "different times" in resolution.text
    assert "Window Washing" not in resolution.text
    assert "2:25 PM" not in resolution.text


def test_calendar_lookup_answer_lists_distinct_matches_without_single_match_caveat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        interpreter,
        "resolve_calendar_lookup_synthesis",
        lambda *_args, **_kwargs: interpreter.CalendarLookupSynthesisResolution(
            synthesis=CalendarLookupSynthesis(
                status="matched",
                selected_event_indexes=[0, 1],
                selection_reason="Both visits answer the plural request.",
            ),
            openai_requests=1,
        ),
    )

    resolution = interpreter.resolve_calendar_lookup_answer(
        "List my two medical appointments tomorrow.",
        [
            {
                "title": "Visit A",
                "display_start_date": "2026-07-28",
                "display_start_time": "09:00",
                "display_end_time": "09:30",
                "location": "Clinic A",
            },
            {
                "title": "Visit B",
                "display_start_date": "2026-07-28",
                "display_start_time": "14:00",
                "display_end_time": "14:30",
                "location": "Clinic B",
            },
        ],
        lookup_target="medical appointments tomorrow",
        response_scope="focused",
        fallback="Raw agenda",
        live=True,
    )

    assert resolution.text.startswith("I found 2 matching events:")
    assert "Location: Clinic A." in resolution.text
    assert "Location: Clinic B." in resolution.text
    assert "one best match" not in resolution.text


def test_invalid_related_group_does_not_discard_valid_event_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarLookupSynthesis(
                status="matched",
                selected_event_indexes=[0],
                related_event_groups=[[0, 1]],
                selection_reason="The first event is the supported match.",
            )
        ),
    )

    resolution = interpreter.resolve_calendar_lookup_synthesis(
        "Find my appointment.",
        [{"title": "Supported match"}, {"title": "Other event"}],
        lookup_target="appointment",
        response_scope="focused",
        live=True,
    )

    assert resolution.synthesis is not None
    assert resolution.synthesis.selected_event_indexes == [0]
    assert resolution.synthesis.related_event_groups == []
    assert "selection was retained" in resolution.warnings[0]
