from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from keystone_agents.agents import calendar_action_interpreter as calendar_interpreter
from keystone_agents.agents.chief_of_staff import plan_chief_of_staff_request
from keystone_agents.agents.orchestrator import route_request
from keystone_agents.manual_request import infer_manual_request_plan
from keystone_agents.schemas.calendar_action import CalendarLookupSynthesis
from keystone_agents.schemas.chief_of_staff import (
    ChiefOfStaffResult,
    ChiefSpecialistToolInput,
)
from keystone_agents.schemas.manual_request_plan import AskShapePolicy, ManualRequestPlan
from keystone_agents.schemas.orchestrator import OrchestratorResult
from scripts import run_chief_of_staff as chief_script


def _orchestrate(request: str) -> OrchestratorResult:
    plan = infer_manual_request_plan(request, requested_agent="orchestrator")
    return route_request(request, manual_plan=plan)


def test_orchestrator_read_contract_preserves_request_shape_and_selects_owner() -> None:
    request = (
        "Review the business-agent architecture and recommend the next three "
        "implementation steps without writing or posting anything."
    )
    plan = infer_manual_request_plan(request, requested_agent="orchestrator")
    result = route_request(request, manual_plan=plan)

    assert plan.objective == request
    assert plan.target_agent == "chief_of_staff"
    assert result.route == "chief_of_staff"
    assert result.workflow == ["chief_of_staff"]
    assert result.decision_trace is not None
    assert result.decision_trace.selected_route == "chief_of_staff"
    assert "no_send_enforced" in result.decision_trace.safety_gates_applied


def test_orchestrator_write_contract_creates_internal_advice_not_provider_state() -> None:
    result = _orchestrate(
        "Update the Airtable CRM row for Lindus Health with this reviewed note."
    )

    assert result.route == "airtable_context_agent"
    assert "crm_preflight" in result.workflow
    assert result.approval_required is True
    assert result.send_enabled is False
    assert {"save_to_crm", "crm_write"} <= set(result.forbidden_actions)
    assert result.artifacts.crm_ready_fields
    assert any("not written externally" in note for note in result.artifacts.notes)


def test_orchestrator_allows_scoped_chief_airtable_lifecycle_to_reach_manager() -> None:
    request = (
        "Using Airtable context, create one marked KBA test expense in the Business "
        "Expenses table, verify it, update the same record description, verify it "
        "again, and remove only that test record."
    )
    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    result = route_request(request, manual_plan=plan)

    assert result.route == "airtable_context_agent"
    assert result.refused is False
    assert result.send_enabled is False
    assert result.decision_trace is not None
    assert result.decision_trace.selected_route == "airtable_context_agent"
    assert "send_email" in result.forbidden_actions


def test_orchestrator_modify_contract_replans_after_explicit_user_correction() -> None:
    original = _orchestrate("Find current behavioral-health partnership opportunities.")
    corrected_request = (
        "Correction: do not scout opportunities. Research NeuroFlow only and return "
        "a read-only company brief."
    )
    corrected = _orchestrate(corrected_request)

    assert original.route == "opportunity_scout"
    assert corrected.route == "business_research_analyst"
    assert corrected.workflow == ["business_research_analyst"]
    assert corrected.decision_trace is not None
    assert corrected.decision_trace.selected_route == "business_research_analyst"
    assert "opportunity_scout" in corrected.decision_trace.rejected_routes
    assert corrected.send_enabled is False


def test_orchestrator_ambiguous_modify_request_stops_before_side_effect() -> None:
    result = _orchestrate("Update it.")

    assert result.route == "clarification"
    assert result.workflow == []
    assert result.decision_trace is not None
    assert result.decision_trace.missing_information_blockers
    assert result.send_enabled is False


def test_orchestrator_schema_cannot_be_modified_to_enable_send() -> None:
    with pytest.raises(ValidationError):
        OrchestratorResult(route="outreach_composer", send_enabled=True)


def test_chief_read_contract_uses_operating_context_without_mutation() -> None:
    result = plan_chief_of_staff_request(
        "Chief of staff obtain context on the adolescent depression measurement project."
    )

    assert result.recommended_route.workflow_type == "project-context-review"
    assert "business_workflow_state" in result.context_sources_considered
    assert result.write_requests == []
    assert result.send_enabled is False
    assert result.slack_post_allowed is False


def test_chief_internal_write_contract_returns_reviewable_artifact_plan() -> None:
    result = plan_chief_of_staff_request(
        "Chief of staff create a Google Doc named KBA RWM Test in KNIOps with "
        "a one-sentence internal implementation note."
    )

    assert result.recommended_route.workflow_type == "artifact-write-plan"
    assert [request.destination.value for request in result.write_requests] == [
        "google_doc"
    ]
    assert result.write_requests[0].approval_required is True
    assert result.send_enabled is False
    assert result.slack_post_allowed is False


def test_chief_modify_contract_revises_plan_after_new_operator_direction() -> None:
    first = plan_chief_of_staff_request(
        "Chief of staff create a Google Doc with an internal implementation note."
    )
    revised = plan_chief_of_staff_request(
        "Correction: do not create a document. Review the project context and list "
        "the missing evidence only."
    )

    assert first.write_requests
    assert revised.recommended_route.workflow_type == "project-context-review"
    assert revised.write_requests == []
    assert revised.send_enabled is False
    assert revised.slack_post_allowed is False


def test_chief_structured_handoff_keeps_nested_specialist_advisory() -> None:
    result = plan_chief_of_staff_request(
        "Chief of staff agent: use preprints context agent history and Zotero "
        "context agent evidence before Business Research Agent reviews NeuroFlow. "
        "Return the best next owner and keep all sends, posts, drafts, and writes blocked."
    )

    assert result.durable_handoff is not None
    assert result.durable_handoff.agent == "business_research_analyst"
    assert [handoff.agent for handoff in result.context_handoffs] == [
        "preprints_context_agent",
        "zotero_context_agent",
    ]
    assert all(handoff.requires_approval is False for handoff in result.context_handoffs)
    assert result.send_enabled is False


def test_chief_nested_tool_input_restores_all_advisory_boundaries() -> None:
    tool_input = ChiefSpecialistToolInput(
        raw_operator_request="Review this approved write request.",
        specialist_task="Return a write plan only.",
        side_effect_boundaries=[],
    )

    assert {
        "nested_specialist_advisory_only",
        "no_send",
        "no_publish",
        "no_nested_live_write",
    } <= set(tool_input.side_effect_boundaries)


def test_chief_failure_reconciliation_preserves_verified_airtable_write() -> None:
    payload: dict[str, object] = {
        "status": "failed",
        "human_summary": "The model did not finish.",
        "public_result": {
            "status": "failed",
            "title": "Business Agents Run Failed",
            "text": "The model did not finish.",
            "completion_confirmed": False,
            "provider_write_attempted": False,
        },
    }
    receipt = {
        "status": "success",
        "operation": "create_expense_from_receipt",
        "tool_name": "airtable_create_expense_from_receipt",
        "provider": "airtable",
        "table": "Business Expenses",
        "record_id": "recVerifiedExpense",
        "provider_write": True,
        "verification": {"passed": True},
    }

    chief_script._reconcile_verified_write_after_sdk_failure(
        payload,
        tool_receipts=[receipt],
        sdk_failure={"failure_kind": "model_tool_turns_exhausted"},
    )

    assert payload["status"] == "partial"
    assert payload["block_kind"] == "verified_provider_write_synthesis_incomplete"
    assert "Do not repeat the write" in str(payload["human_summary"])
    assert payload["request_cache"]["post_side_effect_reconciliation"][
        "retry_mutation"
    ] is False
    assert payload["side_effects"]["external_write_performed"] is True
    assert payload["side_effects"]["evidence_complete"] is True
    assert payload["public_result"]["status"] == "partial"


def test_chief_failure_reconciliation_ignores_unverified_write() -> None:
    payload: dict[str, object] = {
        "status": "failed",
        "human_summary": "The provider result was not verified.",
    }
    receipt = {
        "status": "partial",
        "operation": "update",
        "tool_name": "airtable_write_record",
        "provider": "airtable",
        "record_id": "recUnverifiedExpense",
        "provider_write": True,
        "verification": {"passed": False},
    }

    chief_script._reconcile_verified_write_after_sdk_failure(
        payload,
        tool_receipts=[receipt],
        sdk_failure={"failure_kind": "provider_verification_failed"},
    )

    assert payload["status"] == "failed"
    assert "request_cache" not in payload
    assert "side_effects" not in payload


def test_chief_schema_blocks_unscoped_post_and_direct_send() -> None:
    with pytest.raises(ValidationError):
        ChiefOfStaffResult(send_enabled=True)
    with pytest.raises(ValidationError):
        ChiefOfStaffResult(
            slack_post_allowed=True,
            slack_post_policy="requires_human_review",
            slack_target_channel="general",
        )


def test_chief_calendar_completion_preserves_slack_renderer_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = SimpleNamespace(
        operation="update",
        title="",
        event_reference="KBA_TEST_CALENDAR_COS_0718",
        start_date="",
        start_time="15:00",
        end_time="15:30",
        description="revised through the same natural Slack thread",
    )
    monkeypatch.setattr(
        chief_script,
        "execute_direct_calendar_action",
        lambda *_args, **_kwargs: {
            "status": "done",
            "calendar_action": vars(plan),
            "calendar_lookup": {
                "title": "KBA_TEST_CALENDAR_COS_0718",
                "start_date": "2026-07-24",
            },
            "tool_receipt": {
                "operation": "update",
                "title": "KBA_TEST_CALENDAR_COS_0718",
                "start_date": "2026-07-24",
                "start_time": "15:00",
                "end_time": "15:30",
                "event_id": "event-test-id",
                "html_link": "https://calendar.example.test/event",
                "verification": {"passed": True},
            },
            "side_effects": {"calendar_write_performed": True},
        },
    )

    exit_code = chief_script._run_interpreted_calendar_action(
        input_text="move that same event and revise its note",
        plan=plan,
        json_output=True,
        openai_requests=1,
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["output"]["summary"].startswith(
        "Updated KBA_TEST_CALENDAR_COS_0718"
    )
    assert payload["output"]["recommended_route"]["workflow_type"] == (
        "calendar-action-complete"
    )
    recommended_actions = payload["output"]["recommended_actions"]
    assert "event-test-id" not in json.dumps(recommended_actions)
    assert "calendar.example.test" not in json.dumps(recommended_actions)
    assert recommended_actions == [
        (
            'Calendar note verified from the requested update: '
            '"revised through the same natural Slack thread".'
        ),
        "The existing event was modified; no duplicate event was created.",
    ]
    assert payload["tool_receipt"]["verification"]["passed"] is True
    assert payload["tool_receipt"]["event_id"] == "event-test-id"
    assert payload["tool_receipt"]["html_link"] == "https://calendar.example.test/event"
    assert payload["usage"]["requests"] == 1


def test_chief_calendar_read_returns_answer_without_route_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = SimpleNamespace(
        operation="read",
        read_scope="time_window",
        description="",
        event_reference="",
        title="",
        start_date="2026-07-28",
        start_time="",
        end_time="",
    )
    answer = (
        "Google Calendar events:\n"
        '- "Example Medical Clinic" (Care Appointments) on 2026-07-28 '
        "from 10:40 AM to 11:40 AM"
    )
    monkeypatch.setattr(
        chief_script,
        "execute_direct_calendar_action",
        lambda *_args, **_kwargs: {
            "status": "done",
            "human_summary": answer,
            "calendar_action": vars(plan),
            "calendar_lookup": {"calendar_scope": "all_readable"},
            "tool_receipt": {
                "operation": "read_calendar_window",
                "events": [{"title": "Example Medical Clinic"}],
                "verification": {"passed": True},
            },
            "public_result": {
                "status": "completed",
                "title": "Business Agents Result Ready",
                "text": answer,
            },
            "side_effects": {"calendar_write_performed": False},
        },
    )
    exit_code = chief_script._run_interpreted_calendar_action(
        input_text="list tomorrow from every readable Google Calendar",
        plan=plan,
        json_output=True,
        openai_requests=2,
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["slack_display_text"] == answer
    assert payload["display_text"] == answer
    assert payload["output"]["summary"] == answer
    assert payload["output"]["recommended_actions"] == []
    assert payload["output"]["approval_required"] is False
    assert payload["output"]["recommended_route"]["workflow_type"] == (
        "calendar-read-complete"
    )
    assert "workflow" not in payload["slack_display_text"].lower()
    assert "route" not in payload["slack_display_text"].lower()


def test_chief_targeted_calendar_read_returns_model_selected_provider_answer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = SimpleNamespace(
        operation="read",
        read_scope="time_window",
        query="",
        description="",
        event_reference="",
        title="",
        start_date="2026-07-28",
        start_time="",
        end_time="",
    )
    events = [
        {
            "title": "Window Washing",
            "start_date": "2026-07-28",
            "start_time": "08:30",
            "end_time": "09:30",
            "source_calendar_name": "",
            "source_calendar_primary": True,
        },
        {
            "title": "Example Medical Clinic",
            "start_date": "2026-07-28",
            "start_time": "10:40",
            "end_time": "11:40",
            "location": "100 Example Avenue, Exampleville, PA 19000",
            "source_calendar_name": "Care Appointments",
            "source_calendar_primary": False,
        },
    ]
    monkeypatch.setattr(
        chief_script,
        "execute_direct_calendar_action",
        lambda *_args, **_kwargs: {
            "status": "done",
            "human_summary": "Google Calendar events: raw list",
            "calendar_action": vars(plan),
            "calendar_lookup": {"calendar_scope": "all_readable"},
            "tool_receipt": {
                "operation": "read_calendar_window",
                "events": events,
                "verification": {"passed": True},
            },
            "public_result": {
                "status": "completed",
                "title": "Business Agents Result Ready",
                "text": "Google Calendar events: raw list",
            },
            "side_effects": {"calendar_write_performed": False},
        },
    )
    expected = (
        'I found one matching event: "Example Medical Clinic" on the '
        "Care Appointments calendar on 2026-07-28, from 10:40 AM to 11:40 AM. "
        "Location: 100 Example Avenue, Exampleville, PA 19000."
    )
    monkeypatch.setattr(
        chief_script,
        "resolve_calendar_lookup_answer",
        lambda *_args, **_kwargs: SimpleNamespace(
            text=expected,
            openai_requests=1,
            warnings=(),
        ),
    )

    exit_code = chief_script._run_interpreted_calendar_action(
        input_text="CoS, find and list my medical appointment that is tomorrow.",
        plan=plan,
        json_output=True,
        openai_requests=2,
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="context_lookup",
            provider_system="google_calendar",
            provider_operations=["read"],
            provider_read_scope="bounded_collection",
            primary_target="medical appointment tomorrow",
            ask_shape=AskShapePolicy(ask_breadth="narrow"),
        ),
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["slack_display_text"] == expected
    assert payload["public_result"]["text"] == expected
    assert "Window Washing" not in payload["slack_display_text"]
    assert payload["usage"]["requests"] == 3


def test_chief_groups_duplicate_calendar_records_and_uses_local_time(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = SimpleNamespace(
        operation="read",
        read_scope="time_window",
        query="",
        start_date="2026-07-28",
    )
    events = [
        {
            "title": "Example Medical Clinic",
            "start": "2026-07-28T10:40:00-04:00",
            "end": "2026-07-28T11:40:00-04:00",
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
            "start": "2026-07-28T14:25:00Z",
            "end": "2026-07-28T15:00:00Z",
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
    ]
    monkeypatch.setattr(
        chief_script,
        "execute_direct_calendar_action",
        lambda *_args, **_kwargs: {
            "status": "done",
            "human_summary": "Raw full-day agenda",
            "tool_receipt": {
                "operation": "read_calendar_window",
                "events": events,
                "verification": {"passed": True},
            },
            "side_effects": {"calendar_write_performed": False},
        },
    )
    monkeypatch.setattr(
        calendar_interpreter,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=CalendarLookupSynthesis(
                status="matched",
                selected_event_indexes=[0, 1],
                related_event_groups=[[0, 1]],
                selection_reason=(
                    "The overlapping records appear to describe the same visit."
                ),
            )
        ),
    )

    exit_code = chief_script._run_interpreted_calendar_action(
        input_text=(
            "CoS, find my medical appointment tomorrow. What is its time and location?"
        ),
        plan=plan,
        json_output=True,
        openai_requests=2,
        manual_plan=ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="context_lookup",
            provider_system="google_calendar",
            provider_operations=["read"],
            provider_read_scope="bounded_collection",
            primary_target="medical appointment tomorrow",
            ask_shape=AskShapePolicy(ask_breadth="narrow"),
        ),
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert "one likely event represented by 2 Calendar entries" in payload[
        "slack_display_text"
    ]
    assert "10:25 AM to 11:00 AM" in payload["slack_display_text"]
    assert "10:40 AM to 11:40 AM" in payload["slack_display_text"]
    assert "100 Example Avenue" in payload["slack_display_text"]
    assert "different times" in payload["slack_display_text"]
    assert "2:25 PM" not in payload["slack_display_text"]
    assert "Raw full-day agenda" not in payload["slack_display_text"]
