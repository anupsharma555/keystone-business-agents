"""Focused admission tests for marked lifecycles and Chief coordination."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from keystone_agents.agent_tool_policy import tool_name_for_policy
from keystone_agents.agents.airtable_context import build_airtable_context_agent
from keystone_agents.agents.chief_of_staff import build_chief_of_staff_agent
from keystone_agents.agents.google_workspace_context import (
    build_google_workspace_context_agent,
)
from keystone_agents.agents.outreach_composer import build_outreach_composer_agent
from keystone_agents.agents.zotero_context import build_zotero_context_agent
from keystone_agents.capabilities.tool_scope import scope_tools_for_request
from keystone_agents.planning.compatibility import (
    infer_manual_request_plan,
    request_forbids_live_research,
)
from keystone_agents.quality_budget import chief_of_staff_quality_budget
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan


def _tool_names(agent: object) -> set[str]:
    return {tool_name_for_policy(tool) for tool in agent.tools}


def test_marked_composite_lifecycles_survive_request_scope() -> None:
    airtable_request = (
        "I approve one exact synthetic Airtable lifecycle in Business Expenses marked "
        "KBA_TEST_RECORD: create it, read it back, revise its description, confirm "
        "the same row changed, then remove it and confirm it is gone."
    )
    airtable_plan = infer_manual_request_plan(
        airtable_request,
        requested_agent="airtable_context_agent",
    )
    workspace_request = (
        "I approve one exact synthetic KNIOps document lifecycle titled KBA_TEST_DOC "
        "Orchard Handoff: create it with one short paragraph, read it back, replace "
        "the paragraph, confirm the same document changed, then move it to trash and "
        "verify it."
    )
    workspace_plan = infer_manual_request_plan(
        workspace_request,
        requested_agent="google_workspace_context_agent",
    )

    airtable_agent = build_airtable_context_agent(
        request_text=airtable_request,
        manual_plan=airtable_plan,
        tool_tier="internal_write",
    )
    workspace_agent = build_google_workspace_context_agent(
        request_text=workspace_request,
        manual_plan=workspace_plan,
        tool_tier="internal_write",
    )

    assert set(airtable_plan.provider_operations) == {
        "create",
        "read",
        "update",
        "verify",
        "delete",
    }
    assert set(workspace_plan.provider_operations) == {
        "create",
        "read",
        "update",
        "verify",
        "delete",
    }
    assert _tool_names(airtable_agent) == {"airtable_test_record_lifecycle"}
    assert _tool_names(workspace_agent) == {"google_doc_test_lifecycle"}


def test_marked_calendar_lifecycle_admits_full_tool_scope_only() -> None:
    request = (
        "Chief, I approve one exact KBA_TEST_CALENDAR event lifecycle. Create "
        "KBA_TEST_CALENDAR Orchard Continuity 20261012 on October 12 at 2:00 PM Eastern "
        "for 30 minutes; read it back; "
        "update the same event to 2:30 PM; verify it; then delete only that test event "
        "and confirm it is gone."
    )
    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    agent = build_chief_of_staff_agent(
        request_text=request,
        manual_request_plan=plan,
    )

    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "business_system_write"
    assert plan.provider_system == "google_calendar"
    assert plan.provider_operations == ["create", "read", "update", "verify", "delete"]
    assert _tool_names(agent) == {
        "read_google_calendar_window",
        "create_google_calendar_event",
        "update_google_calendar_event",
        "delete_google_calendar_event",
    }


def test_ordinary_calendar_payload_words_do_not_expand_operation_scope() -> None:
    request = (
        "Chief, create a calendar event titled Delete and Update Review on October 12 "
        "at 2:00 PM Eastern."
    )
    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    assert plan.provider_operations == ["create"]


def test_chief_keeps_named_research_specialist_as_one_bounded_advisor() -> None:
    request = (
        "Chief, using the approved source packet in this thread, have the business "
        "research analyst assess whether the evidence supports a pilot-readiness "
        "claim, then give me the safest next step. Don't search the web, approve "
        "anything, or change state."
    )
    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    agent = build_chief_of_staff_agent(
        request_text=request,
        manual_request_plan=plan,
        include_specialist_tools=True,
    )

    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "route_request"
    assert plan.workflow == ["business_research_analyst"]
    assert _tool_names(agent) == {
        "inspect_active_work_items",
        "business_research_analyst_as_specialist_tool",
    }
    assert (
        chief_of_staff_quality_budget(
            request_text=request,
            manual_request_plan=plan,
        ).tool_tier
        == "deep_retrieval"
    )


def test_quality_budget_raises_write_tier_only_for_typed_provider_mutation() -> None:
    provider_write = ManualRequestPlan(
        source="canonical:test",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="business_system_write",
        provider_system="google_calendar",
        provider_operations=["create", "update", "delete"],
        ask_shape={"permission_state": "approval_required"},
    )
    read_only = ManualRequestPlan.model_validate(
        {
            **provider_write.model_dump(mode="python"),
            "ask_shape": {"permission_state": "read_only"},
        }
    )
    unspecified_provider = provider_write.model_copy(update={"provider_system": "unspecified"})

    assert (
        chief_of_staff_quality_budget(manual_request_plan=provider_write).tool_tier
        == "internal_write"
    )
    assert chief_of_staff_quality_budget(manual_request_plan=read_only).tool_tier == "core_read"
    assert (
        chief_of_staff_quality_budget(manual_request_plan=unspecified_provider).tool_tier
        == "core_read"
    )


@pytest.mark.parametrize(
    ("requested_agent", "request_text", "builder"),
    [
        (
            "airtable_context_agent",
            "Inspect the Business Expenses fields in Airtable and write a concise "
            "summary here for review.",
            build_airtable_context_agent,
        ),
        (
            "google_workspace_context_agent",
            "Find the most recently changed Google Doc metadata and write a concise "
            "summary here for review.",
            build_google_workspace_context_agent,
        ),
    ],
)
def test_response_only_write_verb_does_not_admit_provider_mutation(
    requested_agent: str,
    request_text: str,
    builder: object,
) -> None:
    plan = infer_manual_request_plan(request_text, requested_agent=requested_agent)
    agent = builder(
        request_text=request_text,
        manual_plan=plan,
        tool_tier="internal_write",
    )

    assert set(plan.provider_operations) <= {"read", "search", "verify"}
    assert not _tool_names(agent).intersection(
        {
            "airtable_write_record",
            "airtable_test_record_lifecycle",
            "google_doc_write",
            "google_doc_trash",
            "google_doc_test_lifecycle",
        }
    )


@pytest.mark.parametrize(
    ("requested_agent", "request_text", "builder", "lifecycle_tool"),
    [
        (
            "airtable_context_agent",
            "Read-only: inspect the Airtable record titled "
            '"KBA_TEST_RECORD create update delete lifecycle" and write a summary '
            "here. Make no changes.",
            build_airtable_context_agent,
            "airtable_test_record_lifecycle",
        ),
        (
            "google_workspace_context_agent",
            "Read-only: find the Google Doc titled "
            '"KBA_TEST_DOC create update delete lifecycle" and write a summary '
            "here. Make no changes.",
            build_google_workspace_context_agent,
            "google_doc_test_lifecycle",
        ),
        (
            "zotero_context_agent",
            "Read-only: inspect the Zotero item titled "
            '"KBA_TEST_NOTE create update delete lifecycle" and write a summary '
            "here. Make no changes.",
            build_zotero_context_agent,
            "zotero_test_note_lifecycle",
        ),
    ],
)
def test_read_only_request_blocks_marker_words_from_admitting_lifecycle(
    requested_agent: str,
    request_text: str,
    builder: object,
    lifecycle_tool: str,
) -> None:
    plan = infer_manual_request_plan(request_text, requested_agent=requested_agent)
    agent = builder(
        request_text=request_text,
        manual_plan=plan,
        tool_tier="internal_write",
    )

    assert plan.ask_shape.permission_state == "read_only"
    assert set(plan.provider_operations) <= {"read", "search", "verify"}
    assert lifecycle_tool not in _tool_names(agent)


def test_provider_write_scope_uses_exact_calendar_and_workspace_operations() -> None:
    calendar_plan = ManualRequestPlan(
        source="canonical:test",
        target_agent="chief_of_staff",
        intent="business_system_write",
        provider_system="google_calendar",
        provider_operations=["create"],
        ask_shape={"permission_state": "approval_required"},
    )
    calendar_tools = [
        SimpleNamespace(name=name)
        for name in (
            "read_google_calendar_window",
            "create_google_calendar_event",
            "update_google_calendar_event",
            "delete_google_calendar_event",
        )
    ]
    workspace_plan = ManualRequestPlan(
        source="canonical:test",
        target_agent="google_workspace_context_agent",
        intent="business_system_write",
        provider_system="google_workspace",
        provider_operations=["update"],
        ask_shape={"permission_state": "approval_required"},
    )
    workspace_tools = [
        SimpleNamespace(name=name)
        for name in (
            "google_doc_write",
            "google_doc_trash",
            "google_drive_create_folder",
            "google_drive_rename_folder",
            "google_sheet_create",
            "google_sheet_update_row",
            "google_sheet_trash",
        )
    ]

    calendar = scope_tools_for_request(
        "chief_of_staff",
        calendar_tools,
        manual_request_plan=calendar_plan,
        tool_tier="internal_write",
    )
    workspace = scope_tools_for_request(
        "google_workspace_context_agent",
        workspace_tools,
        manual_request_plan=workspace_plan,
        tool_tier="internal_write",
    )

    assert {tool.name for tool in calendar.tools} == {
        "read_google_calendar_window",
        "create_google_calendar_event",
    }
    assert {tool.name for tool in workspace.tools} == {
        "google_doc_write",
        "google_drive_rename_folder",
        "google_sheet_update_row",
    }


def test_quoted_chief_advisory_history_and_no_agent_boundary_attach_no_specialist() -> None:
    request = (
        'Chief, the old runbook said "have the business research analyst assess the '
        'packet, then recommend a next step." Do not run any agent; summarize that '
        "historical instruction only."
    )
    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    agent = build_chief_of_staff_agent(
        request_text=request,
        manual_request_plan=plan,
        include_specialist_tools=True,
    )
    synthetic_workflow_plan = ManualRequestPlan(
        source="canonical:test",
        target_agent="chief_of_staff",
        intent="route_request",
        workflow=["business_research_analyst"],
    )

    assert plan.target_agent == "chief_of_staff"
    assert plan.workflow == []
    assert "business_research_analyst_as_specialist_tool" not in _tool_names(agent)
    assert (
        chief_of_staff_quality_budget(
            request_text=request,
            manual_request_plan=synthetic_workflow_plan,
        ).tool_tier
        == "core_read"
    )


@pytest.mark.parametrize(
    "boundary",
    ["without searching", "without browsing", "without researching"],
)
def test_live_research_negative_gerunds_are_respected(boundary: str) -> None:
    request = f"Compare these two supplied opportunities {boundary}."
    plan = infer_manual_request_plan(request, requested_agent="opportunity_scout")

    assert request_forbids_live_research(request) is True
    assert plan.requires_live_search is False
    assert plan.intent == "opportunity_search"


def test_direct_outreach_supplied_facts_keep_draft_intent() -> None:
    request = (
        "Using only these approved synthetic facts, draft a warm organization-level "
        "introduction for review: Cedar Youth Health provides measurement dashboards. "
        "Do not send, save, or create an approval."
    )
    plan = infer_manual_request_plan(request, requested_agent="outreach_composer")

    assert plan.target_agent == "outreach_composer"
    assert plan.intent == "outreach_draft"
    assert plan.requires_approved_context is False
    assert plan.requires_live_search is False
    assert plan.ask_shape.source_type_preference == ["approved_synthetic"]


def test_natural_supplied_outreach_draft_gets_only_bounded_composition_tools() -> None:
    request = (
        "Here’s an approved synthetic setup for a practice: Pine Harbor Behavioral "
        "Health says it runs measurement-based care across three community clinics "
        "and wants cleaner reporting; no person or email has been approved. Write a "
        "friendly organization-level introduction in 90-110 words for internal "
        "review, then add one separate Contact gap line. Stay within these facts. "
        "Don’t research, send, post, save, open an approval, or update tracking."
    )
    plan = infer_manual_request_plan(request, requested_agent="outreach_composer")
    agent = build_outreach_composer_agent(
        request_text=request,
        manual_request_plan=plan,
    )

    assert plan.ask_shape.permission_state == "draft_only"
    assert plan.requires_approved_context is False
    assert _tool_names(agent) == {
        "check_unsupported_claims",
        "build_approved_outreach_drafting_context",
        "compose_outreach_draft_llm_constrained",
    }


def test_internal_call_prep_does_not_infer_negated_outreach_channels() -> None:
    request = (
        "I'm preparing for a first conversation with a synthetic clinic network. "
        "From that context only, give three discovery questions, two safe claims, "
        "and one unresolved point. This is internal call prep, not an email or "
        "LinkedIn draft; don't research, save, or send anything."
    )
    plan = infer_manual_request_plan(request, requested_agent="outreach_composer")

    assert plan.target_agent == "outreach_composer"
    assert plan.intent == "outreach_draft"
    assert plan.ask_shape.audience_scope == "internal"
    assert plan.outreach_channel == ""
    assert plan.requires_approved_context is False
    assert plan.requires_live_search is False
    assert plan.provider_system == "unspecified"
    assert plan.provider_operations == []
    assert plan.side_effect_policy == "draft_or_read_only"
