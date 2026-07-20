from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from keystone_agents.agents import calendar_action_interpreter as interpreter
from keystone_agents.calendar_actions import (
    CalendarActionPlan,
    calendar_interpretation_context,
    compact_calendar_interpretation_request,
    infer_calendar_action_plan,
    is_calendar_action_candidate,
)
from keystone_agents.schemas.calendar_action import CalendarActionInterpretation

REQUEST = (
    "add this event to the calendar: "
    "\u201cLivestream with Corey Ching and Peter Steinberger\u201d July 14 at 2pm EDT"
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
