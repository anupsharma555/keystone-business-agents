from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from keystone_agents.agents.chief_of_staff import plan_chief_of_staff_request
from keystone_agents.agents.orchestrator import route_request
from keystone_agents.manual_request import infer_manual_request_plan
from keystone_agents.schemas.chief_of_staff import (
    ChiefOfStaffResult,
    ChiefSpecialistToolInput,
)
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
