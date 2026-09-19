from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

import keystone_agents.cli as cli
import keystone_agents.slack_actions as slack_actions
from keystone_agents.schemas.calendar_action import CalendarActionInterpretation
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan


def _calendar_manual_plan(
    operation: str,
    request: str,
    *,
    read_scope: str = "bounded_collection",
) -> ManualRequestPlan:
    read_only = operation == "read"
    return ManualRequestPlan(
        source="llm",
        target_agent="chief_of_staff",
        intent="context_lookup" if read_only else "business_system_write",
        task_objective="context_lookup" if read_only else "business_system_write",
        provider_system="google_calendar",
        provider_operations=[operation],
        provider_read_scope=read_scope if read_only else "single_item",
        provider_result_mode="items",
        primary_target="calendar events",
        objective=request,
        ask_shape={
            "permission_state": "read_only" if read_only else "approval_required"
        },
    )


def _resolve_calendar_turn(
    monkeypatch: pytest.MonkeyPatch,
    request: str,
    interpretation: CalendarActionInterpretation,
    *,
    verified_objects: tuple[object, ...] = (),
    read_scope: str = "bounded_collection",
) -> cli.CalendarActionPlan:
    monkeypatch.setattr(
        "keystone_agents.agents.calendar_action_interpreter.run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(output=interpretation),
    )
    resolution = cli.resolve_calendar_action_plan(
        request,
        cli.infer_calendar_action_plan(request, today=date(2026, 7, 30)),
        manual_plan=_calendar_manual_plan(
            interpretation.operation,
            request,
            read_scope=read_scope,
        ),
        live=True,
        today=date(2026, 7, 30),
        verified_objects=verified_objects,
    )
    assert resolution.plan is not None
    assert resolution.plan.complete is True
    return resolution.plan


def test_six_ask_slack_calendar_lifecycle_preserves_exact_object_and_thread(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'calendar-slack-lifecycle.db'}"
    team_id = "T-CALENDAR"
    channel_id = "C-CALENDAR"
    read_thread_ts = "1786000000.000100"
    lifecycle_thread_ts = "1786000100.000100"
    event_id = "event_fresh_thread_123"
    event = {
        "event_id": event_id,
        "calendar_id": "primary",
        "title": "KBA_TEST_CALENDAR Fresh Thread Continuity 20260915",
        "start_date": "2026-09-15",
        "start_time": "14:35",
        "end_time": "15:05",
        "timezone": "America/New_York",
        "all_day": False,
        "repeat_each_day": False,
        "description": "Synthetic KBA fresh-thread continuity validation.",
        "deleted": False,
    }
    provider_calls: list[tuple[str, str]] = []
    read_bounds: dict[str, object] = {}

    def fake_read_window(time_min: str, time_max: str, **kwargs: object) -> dict:
        read_bounds.update(
            {"time_min": time_min, "time_max": time_max, **kwargs}
        )
        return {
            "status": "success",
            "operation": "read_calendar_window",
            "events": [
                {
                    "event_id": "inside_window",
                    "title": "Inside requested window",
                    "display_start_date": "2026-09-14",
                    "display_start_time": "15:00",
                    "display_end_time": "15:30",
                }
            ],
        }

    def fake_create(title: str, start_date: str, **kwargs: object) -> dict:
        provider_calls.append(("create", event_id))
        event.update(
            {
                "title": title,
                "start_date": start_date,
                "start_time": str(kwargs.get("start_time") or ""),
                "end_time": str(kwargs.get("end_time") or ""),
                "description": str(kwargs.get("description") or ""),
            }
        )
        return {
            "status": "success",
            "operation": "create_calendar_event",
            **event,
            "verification": {
                "status": "verified",
                "passed": True,
                "start_match": True,
                "end_match": True,
                "all_day_match": True,
                "timezone_match": True,
                "recurrence_match": True,
                "description_match": True,
            },
            "send_enabled": False,
        }

    def fake_update(resolved_event_id: str, **kwargs: object) -> dict:
        assert resolved_event_id == event_id
        provider_calls.append(("update", resolved_event_id))
        for key in ("title", "start_date", "start_time", "end_time"):
            value = str(kwargs.get(key) or "").strip()
            if value:
                event[key] = value
        description = str(kwargs.get("description") or "").strip()
        append_description = bool(kwargs.get("append_description"))
        if description:
            event["description"] = (
                f"{event['description']}\n{description}".strip()
                if append_description
                else description
            )
        return {
            "status": "success",
            "operation": "update_calendar_event",
            **event,
            "description_mode": "append" if append_description else "replace",
            "verification": {
                "status": "verified",
                "passed": True,
                "start_match": True,
                "end_match": True,
                "all_day_match": True,
                "timezone_match": True,
                "description_match": True,
            },
            "send_enabled": False,
        }

    def fake_read_event(resolved_event_id: str, **kwargs: object) -> dict:
        assert resolved_event_id == event_id
        assert kwargs["include_description"] is True
        provider_calls.append(("read", resolved_event_id))
        return {
            "status": "success",
            "operation": "read_calendar_event",
            **event,
            "display_start_date": event["start_date"],
            "display_start_time": event["start_time"],
            "display_end_time": event["end_time"],
            "found": True,
            "verification": {
                "status": "verified_present",
                "passed": True,
            },
            "send_enabled": False,
        }

    def fake_delete(resolved_event_id: str, **_kwargs: object) -> dict:
        assert resolved_event_id == event_id
        provider_calls.append(("delete", resolved_event_id))
        event["deleted"] = True
        return {
            "status": "success",
            "operation": "delete_calendar_event",
            **event,
            "verification": {
                "status": "verified_absent",
                "passed": True,
                "event_absent_after": True,
            },
            "send_enabled": False,
        }

    monkeypatch.setattr(cli, "read_google_calendar_window_impl", fake_read_window)
    monkeypatch.setattr(cli, "read_google_calendar_event_impl", fake_read_event)
    monkeypatch.setattr(cli, "create_google_calendar_event_impl", fake_create)
    monkeypatch.setattr(cli, "update_google_calendar_event_impl", fake_update)
    monkeypatch.setattr(cli, "delete_google_calendar_event_impl", fake_delete)

    read_request = (
        "CoS, read all Google Calendars I can access and list only the events on "
        "September 14, 2026 between 2:00 PM and 4:30 PM Eastern. "
        "Do not change anything."
    )
    read_plan = _resolve_calendar_turn(
        monkeypatch,
        read_request,
        CalendarActionInterpretation(
            operation="read",
            operation_source_text=read_request,
            read_scope="time_window",
            read_selection="all",
            calendar_scope="all_readable",
            calendar_scope_source_text="all Google Calendars I can access",
            date_scope="specific_date",
            start_date="2026-09-14",
            date_source_text="September 14, 2026",
            start_time="14:00",
            end_time="16:30",
            time_source_text="between 2:00 PM and 4:30 PM",
            timezone="America/New_York",
            timezone_source_text="Eastern",
        ),
    )
    read_payload = cli.execute_direct_calendar_action(
        read_request,
        read_plan,
        live=True,
        now=datetime.fromisoformat("2026-07-30T12:00:00-04:00"),
    )
    assert read_payload["status"] == "done"
    assert read_bounds["time_min"] == "2026-09-14T14:00:00-04:00"
    assert read_bounds["time_max"] == "2026-09-14T16:30:00-04:00"
    assert read_bounds["calendar_scope"] == "all_readable"
    assert "Inside requested window" in read_payload["slack_display_text"]
    assert read_payload["side_effects"]["calendar_write_performed"] is False

    def run_persisted_turn(
        request: str,
        plan: cli.CalendarActionPlan,
        request_ts: str,
    ) -> dict:
        exit_code = cli.run_direct_calendar_action(
            request,
            plan,
            live=True,
            json_output=True,
            database_url=database_url,
            execution_context={
                "slack_context": {
                    "team_id": team_id,
                    "channel_id": channel_id,
                    "thread_ts": lifecycle_thread_ts,
                    "request_ts": request_ts,
                }
            },
        )
        assert exit_code == 0
        return json.loads(capsys.readouterr().out)

    create_request = (
        "CoS, add this event to Google Calendar: Topic: "
        "KBA_TEST_CALENDAR Fresh Thread Continuity 20260915. "
        "Time: September 15, 2026 at 2:35 PM Eastern Time. "
        "End time: 3:05 PM Eastern Time. Add this note: "
        "Synthetic KBA fresh-thread continuity validation. No external attendees."
    )
    create_plan = _resolve_calendar_turn(
        monkeypatch,
        create_request,
        CalendarActionInterpretation(
            operation="create",
            operation_source_text="add this event",
            title=event["title"],
            title_source_text=f"Topic: {event['title']}",
            start_date="2026-09-15",
            date_source_text="September 15, 2026",
            start_time="14:35",
            end_time="15:05",
            time_source_text="2:35 PM to 3:05 PM",
            timezone="America/New_York",
            description=event["description"],
            description_source_text=f"Add this note: {event['description']}",
        ),
    )
    create_payload = run_persisted_turn(
        create_request, create_plan, "1786000101.000100"
    )
    assert create_payload["continuation_objects"][0]["object_id"] == event_id

    references = slack_actions.latest_verified_provider_objects_for_slack_thread(
        team_id=team_id,
        channel_id=channel_id,
        thread_ts=lifecycle_thread_ts,
        before_request_ts="1786000102.000100",
        database_url=database_url,
    )
    assert references[0].object_id == event_id

    move_request = (
        "Move the calendar event you just created in this thread to September 16, "
        "2026 at 3:20 PM Eastern. Keep the same duration and everything else unchanged."
    )
    move_plan = _resolve_calendar_turn(
        monkeypatch,
        move_request,
        CalendarActionInterpretation(
            operation="update",
            operation_source_text="Move",
            event_reference="the calendar event you just created in this thread",
            event_reference_source_text=(
                "the calendar event you just created in this thread"
            ),
            event_reference_from_thread_context=True,
            start_date="2026-09-16",
            date_source_text="September 16, 2026",
            start_time="15:20",
            end_time="15:50",
            time_source_text="3:20 PM; keep the same duration",
            timezone="America/New_York",
        ),
        verified_objects=references,
    )
    assert move_plan.event_id == event_id
    run_persisted_turn(move_request, move_plan, "1786000102.000100")

    references = slack_actions.latest_verified_provider_objects_for_slack_thread(
        team_id=team_id,
        channel_id=channel_id,
        thread_ts=lifecycle_thread_ts,
        before_request_ts="1786000103.000100",
        database_url=database_url,
    )
    assert references[0].object_id == event_id
    assert references[0].effective_date == "2026-09-16"

    rename_request = (
        "Orchestrator, rename that event to KBA_TEST_CALENDAR Fresh Thread "
        "Continuity Updated 20260916 and append this note: Verified same-object "
        "update after a cross-date move. Keep its date, time, duration, and other "
        "details unchanged."
    )
    execution_request = cli.build_execution_request(
        "\n".join(
            [
                "business agents continue this prior Slack thread.",
                "Prior task owner (advisory): chief_of_staff",
                f"Previous request: {move_request}",
                f"User follow-up: {rename_request}",
                "Continue the same agent task.",
            ]
        )
    )
    execution_request = cli._execution_request_with_workflow_verified_objects(
        execution_request,
        {
            "verified_provider_objects": [
                reference.model_dump(mode="json") for reference in references
            ]
        },
    )
    assert execution_request.requested_agent == "orchestrator"
    assert execution_request.continuation.prior_agent == "chief_of_staff"
    assert execution_request.continuation.verified_objects[0].object_id == event_id
    rename_plan = _resolve_calendar_turn(
        monkeypatch,
        execution_request.current_request,
        CalendarActionInterpretation(
            operation="update",
            operation_source_text="rename",
            event_reference="that event",
            event_reference_source_text="that event",
            event_reference_from_thread_context=True,
            title="KBA_TEST_CALENDAR Fresh Thread Continuity Updated 20260916",
            title_source_text=(
                "KBA_TEST_CALENDAR Fresh Thread Continuity Updated 20260916"
            ),
            description="Verified same-object update after a cross-date move.",
            description_source_text=(
                "append this note: Verified same-object update after a cross-date move."
            ),
            description_mode="append",
        ),
        verified_objects=execution_request.continuation.verified_objects,
    )
    assert rename_plan.event_id == event_id
    run_persisted_turn(rename_request, rename_plan, "1786000103.000100")

    references = slack_actions.latest_verified_provider_objects_for_slack_thread(
        team_id=team_id,
        channel_id=channel_id,
        thread_ts=lifecycle_thread_ts,
        before_request_ts="1786000103.700100",
        database_url=database_url,
    )
    exact_read_request = (
        "Read that same event and report its exact title, date, start time, "
        "end time, and description. Do not change anything."
    )
    exact_read_plan = _resolve_calendar_turn(
        monkeypatch,
        exact_read_request,
        CalendarActionInterpretation(
            operation="read",
            operation_source_text="Read",
            event_reference="that same event",
            event_reference_source_text="that same event",
            event_reference_from_thread_context=True,
        ),
        verified_objects=references,
        read_scope="single_item",
    )
    assert exact_read_plan.event_id == event_id
    exact_read_payload = run_persisted_turn(
        exact_read_request,
        exact_read_plan,
        "1786000103.700100",
    )
    assert exact_read_payload["tool_receipt"]["found"] is True
    assert event["title"] in exact_read_payload["slack_display_text"]
    assert event["description"] in exact_read_payload["slack_display_text"]

    delete_request = "Delete the calendar event you just updated in this thread."
    thread_messages = [
        {"ts": "1786000101.000100", "user": "U-OPERATOR", "text": create_request},
        {
            "ts": "1786000101.500100",
            "bot_id": "B-KNI",
            "subtype": "bot_message",
            "text": "Google Calendar event created and verified.",
        },
        {"ts": "1786000102.000100", "user": "U-OPERATOR", "text": move_request},
        {
            "ts": "1786000102.500100",
            "bot_id": "B-KNI",
            "subtype": "bot_message",
            "text": "Google Calendar event moved and verified.",
        },
        {"ts": "1786000103.000100", "user": "U-OPERATOR", "text": rename_request},
        {
            "ts": "1786000103.500100",
            "bot_id": "B-KNI",
            "subtype": "bot_message",
            "text": "Google Calendar event renamed and verified.",
        },
        {"ts": "1786000104.000100", "user": "U-OPERATOR", "text": delete_request},
    ]
    selected_context = slack_actions.build_selected_message_context(
        {
            "type": "message_action",
            "callback_id": slack_actions.RUN_AGENT_MESSAGE_CALLBACK_ID,
            "team": {"id": team_id, "domain": "kni"},
            "channel": {"id": channel_id, "name": "ai-agents-workflow"},
            "message": {
                "ts": "1786000104.000100",
                "thread_ts": lifecycle_thread_ts,
                "user": "U-OPERATOR",
                "text": delete_request,
            },
        },
        thread_messages=thread_messages,
    )
    workflow_state = slack_actions.orchestrator_workflow_state_from_slack_context(
        selected_context,
        request_text=delete_request,
        database_url=database_url,
    )
    workflow_state = cli._attach_verified_provider_scope_from_slack_thread(
        workflow_state,
        database_url=database_url,
    )
    assert [item["role"] for item in workflow_state["recent_slack_thread"]] == [
        "operator",
        "agent",
        "operator",
        "agent",
        "operator",
        "agent",
        "operator",
    ]
    assert cli._latest_distinct_slack_operator_turn(
        workflow_state,
        current_request=delete_request,
    ) == rename_request
    newest_object = workflow_state["verified_provider_objects"][0]
    assert newest_object["object_id"] == event_id
    assert newest_object["display_name"] == (
        "KBA_TEST_CALENDAR Fresh Thread Continuity Updated 20260916"
    )

    delete_execution_request = cli._execution_request_with_workflow_verified_objects(
        cli.build_execution_request(
            "\n".join(
                [
                    "business agents continue this prior Slack thread.",
                    "Prior task owner (advisory): orchestrator",
                    f"Previous request: {rename_request}",
                    f"User follow-up: {delete_request}",
                    "Continue the same agent task.",
                ]
            )
        ),
        workflow_state,
    )
    delete_plan = _resolve_calendar_turn(
        monkeypatch,
        delete_execution_request.current_request,
        CalendarActionInterpretation(
            operation="delete",
            operation_source_text="Delete",
            event_reference="the calendar event you just updated in this thread",
            event_reference_source_text=(
                "the calendar event you just updated in this thread"
            ),
            event_reference_from_thread_context=True,
        ),
        verified_objects=delete_execution_request.continuation.verified_objects,
    )
    assert delete_plan.event_id == event_id
    delete_payload = run_persisted_turn(
        delete_request, delete_plan, "1786000104.000100"
    )
    assert delete_payload["continuation_objects"][0]["lifecycle_state"] == "deleted"
    assert provider_calls == [
        ("create", event_id),
        ("update", event_id),
        ("update", event_id),
        ("read", event_id),
        ("delete", event_id),
    ]

    final_references = (
        slack_actions.latest_verified_provider_objects_for_slack_thread(
            team_id=team_id,
            channel_id=channel_id,
            thread_ts=lifecycle_thread_ts,
            before_request_ts="1786000105.000100",
            database_url=database_url,
        )
    )
    assert final_references[0].object_id == event_id
    assert final_references[0].lifecycle_state == "deleted"
    assert (
        slack_actions.latest_verified_provider_objects_for_slack_thread(
            team_id=team_id,
            channel_id=channel_id,
            thread_ts=read_thread_ts,
            before_request_ts="1786000105.000100",
            database_url=database_url,
        )
        == ()
    )
