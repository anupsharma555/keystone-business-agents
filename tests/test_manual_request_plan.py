from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from keystone_agents import cli
from keystone_agents.agents.manual_request_planner import (
    ManualRequestPlannerInput,
    _compact_manual_planner_context,
    _planner_model_configs,
    resolve_manual_request_plan,
)
from keystone_agents.agents.orchestrator import route_request, run_orchestrator_preflight
from keystone_agents.gmail_triage.execution_plan import (
    gmail_message_count_scope,
    infer_gmail_execution_plan,
    resolve_gmail_execution_plan,
)
from keystone_agents.manual_request import (
    has_explicit_local_attachment_context,
    infer_manual_request_plan,
    is_internal_slack_composition_plan,
    is_single_owner_gmail_reply_request,
    live_search_allowed_for_execution,
    looks_like_stateful_work_request,
    looks_like_supplied_context_synthesis_request,
    merge_manual_request_plan,
    positive_capability_text,
    reconcile_manual_request_followup,
    resolve_manual_request_owner,
)
from keystone_agents.outreach_composer.execution_plan import infer_outreach_execution_plan
from keystone_agents.planning.compatibility import selected_public_page_url
from keystone_agents.planning.composition_admission import (
    is_selected_public_url_read_plan,
)
from keystone_agents.runtime.provider_context import (
    ProviderContextStageResult,
    provider_context_requirements_satisfied,
)
from keystone_agents.schemas.approval import ApprovalState
from keystone_agents.schemas.decision_ownership import (
    AgentDecisionRecord,
    DecisionValidatorOutcome,
)
from keystone_agents.schemas.manual_request_plan import (
    AskShapePolicy,
    ManualProviderActionStep,
    ManualProviderResultSetScope,
    ManualRequestPlan,
    ProviderContextRequirement,
)
from keystone_agents.schemas.output_constraints import InterpretedOutputConstraints
from keystone_agents.semantic_execution import ExecutionIntentAuthority
from keystone_agents.test_pack_specs import get_test_pack_spec


def _offline_orchestrator_preflight(
    request_text: str,
    *,
    requested_agent: str | None = None,
    **_kwargs: object,
):
    plan = infer_manual_request_plan(
        request_text,
        requested_agent=requested_agent,
    ).model_copy(update={"source": "llm"})
    return cli.OrchestratorPreflight(
        request_text=request_text,
        requested_agent=requested_agent,
        advisory_only=requested_agent not in {None, "orchestrator"},
        selected_agent=plan.target_agent,
        manual_request_plan=plan,
        route_result=cli.route_request(request_text, manual_plan=plan),
        sdk_usage_events=[{"usage": {"requests": 1}}],
    )


@pytest.mark.parametrize(
    "rendered_url",
    [
        "https://www.nimh.nih.gov/health/topics/technology-and-the-future-of-mental-health-treatment",
        (
            "<https://www.nimh.nih.gov/health/topics/technology-and-the-future-of-mental-health-treatment"
            "|NIMH treatment technology page>"
        ),
    ],
)
def test_selected_public_page_read_compiles_to_one_exact_provider_read(
    rendered_url: str,
) -> None:
    request = (
        "Could you read only this NIMH page and give me two concise, source-supported "
        "facts? Include the final URL and any extraction limitation. Don't search "
        f"elsewhere or change anything: {rendered_url}"
    )

    plan = infer_manual_request_plan(
        request,
        requested_agent="business_research_analyst",
    )

    assert plan.primary_target == (
        "https://www.nimh.nih.gov/health/topics/technology-and-the-future-of-mental-health-treatment"
    )
    assert plan.target_type == "url"
    assert plan.provider_operations == ["read"]
    assert plan.provider_read_scope == "single_item"
    assert plan.task_objective == "source_research"
    assert plan.expected_artifact_type == "source_summary"
    assert plan.requires_live_search is False
    assert plan.ask_shape.prior_context_dependency == "selected_context"
    assert plan.ask_shape.permission_state == "read_only"
    assert plan.ask_shape.strict_filter_mode == "exact"
    assert request in plan.objective
    assert is_selected_public_url_read_plan(plan) is True


def test_selected_public_page_read_accepts_natural_give_me_slack_request() -> None:
    request = (
        "I’m looking at this NIMH page before a planning call. Using only the page "
        "itself, give me two short bullets on what it says about how psychotherapy can "
        "be delivered or evaluated, then one sentence on what KNI should not infer from "
        "it. Include the final page URL and mention any extraction limitation. Please "
        "don’t search elsewhere, look for contacts, save anything, or change anything: "
        "<https://www.nimh.nih.gov/health/topics/psychotherapies|"
        "nimh.nih.gov/health/topics/psychotherapies>"
    )

    plan = infer_manual_request_plan(
        request,
        requested_agent="business_research_analyst",
    )

    assert plan.primary_target == (
        "https://www.nimh.nih.gov/health/topics/psychotherapies"
    )
    assert plan.provider_operations == ["read"]
    assert plan.provider_read_scope == "single_item"
    assert plan.requires_live_search is False
    assert is_selected_public_url_read_plan(plan) is True


def test_exact_current_gmail_thread_keeps_single_item_read_scope() -> None:
    request = (
        "Read the current Halo email in my operator@example.com inbox, then "
        "draft a short reply here."
    )

    plan = infer_manual_request_plan(request, requested_agent="gmail_triage")

    assert plan.target_agent == "gmail_triage"
    assert plan.target_type == "gmail_thread"
    assert plan.provider_system == "gmail"
    assert plan.provider_operations == ["read"]
    assert plan.provider_read_scope == "single_item"


def test_gmail_candidate_triage_keeps_bounded_collection_scope() -> None:
    request = (
        "Please review my recent G2i emails, choose the current interview "
        "conversation, and draft a short reply here."
    )

    plan = infer_manual_request_plan(request, requested_agent="gmail_triage")

    assert plan.target_agent == "gmail_triage"
    assert plan.target_type == "gmail_message_collection"
    assert plan.provider_read_scope == "bounded_collection"


def test_selected_public_page_read_preserves_natural_compact_bullet_count() -> None:
    request = (
        "Please use just this page and tell me in two compact bullets what it says. "
        "Don’t search outside the page: "
        "https://www.nimh.nih.gov/health/publications/children-and-mental-health"
    )

    plan = infer_manual_request_plan(
        request,
        requested_agent="business_research_analyst",
    )

    constraints = plan.ask_shape.output_constraints
    assert constraints.item_count_mode == "exact"
    assert constraints.minimum_items == 2
    assert constraints.maximum_items == 2


@pytest.mark.parametrize(
    "prompt",
    [
        (
            "Compare these two pages and summarize the differences: "
            "https://example.com/one and https://example.org/two."
        ),
        (
            "Research Example Health broadly, starting with this page and then using "
            "other sources: https://example.com/about"
        ),
        (
            "Use backend browser diagnostics to inspect only this page for console "
            "errors: https://example.com/status"
        ),
        (
            'The earlier prompt said "read only this page"; do not execute it now: '
            "https://example.com/prior"
        ),
        (
            "Yesterday I asked the analyst to read only this page: "
            "https://example.com/prior"
        ),
        (
            "The prior request was to read only this page: https://example.com/prior. "
            "Summarize the old instruction without executing it."
        ),
        (
            'Classify this quoted example, not its instruction: "read only this page '
            'https://example.com/quoted".'
        ),
    ],
)
def test_selected_public_page_read_rejects_ambiguous_or_nonexecuting_shapes(
    prompt: str,
) -> None:
    plan = infer_manual_request_plan(
        prompt,
        requested_agent="business_research_analyst",
    )

    assert selected_public_page_url(prompt) == ""
    assert is_selected_public_url_read_plan(plan) is False


def test_page_bounded_multi_agent_workflow_preserves_graph_and_disables_broad_search() -> None:
    request = (
        "Please use one WorkItem to identify one pilot opportunity for KNI from "
        "Cartwheel's public homepage, research the company using only that page, then "
        "prepare a 70-word outreach draft for internal review. Show the URL. Do not "
        "send or save the draft, and do not look beyond this page: "
        "https://www.cartwheelcare.org/"
    )

    plan = infer_manual_request_plan(request, requested_agent="orchestrator")

    assert plan.workflow == [
        "opportunity_scout",
        "business_research_analyst",
        "outreach_composer",
    ]
    assert plan.intent == "opportunity_to_outreach_loop"
    assert plan.primary_target == "https://www.cartwheelcare.org/"
    assert plan.target_type == "url"
    assert plan.provider_operations == ["read"]
    assert plan.requires_live_search is False
    assert plan.requires_durable_state is True
    assert plan.ask_shape.permission_state == "draft_only"
    assert is_selected_public_url_read_plan(plan) is True


def test_manual_plan_preserves_exact_source_table_and_no_broadening_shape() -> None:
    plan = infer_manual_request_plan(
        "Find exactly active roles from official sources only. Return a table and zero "
        "results if none; do not broaden.",
        requested_agent="opportunity_scout",
    )

    assert plan.ask_shape.ask_breadth == "narrow"
    assert plan.ask_shape.source_type_preference == ["official"]
    assert plan.ask_shape.strict_filter_mode == "exact"
    assert plan.ask_shape.output_form == "table"
    assert plan.ask_shape.stop_condition == "return_zero_without_broadening_if_no_exact_match"


@pytest.mark.parametrize(
    ("artifact_noun", "extension"),
    [
        ("diagram", "png"),
        ("document", "pdf"),
        ("screenshot", "jpg"),
    ],
)
def test_local_attachment_is_selected_context_not_a_workflow(
    artifact_noun: str,
    extension: str,
) -> None:
    request = (
        f"@KNI CoS, looking at this {artifact_noun}, what are the three main stages? "
        "Keep it to three short bullets. Don't search or change anything.\n"
        "Operator-supplied Slack attachment local path: "
        f"/private/tmp/kni-business-agent-slack-files/123/example.{extension}"
    )

    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    assert has_explicit_local_attachment_context(request) is True
    assert looks_like_supplied_context_synthesis_request(request) is True
    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "route_request"
    assert plan.workflow == []
    assert plan.requires_live_search is False
    assert plan.requires_durable_state is False
    assert plan.provider_system == "unspecified"
    assert plan.provider_operations == []
    assert plan.ask_shape.ask_breadth == "narrow"
    assert plan.ask_shape.prior_context_dependency == "selected_context"
    assert plan.ask_shape.source_type_preference == ["local_attachment"]


def test_local_attachment_does_not_cancel_an_explicit_airtable_write() -> None:
    request = (
        "@KNI CoS, add this receipt to Airtable as a personal expense.\n"
        "Operator-supplied Slack attachment local path: "
        "/private/tmp/kni-business-agent-slack-files/123/receipt.png"
    )

    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    assert has_explicit_local_attachment_context(request) is True
    assert looks_like_supplied_context_synthesis_request(request) is False
    assert plan.intent == "business_system_write"
    assert plan.provider_system == "airtable"


def test_supplied_email_facts_capture_selected_context_dependency() -> None:
    plan = infer_manual_request_plan(
        (
            "Outreach Composer, turn the reply outline into a draft using only "
            "the supplied email facts. Show it here for review. Do not access "
            "Gmail, create a provider draft, send, or modify anything."
        ),
        requested_agent="outreach_composer",
    )

    assert plan.target_agent == "outreach_composer"
    assert plan.ask_shape.prior_context_dependency == "selected_context"
    assert plan.ask_shape.permission_state == "draft_only"


def test_llm_cannot_replace_selected_attachment_with_unrequested_provider_read() -> None:
    request = (
        "@KNI CoS, looking at this diagram, summarize the three main stages. "
        "Don't search or change anything.\n"
        "Operator-supplied Slack attachment local path: "
        "/private/tmp/kni-business-agent-slack-files/123/architecture.png"
    )
    base = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    candidate = base.model_copy(
        update={
            "source": "llm",
            "provider_system": "google_workspace",
            "provider_operations": ["read"],
            "rationale": "The diagram might be available in Drive.",
        }
    )

    merged = merge_manual_request_plan(base, candidate)

    assert merged.target_agent == "chief_of_staff"
    assert merged.provider_system == "unspecified"
    assert merged.provider_operations == []
    assert merged.ask_shape.source_type_preference == ["local_attachment"]
    assert any("selected local attachment" in warning for warning in merged.planner_warnings)


@pytest.mark.parametrize(
    "request_text",
    [
        (
            "@KNI CoS, summarize this source packet for NeuroFlow. "
            "Operator-supplied Slack attachment local path: "
            "/private/tmp/kni-business-agent-slack-files/123/neuroflow.pdf"
        ),
        (
            "@KNI CoS, what does the attached Acme Health research brief establish? "
            "Operator-supplied Slack attachment local path: "
            "/private/tmp/kni-business-agent-slack-files/456/acme-health.png"
        ),
    ],
)
def test_chief_delegates_company_attachment_summary_to_business_research(
    request_text: str,
) -> None:
    plan = infer_manual_request_plan(request_text, requested_agent="chief_of_staff")

    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"
    assert plan.target_type == "company"
    assert plan.expected_artifact_type == "source_summary"
    assert plan.requires_live_search is False
    assert plan.provider_system == "unspecified"
    assert plan.provider_operations == []
    assert plan.ask_shape.source_type_preference == ["local_attachment"]
    assert cli._is_direct_supplied_response_request(
        request_text,
        requested_route="business_research_analyst",
    )


@pytest.mark.parametrize(
    "request_text",
    [
        "Check my latest email and summarize it.",
        "What did the newest message say?",
        "Could you look at the most recent note in my inbox?",
    ],
)
def test_llm_gmail_plan_keeps_equivalent_read_phrasings_on_one_execution_contract(
    request_text: str,
) -> None:
    manual_plan = ManualRequestPlan(
        source="llm",
        target_agent="gmail_triage",
        intent="gmail_triage",
        task_objective="gmail_triage",
        expected_artifact_type="gmail_triage_report",
        provider_system="gmail",
        provider_operations=["search", "read"],
        primary_target="latest inbox message",
        target_type="gmail_message_collection",
        gmail_query="in:inbox",
        desired_count=1,
    )

    plan = resolve_gmail_execution_plan(
        request_text,
        manual_plan=manual_plan,
    )

    assert plan.source == "llm_manual_plan"
    assert plan.operation == "single_message_triage"
    assert plan.read_scope == "message"
    assert plan.gmail_query == "in:inbox"
    assert plan.live_read_required is True


def test_llm_gmail_plan_uses_typed_update_even_without_canonical_update_words() -> None:
    manual_plan = ManualRequestPlan(
        source="llm",
        target_agent="gmail_triage",
        intent="business_system_write",
        task_objective="gmail_triage",
        provider_system="gmail",
        provider_operations=["read", "update", "verify"],
        primary_target="Partnership follow-up",
        target_type="gmail_thread",
    )

    plan = resolve_gmail_execution_plan(
        "Please tighten the saved reply and make sure the revision stuck.",
        manual_plan=manual_plan,
    )

    assert plan.operation == "update_draft"
    assert plan.draft_subject_hint == "Partnership follow-up"
    assert plan.create_gmail_drafts is True


def test_gmail_inline_reply_text_does_not_grant_provider_create() -> None:
    request = (
        "@KNI Gmail Triage Agent, read today's E2B email and write a short reply "
        "here if useful. Do not create a Gmail draft or change anything."
    )

    plan = infer_manual_request_plan(request)

    assert plan.target_agent == "gmail_triage"
    assert plan.provider_system == "gmail"
    assert plan.provider_operations == ["read"]
    assert plan.ask_shape.permission_state == "read_only"
    assert plan.draft_policy == "draft_only_when_reply_needed"


def test_gmail_executor_refuses_contradictory_read_only_create_authority() -> None:
    manual_plan = ManualRequestPlan(
        source="llm",
        requested_agent="gmail_triage",
        target_agent="gmail_triage",
        intent="gmail_triage",
        task_objective="gmail_triage",
        expected_artifact_type="gmail_triage_report",
        provider_system="gmail",
        provider_operations=["read", "create"],
        provider_read_scope="bounded_collection",
        provider_result_mode="items",
        draft_policy="draft_only_when_reply_needed",
        ask_shape=AskShapePolicy(permission_state="read_only"),
    )

    plan = resolve_gmail_execution_plan(
        "Include a short reply here only if useful.",
        manual_plan=manual_plan,
    )

    assert plan.operation == "draft_reply"
    assert plan.create_gmail_drafts is False
    assert plan.draft_replies_in_output is True
    assert plan.artifact_policy == "draft_text_in_output"
    assert "gmail_verified_reply_draft_create" not in plan.candidate_helpers


def test_complete_typed_provider_write_reconciles_contradictory_read_only_label() -> None:
    request = (
        'Move "Architecture review" to 2:10 PM, rename it to '
        '"Architecture review updated", and append the supplied note.'
    )
    fallback = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="business_system_write",
        task_objective="business_system_write",
        expected_artifact_type="business_system_write_plan",
        provider_system="google_calendar",
        provider_operations=["read", "update", "verify"],
        provider_action_steps=[
            ManualProviderActionStep(operation="read", resource_type="calendar_event"),
            ManualProviderActionStep(operation="update", resource_type="calendar_event"),
            ManualProviderActionStep(operation="verify", resource_type="calendar_event"),
        ],
        side_effect_policy="internal_write_approval_required",
        ask_shape=AskShapePolicy(permission_state="read_only"),
    )

    merged = merge_manual_request_plan(fallback, candidate)
    authority = ExecutionIntentAuthority.from_value(merged)

    assert merged.ask_shape.permission_state == "approval_required"
    assert authority.effective_provider_operations("google_calendar") == (
        "read",
        "update",
        "verify",
    )
    assert any(
        "complete typed provider-write contract" in warning
        for warning in merged.planner_warnings
    )


def test_incomplete_provider_write_contract_keeps_read_only_ceiling() -> None:
    fallback = infer_manual_request_plan(
        "Review the calendar event without changing it.",
        requested_agent="chief_of_staff",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="business_system_write",
        task_objective="business_system_write",
        expected_artifact_type="business_system_write_plan",
        provider_system="google_calendar",
        provider_operations=["read", "update", "verify"],
        provider_action_steps=[
            ManualProviderActionStep(operation="read", resource_type="calendar_event"),
            ManualProviderActionStep(operation="verify", resource_type="calendar_event"),
        ],
        side_effect_policy="internal_write_approval_required",
        ask_shape=AskShapePolicy(permission_state="read_only"),
    )

    merged = merge_manual_request_plan(fallback, candidate)
    authority = ExecutionIntentAuthority.from_value(merged)

    assert merged.ask_shape.permission_state == "read_only"
    assert authority.effective_provider_operations("google_calendar") == (
        "read",
        "verify",
    )


def test_gmail_executor_refuses_create_when_no_draft_is_requested() -> None:
    manual_plan = ManualRequestPlan(
        source="llm",
        requested_agent="gmail_triage",
        target_agent="gmail_triage",
        intent="gmail_triage",
        task_objective="contact_discovery",
        expected_artifact_type="contact_candidates",
        provider_system="gmail",
        provider_operations=["search", "read", "create"],
        provider_read_scope="bounded_collection",
        provider_result_mode="items",
        draft_policy="no_drafts_requested",
        ask_shape=AskShapePolicy(permission_state="read_only"),
    )

    plan = resolve_gmail_execution_plan(
        "Who handled the account? Answer here only.",
        manual_plan=manual_plan,
    )

    assert plan.operation == "contact_lookup"
    assert plan.create_gmail_drafts is False
    assert plan.draft_replies_in_output is False
    assert "gmail_verified_reply_draft_create" not in plan.candidate_helpers


def test_canonical_gmail_collection_count_cannot_erode_to_one_thread() -> None:
    manual_plan = ManualRequestPlan(
        source="llm",
        requested_agent="gmail_triage",
        target_agent="gmail_triage",
        intent="gmail_triage",
        task_objective="gmail_triage",
        expected_artifact_type="gmail_triage_report",
        provider_system="gmail",
        provider_operations=["search", "read"],
        provider_read_scope="bounded_collection",
        provider_result_mode="count",
        gmail_mailbox_direction="inbound",
        gmail_date_scope="today",
        primary_target="emails sent to me today",
        target_type="gmail_thread",
        desired_count=1,
        gmail_query="newer_than:1d",
        draft_policy="no_drafts_requested",
    )

    plan = resolve_gmail_execution_plan(
        "how many emails sent to me today?",
        manual_plan=manual_plan,
    )

    assert plan.operation == "message_count"
    assert plan.read_scope == "collection"
    assert plan.mailbox_direction == "inbound"
    assert plan.date_scope == "today"
    assert plan.source_label == ""
    assert plan.candidate_helpers == ["gmail_paginated_message_count"]
    assert plan.create_gmail_drafts is False


def test_typed_gmail_count_owns_exact_operator_timezone_day_and_direction() -> None:
    manual_plan = ManualRequestPlan(
        source="llm",
        target_agent="gmail_triage",
        intent="gmail_triage",
        provider_system="gmail",
        provider_operations=["search", "read"],
        provider_read_scope="bounded_collection",
        provider_result_mode="count",
        gmail_mailbox_direction="inbound",
        gmail_date_scope="today",
        gmail_query="newer_than:1d is:unread in:sent",
    )
    plan = resolve_gmail_execution_plan("different surface wording", manual_plan=manual_plan)

    scope = gmail_message_count_scope(
        plan,
        now=datetime(2026, 7, 21, 19, 45, tzinfo=ZoneInfo("America/New_York")),
    )

    start = datetime(2026, 7, 21, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    end = datetime(2026, 7, 22, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    assert scope["query"] == (
        f"to:me -in:sent after:{int(start.timestamp()) - 1} before:{int(end.timestamp())} is:unread"
    )
    assert "newer_than:1d" not in scope["query"]
    assert scope["window_start"] == start.isoformat()
    assert scope["window_end"] == end.isoformat()
    assert scope["timezone"] == "America/New_York"


@pytest.mark.parametrize(
    "requested_fields",
    [["subject"], ["sender"], ["date"], ["sender", "date"]],
)
def test_gmail_followup_projection_preserves_verified_result_scope(
    requested_fields: list[str],
) -> None:
    prior_scope = ManualProviderResultSetScope(
        source_run_id="run-6424",
        provider_system="gmail",
        provider_read_scope="bounded_collection",
        target_type="gmail_message_collection",
        gmail_mailbox_direction="inbound",
        gmail_date_scope="today",
        query="to:me -in:sent after:1784606399 before:1784692800",
        label="",
        timezone="America/New_York",
        window_start="2026-07-21T00:00:00-04:00",
        window_end="2026-07-22T00:00:00-04:00",
        item_count=4,
        complete=True,
        verified=True,
    )
    inconsistent_planner_output = ManualRequestPlan(
        source="llm",
        target_agent="gmail_triage",
        provider_system="gmail",
        provider_operations=["search", "read"],
        provider_read_scope="bounded_collection",
        provider_result_mode="count",
        gmail_requested_fields=requested_fields,
    )

    reconciled = reconcile_manual_request_followup(
        inconsistent_planner_output,
        workflow_context={"prior_provider_result_scope": prior_scope.model_dump()},
    )
    execution = resolve_gmail_execution_plan("current turn", manual_plan=reconciled)
    scope = gmail_message_count_scope(execution)

    assert reconciled.provider_result_mode == "items"
    assert reconciled.gmail_mailbox_direction == "inbound"
    assert reconciled.gmail_date_scope == "today"
    assert reconciled.desired_count == 4
    assert execution.operation == "message_projection"
    assert execution.requested_fields == requested_fields
    assert scope["query"] == prior_scope.query
    assert scope["window_start"] == prior_scope.window_start
    assert scope["window_end"] == prior_scope.window_end
    assert execution.expected_result_count == 4


def test_gmail_plain_count_remains_count_without_projection_fields() -> None:
    plan = ManualRequestPlan(
        source="llm",
        target_agent="gmail_triage",
        provider_system="gmail",
        provider_operations=["search", "read"],
        provider_read_scope="bounded_collection",
        provider_result_mode="count",
        gmail_mailbox_direction="inbound",
        gmail_date_scope="today",
    )

    reconciled = reconcile_manual_request_followup(plan)
    execution = resolve_gmail_execution_plan("How many?", manual_plan=reconciled)

    assert reconciled.provider_result_mode == "count"
    assert execution.operation == "message_count"


def test_current_gmail_day_override_wins_over_inconsistent_llm_field() -> None:
    current_request = (
        "Then check yesterday instead. Pick the one that most needs a response, "
        "and draft it here. If I already replied, don't choose it."
    )
    fallback = infer_manual_request_plan(
        current_request,
        requested_agent="gmail_triage",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="gmail_triage",
        target_agent="gmail_triage",
        workflow=["gmail_triage", "outreach_composer"],
        intent="outreach_draft",
        provider_system="gmail",
        provider_operations=["search", "read"],
        provider_read_scope="bounded_collection",
        provider_result_mode="items",
        gmail_mailbox_direction="inbound",
        gmail_date_scope="today",
        gmail_requested_fields=["subject", "sender", "date", "snippet"],
        target_type="gmail_message_collection",
        task_objective="outreach_draft",
        expected_artifact_type="outreach_draft",
        ask_shape=AskShapePolicy(output_form="draft", audience_scope="internal"),
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert fallback.gmail_date_scope == "yesterday"
    assert fallback.gmail_exclude_threads_with_operator_reply is True
    assert merged.gmail_date_scope == "yesterday"
    assert merged.gmail_exclude_threads_with_operator_reply is True
    assert merged.workflow == ["gmail_triage", "outreach_composer"]
    assert merged.task_objective == "outreach_draft"


def test_gmail_operator_reply_exclusion_accepts_answered_and_replied() -> None:
    for request in (
        "Check yesterday's Gmail and skip any thread I already answered.",
        "Check yesterday's Gmail. If I already replied, don't choose it.",
        "Check yesterday's inbox and skip anything I already replied to.",
        "Check yesterday’s inbox and skip any one I’ve already answered.",
    ):
        plan = infer_manual_request_plan(
            request,
            requested_agent="gmail_triage",
        )
        assert plan.gmail_exclude_threads_with_operator_reply is True


def test_natural_gmail_reply_draft_root_preserves_bounded_read_scope() -> None:
    request = (
        "Check yesterday's inbox and pick the one email I'm most likely to need to answer. "
        "Skip anything I already replied to, and draft a short response here for me to "
        "review. Don't send it or create a Gmail draft."
    )

    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    assert plan.target_agent == "gmail_triage"
    assert plan.provider_system == "gmail"
    assert plan.provider_operations == ["read"]
    assert plan.provider_read_scope == "bounded_collection"
    assert plan.provider_result_mode == "items"
    assert plan.gmail_mailbox_direction == "inbound"
    assert plan.gmail_date_scope == "yesterday"
    assert plan.gmail_exclude_threads_with_operator_reply is True
    assert plan.draft_policy == "draft_only_when_reply_needed"
    assert plan.side_effect_policy == "draft_or_read_only"
    assert plan.workflow == []
    assert plan.intent == "gmail_triage"
    assert plan.task_objective == "gmail_triage"
    assert plan.expected_artifact_type == "gmail_triage_report"
    assert plan.outreach_channel == "internal_slack"
    assert plan.requires_durable_state is False
    assert plan.requires_approved_context is False
    assert plan.ask_shape.output_form == "draft"

    canonical_execution = resolve_gmail_execution_plan(
        request,
        manual_plan=plan.model_copy(update={"source": "llm"}),
    )

    assert canonical_execution.operation == "draft_reply"
    assert canonical_execution.read_scope == "collection"
    assert canonical_execution.max_messages == 4
    assert canonical_execution.mailbox_direction == "inbound"
    assert canonical_execution.date_scope == "yesterday"
    assert canonical_execution.create_gmail_drafts is False
    assert canonical_execution.draft_replies_in_output is True
    assert canonical_execution.live_read_required is True
    assert canonical_execution.candidate_helpers == [
        "query_gmail_message_summaries",
        "read_gmail_context",
        "gmail_triage_sdk",
    ]


def test_live_planner_cannot_drop_bounded_read_then_internal_draft_stage() -> None:
    request = (
        "Check yesterday's inbox and pick the one email I'm most likely to need to answer. "
        "Skip anything I already replied to, and draft a short response here for me to "
        "review. Don't send it or create a Gmail draft."
    )
    base = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    incomplete_live_plan = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="gmail_triage",
        workflow=[],
        intent="gmail_triage",
        objective="Review yesterday's inbox and return a draft for internal review.",
        task_objective="gmail_triage",
        expected_artifact_type="gmail_triage_report",
        target_type="gmail_message_collection",
        provider_system="gmail",
        provider_operations=["read"],
        provider_read_scope="bounded_collection",
        provider_result_mode="items",
        gmail_mailbox_direction="inbound",
        gmail_date_scope="yesterday",
        side_effect_policy="draft_or_read_only",
        requires_durable_state=False,
        ask_shape=AskShapePolicy(
            output_form="draft",
            audience_scope="internal",
            permission_state="read_only",
        ),
    )

    merged = merge_manual_request_plan(base, incomplete_live_plan)

    assert merged.target_agent == "gmail_triage"
    assert merged.workflow == []
    assert merged.requires_durable_state is False
    assert merged.intent == "gmail_triage"
    assert merged.task_objective == "gmail_triage"
    assert merged.expected_artifact_type == "gmail_triage_report"
    assert merged.provider_system == "gmail"
    assert merged.provider_operations == ["read"]
    assert merged.provider_read_scope == "bounded_collection"
    assert merged.provider_result_mode == "items"
    assert merged.gmail_exclude_threads_with_operator_reply is True
    assert merged.outreach_channel == "internal_slack"
    assert any(
        "one-agent Gmail read, rank, and Slack-reply" in warning
        for warning in merged.planner_warnings
    )


def test_negative_filter_and_positive_draft_remain_separate_in_same_sentence() -> None:
    request = (
        "Skip anything I already replied to, and draft a short response here "
        "for me to review."
    )

    positive = " ".join(positive_capability_text(request).split()).lower()
    plan = infer_manual_request_plan(
        f"Check yesterday's inbox. {request}",
        requested_agent="chief_of_staff",
    )

    assert "draft a short response here" in positive
    assert "skip anything" not in positive
    assert plan.gmail_exclude_threads_with_operator_reply is True
    assert plan.workflow == []
    assert plan.expected_artifact_type == "gmail_triage_report"


def test_complete_bounded_read_then_draft_clears_planner_context_prerequisite() -> None:
    request = (
        "CoS, check yesterday's inbox and pick the one email I'm most likely to need "
        "to answer. Skip anything I already replied to, and draft a short response "
        "here for me to review. Don't send it or create a Gmail draft."
    )
    base = infer_manual_request_plan(
        request,
        requested_agent="chief_of_staff",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="clarification",
        intent="clarification",
        objective=request,
        task_objective="clarification",
        expected_artifact_type="none",
        provider_system="unspecified",
        missing_required_information=[
            "A selected email or pasted email context is required before drafting."
        ],
        recipient="an unspecified external recipient",
        ask_shape=AskShapePolicy(
            output_form="draft",
            audience_scope="internal",
            permission_state="read_only",
        ),
    )

    merged = merge_manual_request_plan(base, candidate)

    assert merged.target_agent == "gmail_triage"
    assert merged.workflow == []
    assert merged.provider_system == "gmail"
    assert merged.provider_operations == ["read"]
    assert merged.provider_read_scope == "bounded_collection"
    assert merged.provider_result_mode == "items"
    assert merged.expected_artifact_type == "gmail_triage_report"
    assert merged.outreach_channel == "internal_slack"
    assert merged.recipient == ""
    assert merged.requires_approved_context is False
    assert merged.requires_durable_state is False
    assert merged.missing_required_information == []


def test_explicit_no_draft_gmail_triage_rejects_planner_draft_output_shape() -> None:
    request = (
        "CoS, I've been away from email. What arrived today that actually needs me, "
        "and what can wait? Don't draft, label, archive, or send anything."
    )
    base = infer_manual_request_plan(
        request,
        requested_agent="chief_of_staff",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="gmail_triage",
        intent="gmail_triage",
        primary_target="today's email inbox",
        target_type="gmail_message_collection",
        provider_system="gmail",
        provider_operations=["read"],
        provider_read_scope="bounded_collection",
        provider_result_mode="items",
        gmail_mailbox_direction="inbound",
        gmail_date_scope="today",
        objective=request,
        task_objective="gmail_triage",
        expected_artifact_type="gmail_triage_report",
        draft_policy="no_drafts_requested",
        ask_shape=AskShapePolicy(
            output_form="draft",
            audience_scope="internal",
            permission_state="read_only",
        ),
    )

    merged = merge_manual_request_plan(base, candidate)
    execution = resolve_gmail_execution_plan(request, manual_plan=merged)

    assert merged.ask_shape.output_form != "draft"
    assert merged.draft_policy == "no_drafts_requested"
    assert merged.provider_operations == ["read"]
    assert execution.operation == "priority_grouping"
    assert execution.max_messages == 25
    assert execution.draft_replies_in_output is False


@pytest.mark.parametrize(
    "request_text",
    [
        (
            "CoS, who at Acme Compute helped set up my startup account, and what "
            "email address did they use? Check Gmail and answer here only. "
            "Don't change anything."
        ),
        (
            "CoS, what is the email address of the person from Acme Compute who "
            "set up my startup account? Check Gmail and answer here only."
        ),
        (
            "CoS, which Acme Compute contact onboarded me for my account? "
            "Check Gmail and answer here only."
        ),
    ],
)
def test_known_contact_merge_preserves_bounded_organization_query(
    request_text: str,
) -> None:
    base = infer_manual_request_plan(request_text, requested_agent="chief_of_staff")
    candidate = base.model_copy(
        update={
            "source": "llm",
            "primary_target": "Acme Compute startup account setup",
            "gmail_mailbox_direction": "inbound",
            "gmail_date_scope": "today",
            "gmail_requested_fields": [],
            "gmail_query": "Acme Compute startup account setup",
            "lookback_days": 3,
            "draft_policy": "draft_only_when_reply_needed",
        }
    )

    merged = merge_manual_request_plan(base, candidate)
    execution = resolve_gmail_execution_plan(request_text, manual_plan=merged)

    assert merged.primary_target == "Acme Compute"
    assert merged.gmail_mailbox_direction == "any"
    assert merged.gmail_date_scope == "unspecified"
    assert merged.gmail_requested_fields == ["sender", "subject", "date", "snippet"]
    assert merged.gmail_query == '"Acme Compute"'
    assert merged.lookback_days is None
    assert merged.draft_policy == "no_drafts_requested"
    assert execution.operation == "contact_lookup"
    assert execution.gmail_query == '"Acme Compute"'
    assert execution.side_effect_policy == "read_only"


def test_known_contact_merge_preserves_explicit_operator_lookback() -> None:
    request = (
        "CoS, which Acme Compute contact helped me set up my account in the last "
        "30 days? Check Gmail and answer here only."
    )
    base = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    candidate = base.model_copy(
        update={
            "source": "llm",
            "gmail_date_scope": "today",
            "gmail_query": "Acme Compute account setup",
            "lookback_days": 3,
        }
    )

    merged = merge_manual_request_plan(base, candidate)
    execution = resolve_gmail_execution_plan(request, manual_plan=merged)

    assert merged.gmail_query == 'newer_than:30d "Acme Compute"'
    assert merged.lookback_days == 30
    assert merged.gmail_date_scope == "unspecified"
    assert execution.operation == "contact_lookup"
    assert execution.gmail_query == 'newer_than:30d "Acme Compute"'


def test_known_contact_merge_recovers_ownerless_single_owner_llm_plan() -> None:
    request = (
        "CoS, which Acme Compute contact handled my startup onboarding, and what "
        "email address appears in their Gmail messages? Answer here only and "
        "don't change anything."
    )
    base = infer_manual_request_plan(request, requested_agent="gmail_triage")
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="gmail_triage",
        target_agent="orchestrator",
        intent="context_lookup",
        primary_target="Acme Compute startup onboarding",
        target_type="unknown",
        provider_system="unspecified",
        provider_operations=[],
        objective=request,
        task_objective="context_lookup",
        expected_artifact_type="context_summary",
        gmail_query="Acme Compute startup onboarding",
    )

    merged = merge_manual_request_plan(base, candidate)
    execution = resolve_gmail_execution_plan(request, manual_plan=merged)

    assert merged.target_agent == "gmail_triage"
    assert merged.intent == "gmail_triage"
    assert merged.provider_system == "gmail"
    assert merged.provider_operations == ["search", "read"]
    assert merged.task_objective == "contact_discovery"
    assert merged.expected_artifact_type == "contact_candidates"
    assert merged.gmail_query == '"Acme Compute"'
    assert execution.operation == "contact_lookup"
    assert execution.gmail_query == '"Acme Compute"'


@pytest.mark.parametrize(
    "request_text",
    [
        (
            "Check yesterday's inbox, choose the email I should answer first, "
            "and draft a reply here. Don't create a Gmail draft."
        ),
        (
            "Review yesterday's messages and select one that needs my attention. "
            "Write the response in Slack only."
        ),
    ],
)
def test_natural_mailbox_selection_is_a_collection_read(
    request_text: str,
) -> None:
    plan = infer_manual_request_plan(request_text, requested_agent="chief_of_staff")

    assert plan.provider_system == "gmail"
    assert plan.provider_operations == ["read"]
    assert plan.provider_read_scope == "bounded_collection"
    assert plan.provider_result_mode == "items"
    assert plan.gmail_mailbox_direction == "inbound"
    assert plan.gmail_date_scope == "yesterday"


def test_gmail_followup_new_day_does_not_reuse_prior_result_scope() -> None:
    prior_scope = ManualProviderResultSetScope(
        source_run_id="run-6584",
        provider_system="gmail",
        provider_read_scope="bounded_collection",
        target_type="gmail_message_collection",
        gmail_mailbox_direction="inbound",
        gmail_date_scope="today",
        query="to:me -in:sent after:1784951999 before:1785038400",
        timezone="America/New_York",
        window_start="2026-07-25T00:00:00-04:00",
        window_end="2026-07-26T00:00:00-04:00",
        item_count=0,
        complete=True,
        verified=True,
    )
    current_plan = ManualRequestPlan(
        source="llm",
        target_agent="gmail_triage",
        workflow=["gmail_triage", "outreach_composer"],
        intent="outreach_draft",
        provider_system="gmail",
        provider_operations=["search", "read"],
        provider_read_scope="bounded_collection",
        provider_result_mode="items",
        gmail_mailbox_direction="inbound",
        gmail_date_scope="yesterday",
        gmail_requested_fields=["subject", "sender", "date", "snippet"],
        target_type="gmail_message_collection",
        task_objective="outreach_draft",
        expected_artifact_type="outreach_draft",
        ask_shape=AskShapePolicy(output_form="draft", audience_scope="internal"),
    )

    reconciled = reconcile_manual_request_followup(
        current_plan,
        workflow_context={"prior_provider_result_scope": prior_scope.model_dump()},
    )

    assert reconciled.gmail_date_scope == "yesterday"
    assert reconciled.provider_result_scope is None
    assert reconciled.task_objective == "outreach_draft"
    assert reconciled.expected_artifact_type == "outreach_draft"


def test_airtable_aggregate_plan_represents_collection_not_single_item() -> None:
    plan = ManualRequestPlan(
        source="llm",
        target_agent="airtable_context_agent",
        provider_system="airtable",
        provider_operations=["read"],
        provider_read_scope="bounded_collection",
        provider_result_mode="aggregate",
        primary_target="Personal Expenses",
        target_type="business_system_context",
    )

    assert plan.provider_read_scope == "bounded_collection"
    assert plan.provider_result_mode == "aggregate"


def test_airtable_followup_projection_preserves_verified_aggregate_scope() -> None:
    prior_scope = ManualProviderResultSetScope(
        source_run_id="run-6426",
        provider_system="airtable",
        provider_read_scope="bounded_collection",
        target_type="business_system_context",
        item_count=4,
        airtable_base_alias="finance_tax_tracker",
        airtable_table="Personal Expenses",
        airtable_amount_field="Total Expenses",
        airtable_period_field="Estimated Tax Periods",
        airtable_date_field="Date of Expense",
        airtable_estimated_period=3,
        airtable_year=2026,
        aggregate_total="3041.53",
        aggregate_currency="USD",
        item_refs=["recOne", "recTwo", "recThree", "recFour"],
        complete=True,
        verified=True,
    )
    current_plan = ManualRequestPlan(
        source="llm",
        target_agent="airtable_context_agent",
        provider_system="airtable",
        provider_operations=["read"],
        provider_read_scope="bounded_collection",
        provider_result_mode="items",
        primary_target="Personal Expenses total for the current estimated-tax period",
        target_type="business_system_context",
    )

    reconciled = reconcile_manual_request_followup(
        current_plan,
        workflow_context={"prior_provider_result_scope": prior_scope.model_dump()},
    )

    assert reconciled.provider_result_mode == "items"
    assert reconciled.provider_read_scope == "bounded_collection"
    assert reconciled.desired_count == 4
    assert reconciled.provider_result_scope == prior_scope
    assert reconciled.provider_result_scope.item_refs == [
        "recOne",
        "recTwo",
        "recThree",
        "recFour",
    ]


def test_airtable_followup_does_not_reuse_scope_for_different_typed_table() -> None:
    prior_scope = ManualProviderResultSetScope(
        provider_system="airtable",
        provider_read_scope="bounded_collection",
        target_type="business_system_context",
        airtable_table="Personal Expenses",
        item_count=4,
        complete=True,
        verified=True,
    )
    current_plan = ManualRequestPlan(
        source="llm",
        target_agent="airtable_context_agent",
        provider_system="airtable",
        provider_operations=["read"],
        provider_read_scope="bounded_collection",
        provider_result_mode="items",
        primary_target="Business Expenses for the current period",
    )

    reconciled = reconcile_manual_request_followup(
        current_plan,
        workflow_context={"prior_provider_result_scope": prior_scope.model_dump()},
    )

    assert reconciled.provider_result_scope is None
    assert reconciled.primary_target == "Business Expenses for the current period"


def test_canonical_non_gmail_plan_cannot_reopen_gmail_from_request_words() -> None:
    manual_plan = ManualRequestPlan(
        source="canonical",
        target_agent="business_research_analyst",
        intent="research_brief",
        task_objective="source_research",
        expected_artifact_type="research_brief",
        provider_system="unspecified",
    )

    plan = resolve_gmail_execution_plan(
        "Check my latest email and summarize it.",
        manual_plan=manual_plan,
    )

    assert plan.operation == "clarification"
    assert plan.live_read_required is False
    assert plan.candidate_helpers == []
    assert plan.side_effect_policy == "no_gmail_action"


def test_heuristic_gmail_plan_keeps_legacy_phrase_fallback() -> None:
    manual_plan = ManualRequestPlan(
        source="heuristic",
        target_agent="chief_of_staff",
        intent="route_request",
    )

    plan = resolve_gmail_execution_plan(
        "Check my latest email and summarize it.",
        manual_plan=manual_plan,
    )

    assert plan.operation == "single_message_triage"
    assert plan.live_read_required is True


def test_manual_plan_preserves_quick_selected_thread_read_only_shape() -> None:
    plan = infer_manual_request_plan(
        "Give me a quick read of this Gmail thread. Do not search, draft, or send anything.",
        requested_agent="gmail_triage",
    )

    assert plan.ask_shape.ask_breadth == "narrow"
    assert plan.ask_shape.evidence_depth == "quick"
    assert plan.ask_shape.prior_context_dependency == "selected_context"
    assert plan.ask_shape.permission_state == "read_only"
    assert plan.ask_shape.cost_mode == "minimize"


@pytest.mark.parametrize(
    ("request_text", "expected_constraint"),
    [
        (
            "Review the selected Gmail thread; do not draft outreach or save anything.",
            "do not draft outreach or save anything",
        ),
        (
            "Summarize this article without modifying Zotero.",
            "without modifying zotero",
        ),
        (
            "Prepare the Slack copy but never post or schedule it.",
            "never post or schedule it",
        ),
        (
            "Find current opportunities; no outreach or CRM write.",
            "no outreach or crm write",
        ),
        (
            "Draft one reply, don't send it or create a provider draft.",
            "don't send it or create a provider draft",
        ),
    ],
)
def test_manual_plan_preserves_explicit_negative_clause_verbatim(
    request_text: str,
    expected_constraint: str,
) -> None:
    plan = infer_manual_request_plan(request_text, requested_agent="chief_of_staff")

    assert expected_constraint in plan.constraints


def test_llm_plan_cannot_drop_explicit_operator_negative_constraint() -> None:
    fallback = infer_manual_request_plan(
        "Find current opportunities; do not draft outreach or save them.",
        requested_agent="chief_of_staff",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="opportunity_scout",
        intent="opportunity_search",
        primary_target="current opportunities",
        constraints=["current", "source backed"],
    )

    merged = merge_manual_request_plan(
        fallback,
        candidate,
        allow_contextual_delegation=True,
    )

    assert merged.target_agent == "opportunity_scout"
    assert "do not draft outreach or save them" in merged.constraints


@pytest.mark.parametrize(
    "restriction",
    [
        "Do not search the web.",
        "Don't browse the internet.",
        "No search.",
        "Live web search is not allowed.",
        "Use only this supplied inline context.",
    ],
)
def test_semantically_equivalent_no_research_wording_disables_live_search(
    restriction: str,
) -> None:
    plan = infer_manual_request_plan(
        f"Identify the strongest advisory opportunity from this supplied note. {restriction}",
        requested_agent="chief_of_staff",
    )

    assert plan.target_agent == "opportunity_scout"
    assert plan.requires_live_search is False
    assert cli._request_forbids_live_research(plan.objective) is True


def test_canonical_search_plan_is_not_disabled_by_conflicting_background_prose() -> None:
    assert live_search_allowed_for_execution(
        True,
        manual_plan={
            "source": "llm",
            "target_agent": "business_research_analyst",
            "intent": "company_research",
            "primary_target": "Selected Health",
            "target_type": "company",
            "requires_live_search": True,
            "task_objective": "entity_research",
            "expected_artifact_type": "research_brief",
        },
        request_text=(
            "Find current sources for Selected Health. The old note says do not "
            "search, but that note is background only."
        ),
    )


def test_canonical_no_search_plan_is_not_reenabled_by_search_prose() -> None:
    assert not live_search_allowed_for_execution(
        True,
        manual_plan={
            "source": "llm",
            "target_agent": "business_research_analyst",
            "intent": "research_brief",
            "primary_target": "supplied note",
            "target_type": "topic",
            "requires_live_search": False,
            "task_objective": "source_research",
            "expected_artifact_type": "research_brief",
        },
        request_text="Search the web, although the current task uses only the supplied note.",
    )


def test_goal_based_cos_request_infers_ordered_workflow_without_agent_names() -> None:
    request = (
        "CoS, coordinate a tracked multi-step internal review using only these "
        "approved facts: one-owner work can run directly, multi-owner work uses a "
        "WorkItem, and provider receipts must be verified. Summarize the architecture "
        "tradeoff, identify the highest-value validation gap, then draft a concise "
        "internal Slack update for my review. Do not search the web, create or modify "
        "provider records, send email, or post anywhere."
    )

    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    routed = route_request(request, manual_plan=plan)

    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "route_request"
    assert plan.requires_approved_context is False
    assert plan.desired_count == 3
    assert plan.desired_count_explicit is False
    assert plan.ask_shape.output_form == "bullets"
    assert plan.workflow == [
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
    ]
    assert plan.side_effect_policy == "draft_or_read_only"
    assert routed.route == "business_research_analyst"
    assert routed.workflow == plan.workflow
    assert routed.refused is False
    assert routed.approved_context_present is True


def test_goal_based_cos_packet_review_keeps_chief_front_door() -> None:
    request = (
        "CoS, using the approved Northstar Behavioral Analytics packet, review what "
        "is known and unknown, "
        "identify the highest-value advisory opportunity and validation gap, then "
        "draft a concise internal Slack recommendation for my review. Use supplied "
        "materials only. Do not search the web, create provider records, send email, "
        "or post."
    )

    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    assert plan.target_agent == "chief_of_staff"
    assert plan.primary_target == "Northstar Behavioral Analytics"
    assert plan.workflow == [
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
    ]


def test_goal_based_cos_decision_brief_infers_workflow_from_natural_deliverables() -> None:
    request = (
        "CoS, I have five minutes before a partnership discussion. Here is all I know: "
        "Harbor Bridge Health sells behavioral-health care-navigation software to "
        "health plans and says it tracks referral completion and care engagement, but "
        "it has not shared audited outcomes, customer references, implementation data, "
        "or an evaluation design. Give me one decision brief: what is actually "
        "supported, the strongest potential KNI advisory or research fit, the single "
        "validation question that should come first, and a short internal Slack note I "
        "can paste to the team. Use only this note; do not search, create or modify "
        "anything, draft or send email, or post anywhere else."
    )

    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    assert plan.target_agent == "chief_of_staff"
    assert plan.workflow == [
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
    ]
    assert plan.requires_live_search is False
    assert plan.side_effect_policy == "draft_or_read_only"


def test_explicit_agent_workflow_is_diagnostic_advice_not_required_for_same_plan() -> None:
    natural_request = (
        "CoS, using these approved facts: the control plane routes requests and "
        "receipts verify completion. Review the architecture tradeoff, prioritize "
        "the most important validation gap, and prepare an internal Slack brief for "
        "review. Do not send or post it."
    )
    explicit_request = (
        "CoS, using these approved facts: the control plane routes requests and "
        "receipts verify completion. Have Business Research review the architecture "
        "tradeoff, Opportunity Scout prioritize the most important validation gap, "
        "and Outreach Composer prepare an internal Slack brief for review. Do not "
        "send or post it."
    )

    natural = infer_manual_request_plan(natural_request, requested_agent="chief_of_staff")
    explicit = infer_manual_request_plan(explicit_request, requested_agent="chief_of_staff")

    assert (
        natural.workflow
        == explicit.workflow
        == [
            "business_research_analyst",
            "opportunity_scout",
            "outreach_composer",
        ]
    )


def test_llm_workflow_survives_merge_for_chief_of_staff_front_door() -> None:
    fallback = infer_manual_request_plan(
        "CoS, review this internal question and coordinate the necessary work.",
        requested_agent="chief_of_staff",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        workflow=[
            "business research analyst",
            "opportunity_scout",
            "opportunity_scout",
            "outreach composer",
        ],
        intent="route_request",
        objective="Review evidence, prioritize the gap, and draft an internal update.",
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.target_agent == "chief_of_staff"
    assert merged.workflow == [
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
    ]


def test_invalid_llm_clarification_does_not_restore_heuristic_graph() -> None:
    request = (
        "CoS, NeuroFlow is a behavioral health company I want to evaluate. Research "
        "it using current public sources, identify the strongest plausible KNI "
        "advisory or research opportunity and the most important validation gap, "
        "then draft a concise internal Slack recommendation for my review. Cite the "
        "sources you rely on. Do not send email, create provider records, or post "
        "anywhere else."
    )
    fallback = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    incomplete_candidate = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        workflow=["business_research_analyst", "chief_of_staff"],
        intent="clarification",
        objective=request,
    )

    merged = merge_manual_request_plan(fallback, incomplete_candidate)

    assert fallback.primary_target == "NeuroFlow"
    assert merged.target_agent == "chief_of_staff"
    assert merged.workflow == []
    assert merged.intent == "route_request"
    assert merged.requires_durable_state is False
    assert any("No keyword route was restored" in item for item in merged.planner_warnings)


def test_negated_provider_record_actions_do_not_select_write_intent() -> None:
    plan = infer_manual_request_plan(
        "CoS, summarize the architecture and draft an internal update for review. "
        "Do not create or modify provider records, send email, or post.",
        requested_agent="chief_of_staff",
    )

    assert plan.intent != "business_system_write"


def test_manual_plan_treats_negated_calendar_add_as_read_only_gmail_triage() -> None:
    plan = infer_manual_request_plan(
        "Gmail triage: if my latest email includes a meeting time, extract the "
        "calendar details and show staged event details. Do not add it to the calendar.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "gmail_triage"
    assert plan.intent == "gmail_triage"
    assert plan.ask_shape.permission_state == "read_only"
    assert plan.task_objective == "gmail_triage"


def test_personal_schedule_question_selects_typed_calendar_read_without_provider_jargon() -> None:
    plan = infer_manual_request_plan(
        "CoS, what interviews do I have tomorrow?",
        requested_agent="chief_of_staff",
    )

    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "context_lookup"
    assert plan.target_type == "business_system_context"
    assert plan.provider_system == "google_calendar"
    assert plan.provider_operations == ["read"]
    assert plan.provider_read_scope == "bounded_collection"
    assert plan.provider_result_mode == "items"
    assert plan.ask_shape.permission_state == "read_only"


def test_manual_plan_preserves_exact_sentence_summary_stop_shape() -> None:
    plan = infer_manual_request_plan(
        "Summarize Example Health in exactly 2 sentences using fixture context only. "
        "Read-only; no live search or external tools.",
        requested_agent="business_research_analyst",
    )

    assert plan.ask_shape.strict_filter_mode == "exact"
    assert plan.ask_shape.output_form == "brief"
    assert plan.ask_shape.permission_state == "read_only"
    assert plan.ask_shape.stop_condition == "stop_after_exact_requested_sentence_count"


def test_manual_plan_preserves_requested_summary_word_limit() -> None:
    plan = infer_manual_request_plan(
        "Provide the exact title and summarize the stored abstract in no more than 50 words.",
        requested_agent="zotero_context_agent",
    )

    assert plan.ask_shape.output_form == "brief"
    assert plan.ask_shape.strict_filter_mode == "exact"
    assert plan.ask_shape.stop_condition == "stop_after_50_word_summary"


def test_manual_plan_preserves_staged_approval_stop_against_weaker_planner() -> None:
    base = infer_manual_request_plan(
        "Research first, then draft only after approval.", requested_agent="orchestrator"
    )
    candidate = ManualRequestPlan.model_validate(
        {
            **base.model_dump(mode="json"),
            "source": "llm",
            "ask_shape": {
                "permission_state": "unspecified",
                "stop_condition": "",
                "output_form": "draft",
            },
        }
    )
    merged = merge_manual_request_plan(base, candidate)

    assert merged.ask_shape.permission_state == "approval_required"
    assert merged.ask_shape.stop_condition == "stop_before_external_action_until_approval"
    assert merged.ask_shape.output_form == "draft"


def test_llm_interpreted_output_constraints_override_heuristic_fallback() -> None:
    fallback = infer_manual_request_plan(
        "Summarize Abridge in 20 words.",
        requested_agent="business_research_analyst",
    )
    candidate = fallback.model_copy(deep=True)
    candidate.source = "llm"
    candidate.ask_shape.output_constraints = candidate.ask_shape.output_constraints.model_copy(
        update={
            "interpretation": "maximum 20-word answer because the operator used it as a cap",
            "word_count_mode": "maximum",
        }
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.ask_shape.output_constraints.word_count == 20
    assert merged.ask_shape.output_constraints.word_count_mode == "maximum"
    assert "because the operator" in merged.ask_shape.output_constraints.interpretation


def test_manual_planner_prompt_distinguishes_content_from_explicit_headings() -> None:
    prompt = Path("src/keystone_agents/prompts/manual_request_planner.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(prompt.split())

    assert "require_section_headings=true" in normalized
    assert "describe content the specialist must cover" in normalized
    assert "preserve them in `interpretation`, not as mandatory literal headings" in normalized
    assert "Do not invent sections for ordinary content questions" in normalized


def test_manual_planner_prompt_includes_bounded_thread_context() -> None:
    planner_input = ManualRequestPlannerInput(
        request_text="Add this link to the notes: https://example.test/reference",
        requested_agent="chief_of_staff",
        fallback_plan=ManualRequestPlan(requested_agent="chief_of_staff"),
        workflow_context={
            "recent_slack_thread": [
                {
                    "summary": "Zotero article resolved: Example study",
                    "object_id": "internal-item-key",
                }
            ]
        },
    )

    prompt = planner_input.to_prompt()

    assert "Zotero article resolved" in prompt
    assert "Add this link to the notes" in prompt
    assert prompt.index("Operator request:") < prompt.index("Bounded prior thread/work-item")
    assert "Local fallback plan for reference:" not in prompt
    assert "local keyword or phrase classifiers" in prompt


def test_manual_planner_prompt_flags_marked_lifecycle_for_semantic_normalization() -> None:
    planner_input = ManualRequestPlannerInput(
        request_text=(
            "CoS, put a short note in my Gmail drafts with "
            "KBA_TEST_DRAFT_SEMANTIC. Tighten the same saved note, check that "
            "the revision stuck, then throw away only that test draft. Do not send."
        ),
        requested_agent="chief_of_staff",
    )

    prompt = planner_input.to_prompt()

    assert "Marked provider lifecycle attention:" in prompt
    assert "Interpret the full requested operation sequence by meaning" in prompt
    assert "intent=business_system_write" in prompt
    assert "This planning hint does not approve or execute any provider write" in prompt


def test_manual_planner_prompt_does_not_add_lifecycle_hint_to_ordinary_gmail_reply() -> None:
    planner_input = ManualRequestPlannerInput(
        request_text="Read the latest Gmail thread and draft a reply here without sending.",
        requested_agent="chief_of_staff",
    )

    assert "Marked provider lifecycle attention:" not in planner_input.to_prompt()


def test_manual_planner_context_keeps_newest_messages_and_raw_slack_fields() -> None:
    context = _compact_manual_planner_context(
        {
            "recent_slack_thread": [
                {
                    "ts": f"17153664{index:02d}.000100",
                    "user_id": f"U{index:03d}",
                    "text": f"thread message {index}",
                }
                for index in range(10)
            ],
            "prior_agent_runs": [
                {"id": f"run-{index}", "route": "chief_of_staff"} for index in range(7)
            ],
        }
    )

    assert [item["summary"] for item in context["recent_slack_thread"]] == [
        f"thread message {index}" for index in range(2, 10)
    ]
    assert context["recent_slack_thread"][-1]["id"] == "1715366409.000100"
    assert context["recent_slack_thread"][-1]["source_agent"] == "U009"
    assert [item["id"] for item in context["prior_agent_runs"]] == [
        f"run-{index}" for index in range(2, 7)
    ]
    assert context["context_compaction"]["thread_messages_dropped_oldest"] == 2
    assert context["context_compaction"]["prior_runs_dropped_oldest"] == 2


def test_manual_planner_context_preserves_thread_root_and_latest_correction() -> None:
    transcript = (
        "ROOT: create the first bounded object.\n"
        + ("middle context " * 700)
        + "\nLATEST: actually use the Gmail draft and do not send it."
    )

    context = _compact_manual_planner_context({"slack_thread_transcript": transcript})
    bounded = context["slack_thread_transcript"]

    assert len(bounded) <= 6000
    assert bounded.startswith("ROOT: create the first bounded object.")
    assert bounded.endswith("LATEST: actually use the Gmail draft and do not send it.")
    assert "middle of Slack thread omitted" in bounded
    assert context["context_compaction"]["transcript_compacted"] is True
    assert context["context_compaction"]["transcript_original_chars"] == len(transcript)


def test_manual_planner_context_keeps_typed_prior_owner_as_advice() -> None:
    context = _compact_manual_planner_context(
        {
            "execution_continuation": {
                "prior_agent": "business_research_analyst",
                "prior_request": "Assess the supplied company note.",
            }
        }
    )

    assert context["execution_continuation"] == {
        "prior_agent": "business_research_analyst",
        "prior_request": "Assess the supplied company note.",
    }


def test_manual_planner_context_retains_bounded_verified_provider_objects() -> None:
    context = _compact_manual_planner_context(
        {
            "execution_continuation": {
                "provider_affinity": "google_workspace",
                "verified_objects": [
                    {
                        "provider_system": "google_drive",
                        "object_type": "google_document",
                        "object_id": "doc_123",
                        "display_name": "Operating Model",
                        "lifecycle_state": "active",
                        "verification_status": "verified",
                        "provider_scope": {"folder_path": "KNIOps"},
                    },
                    {
                        "provider_system": "gmail",
                        "object_type": "gmail_message",
                        "object_id": "msg_unverified",
                        "lifecycle_state": "active",
                        "verification_status": "unverified",
                    },
                ],
            }
        }
    )

    continuation = context["execution_continuation"]
    assert continuation["provider_affinity"] == "google_workspace"
    assert len(continuation["verified_objects"]) == 1
    assert continuation["verified_objects"][0]["object_id"] == "doc_123"
    assert continuation["verified_objects"][0]["provider_scope"] == {
        "folder_path": "KNIOps"
    }
    assert (
        context["context_compaction"]["verified_provider_objects_retained"]
        == 1
    )


def test_manual_planner_context_preserves_exact_current_workitem_identity() -> None:
    context = _compact_manual_planner_context(
        {
            "current_work_item": {
                "id": "wi_exact",
                "kind": "gmail_triage",
                "status": "in_progress",
                "route": "gmail_triage",
                "title": "Revise marked draft",
                "prior_request": "Create the marked draft without sending it.",
                "target": {
                    "name": "KBA_TEST_DRAFT_EXACT",
                    "object_type": "gmail_draft",
                    "external_id": "draftExact123",
                    "unapproved_extra": "must not survive",
                },
                "selected_artifacts": [
                    {
                        "artifact_type": "gmail_draft",
                        "artifact_id": "draftExact123",
                        "source_agent": "gmail_triage",
                        "title": "KBA_TEST_DRAFT_EXACT",
                        "private_body": "must not survive",
                    }
                ],
                "next_action": {
                    "action": "revise_draft",
                    "agent": "gmail_triage",
                    "description": "Revise only the exact selected draft.",
                    "command_hint": "must not survive",
                },
                "metadata": {"provider_payload": "must not survive"},
            }
        }
    )

    serialized = json.dumps(context, sort_keys=True)
    assert context["current_work_item"]["target"]["external_id"] == "draftExact123"
    assert context["current_work_item"]["selected_artifacts"][0]["artifact_id"] == "draftExact123"
    assert context["current_work_item"]["next_action"]["action"] == "revise_draft"
    assert context["context_compaction"]["work_item_identity_expanded"] is True
    assert context["context_compaction"]["selected_artifacts_retained"] == 1
    assert "must not survive" not in serialized


@pytest.mark.parametrize(
    ("context_summary", "follow_up", "target_agent", "intent"),
    [
        (
            "Airtable record created and verified in the Projects table.",
            "Update this record with status Reviewed.",
            "airtable_context_agent",
            "business_system_write",
        ),
        (
            "Google Docs document created and verified.",
            "Append this paragraph to the document.",
            "google_workspace_context_agent",
            "business_system_write",
        ),
        (
            "Zotero article resolved with one exact item key.",
            "Add a note to this paper.",
            "zotero_context_agent",
            "business_system_write",
        ),
        (
            "Gmail draft created and verified for the selected thread.",
            "Make this shorter and add the link.",
            "gmail_triage",
            "gmail_triage",
        ),
        (
            "The selected Slack message is a marked test post.",
            "Edit that message to include the link.",
            "chief_of_staff",
            "slack_operations",
        ),
        (
            "Research result 2 is the source-backed article requested above.",
            "Summarize link 2 from the prior results.",
            "business_research_analyst",
            "research_brief",
        ),
    ],
)
def test_contextual_follow_up_routes_to_source_owner_without_repeating_system_name(
    context_summary: str,
    follow_up: str,
    target_agent: str,
    intent: str,
) -> None:
    plan = resolve_manual_request_plan(
        follow_up,
        requested_agent="chief_of_staff",
        workflow_state={
            "recent_slack_thread": [{"summary": context_summary}],
            "slack_context": {"thread_ts": "1715366400.000100"},
        },
    )

    assert plan.target_agent == target_agent
    assert plan.intent == intent
    assert not any("did not contain enough information" in item for item in plan.planner_warnings)


@pytest.mark.parametrize(
    ("provider_affinity", "follow_up", "target_agent", "intent", "provider_system"),
    [
        (
            "calendar",
            "What date is the UT Course Orientation Session?",
            "chief_of_staff",
            "context_lookup",
            "google_calendar",
        ),
        (
            "airtable",
            "Which amount was recorded?",
            "airtable_context_agent",
            "context_lookup",
            "airtable",
        ),
        (
            "google_workspace",
            "What is the document title?",
            "google_workspace_context_agent",
            "context_lookup",
            "google_workspace",
        ),
        (
            "gmail",
            "What does the sender need?",
            "gmail_triage",
            "gmail_triage",
            "gmail",
        ),
        (
            "zotero",
            "Who authored the selected paper?",
            "zotero_context_agent",
            "context_lookup",
            "zotero",
        ),
    ],
)
def test_typed_provider_affinity_routes_fully_named_followup_without_phrase_dependency(
    provider_affinity: str,
    follow_up: str,
    target_agent: str,
    intent: str,
    provider_system: str,
) -> None:
    plan = resolve_manual_request_plan(
        follow_up,
        workflow_state={
            "execution_continuation": {
                "provider_affinity": provider_affinity,
                "prior_request": "Prior provider-owned request",
            },
            "recent_slack_thread": [
                {"summary": "Historical agent output mentioned Gmail and Airtable."}
            ],
        },
    )

    assert plan.target_agent == target_agent
    assert plan.intent == intent
    assert plan.provider_system == provider_system


def test_explicit_calendar_create_is_not_rewritten_by_prior_slack_context() -> None:
    request = (
        "@KNI CoS, add event for Q3 estimated payment due on September 15th, "
        "2026 as all day event in google calendar."
    )

    plan = resolve_manual_request_plan(
        request,
        requested_agent="chief_of_staff",
        workflow_state={
            "execution_continuation": {
                "prior_agent": "chief_of_staff",
                "provider_affinity": "",
            },
            "recent_slack_thread": [{"summary": "Earlier Chief response."}],
            "slack_context": {"thread_ts": "1785790917.312769"},
        },
    )

    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "business_system_write"
    assert plan.provider_system == "google_calendar"
    assert plan.provider_operations == ["create"]
    assert plan.primary_target == (
        "add event for Q3 estimated payment due on September 15th, 2026 as all day "
        "event in google calendar"
    )


def test_calendar_approval_continuation_preserves_exact_prior_operation_and_target() -> None:
    prior_request = (
        "@KNI CoS, add event for Q3 estimated payment due on September 15th, "
        "2026 as all day event in google calendar."
    )

    plan = resolve_manual_request_plan(
        "Approved to write it.",
        requested_agent="chief_of_staff",
        workflow_state={
            "execution_continuation": {
                "prior_agent": "chief_of_staff",
                "provider_affinity": "calendar",
                "prior_request": prior_request,
            },
            "recent_slack_thread": [
                {"summary": "The requested Calendar write still needs execution."}
            ],
            "slack_context": {"thread_ts": "1785790917.312769"},
        },
    )

    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "business_system_write"
    assert plan.provider_system == "google_calendar"
    assert plan.provider_operations == ["create"]
    assert plan.primary_target == (
        "add event for Q3 estimated payment due on September 15th, 2026 as all day "
        "event in google calendar"
    )


@pytest.mark.parametrize(
    "workflow_state",
    [
        {},
        {
            "execution_continuation": {
                "prior_agent": "chief_of_staff",
                "provider_affinity": "calendar",
                "prior_request": "List tomorrow's calendar events without changing them.",
            }
        },
        {
            "execution_continuation": {
                "prior_agent": "chief_of_staff",
                "provider_affinity": "gmail",
                "prior_request": (
                    "Add Q3 estimated payment due on September 15th, 2026 to "
                    "Google Calendar."
                ),
            }
        },
    ],
)
def test_bare_approval_cannot_invent_or_cross_provider_write_authority(
    workflow_state: dict[str, object],
) -> None:
    plan = resolve_manual_request_plan(
        "Approved to write it.",
        requested_agent="chief_of_staff",
        workflow_state=workflow_state,
    )

    assert not (
        plan.intent == "business_system_write"
        and plan.provider_operations == ["create"]
    )
    assert plan.workflow == []
    assert not any("did not contain enough information" in item for item in plan.planner_warnings)


def test_typed_prior_owner_supports_provider_free_formatting_without_becoming_explicit() -> None:
    plan = resolve_manual_request_plan(
        "Make that just the three bullets.",
        workflow_state={
            "execution_continuation": {
                "prior_agent": "chief_of_staff",
                "prior_request": "Using only these facts, return exactly three bullets.",
            }
        },
    )

    assert plan.requested_agent is None
    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "route_request"
    assert plan.provider_system == "unspecified"
    assert plan.workflow == []


@pytest.mark.parametrize(
    ("follow_up", "target_agent", "intent", "provider_system"),
    [
        ("Draft an email from it.", "gmail_triage", "gmail_triage", "gmail"),
        (
            "Find a current grant from it.",
            "opportunity_scout",
            "opportunity_search",
            "unspecified",
        ),
    ],
)
def test_current_followup_capability_supersedes_typed_prior_owner(
    follow_up: str,
    target_agent: str,
    intent: str,
    provider_system: str,
) -> None:
    plan = resolve_manual_request_plan(
        follow_up,
        workflow_state={
            "execution_continuation": {
                "prior_agent": "business_research_analyst",
                "prior_request": "Assess the supplied company note.",
            }
        },
    )

    assert plan.target_agent == target_agent
    assert plan.intent == intent
    assert plan.provider_system == provider_system


def test_current_request_explicit_source_beats_prior_thread_source() -> None:
    plan = resolve_manual_request_plan(
        "Create a Google Doc with this summary.",
        requested_agent="chief_of_staff",
        workflow_state={
            "recent_slack_thread": [{"summary": "Airtable record created and verified."}]
        },
    )

    assert plan.target_agent == "google_workspace_context_agent"


def test_ambiguous_thread_referent_does_not_guess_between_provider_owners() -> None:
    preflight = run_orchestrator_preflight(
        "Update it with status Reviewed.",
        requested_agent="chief_of_staff",
        workflow_state={
            "recent_slack_thread": [
                {"summary": "Airtable record created and provider verified."},
                {"summary": "Gmail draft created and provider verified."},
            ],
            "slack_context": {"thread_ts": "1715366400.000100"},
        },
    )

    assert preflight.manual_request_plan.target_agent == "clarification"
    assert preflight.manual_request_plan.side_effect_policy == "draft_or_read_only"
    assert preflight.route_result.route == "clarification"
    assert preflight.route_result.requires_human_review is True
    assert preflight.route_result.clarification_request
    assert any(
        "multiple source owners" in warning
        for warning in preflight.manual_request_plan.planner_warnings
    )


def test_historical_agent_output_cannot_select_owner_over_operator_root_and_current_turn() -> None:
    plan = resolve_manual_request_plan(
        (
            "CoS, please fix the last reply and give me only the same three concise "
            "bullets from my original request."
        ),
        requested_agent="chief_of_staff",
        workflow_state={
            "slack_thread_root": (
                "CoS, using only these facts, give me exactly three bullets: one "
                "shared request envelope; separate direct and stateful backends; "
                "provider completion only after receipt verification."
            ),
            "recent_slack_thread": [
                {
                    "role": "operator",
                    "source_agent": "UUSER",
                    "summary": ("CoS, using only these facts, give me exactly three bullets."),
                },
                {
                    "role": "agent",
                    "source_agent": "kni",
                    "summary": ("Business Research Analyst article summary from a stale route."),
                },
                {
                    "role": "operator",
                    "source_agent": "UUSER",
                    "summary": "CoS, give me the same three bullets and nothing else.",
                },
            ],
        },
    )

    assert plan.target_agent == "chief_of_staff"
    assert plan.intent != "clarification"
    assert plan.workflow == []


def test_orchestrator_preflight_uses_context_before_underspecified_modify_blocker() -> None:
    preflight = run_orchestrator_preflight(
        "Update this record with status Reviewed.",
        requested_agent="chief_of_staff",
        workflow_state={
            "slack_thread_transcript": (
                "1. operator: Create a marked Airtable record.\n"
                "2. agent: Airtable record created and provider verified."
            ),
            "recent_slack_thread": [{"summary": "Airtable record created and provider verified."}],
        },
    )

    assert preflight.execution_allowed is True
    assert preflight.manual_request_plan.target_agent == "airtable_context_agent"
    assert preflight.manual_request_plan.intent == "business_system_write"
    assert preflight.route_result.route == "airtable_context_agent"


def test_manual_planner_reads_thread_evidence_from_nested_slack_context() -> None:
    plan = resolve_manual_request_plan(
        "Attach this PDF to the article.",
        requested_agent="chief_of_staff",
        workflow_state={
            "slack_context": {
                "thread_ts": "1715366400.000100",
                "thread_transcript": (
                    "1. operator: One exact Zotero article was resolved.\n"
                    "2. agent: Zotero article KBA_TEST_ARTICLE is the current target."
                ),
                "thread_messages": [{"text": "One exact Zotero article was resolved."}],
            }
        },
    )

    assert plan.target_agent == "zotero_context_agent"
    assert plan.intent == "business_system_write"


def test_manual_plan_routes_conference_search_to_opportunity_scout() -> None:
    plan = infer_manual_request_plan(
        "Find 5 broad behavioral health AI opportunities across conferences "
        "where Keystone could offer implementation oriented presentations.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_search"
    assert plan.target_type == "conference"
    assert plan.task_objective == "opportunity_discovery"
    assert plan.expected_artifact_type == "opportunity_record"
    assert plan.desired_count == 5
    assert plan.desired_count_explicit is True
    assert plan.requires_live_search is True


def test_context_agent_external_send_request_is_blocked() -> None:
    plan = infer_manual_request_plan(
        "@KNI rss context agent: send the announcement summary to Slack",
        requested_agent="orchestrator",
    )

    assert plan.intent == "blocked_send"
    assert plan.target_agent == "rss_context_agent"
    assert plan.task_objective == "blocked_side_effect"
    assert plan.planner_warnings


def test_context_agent_internal_update_routes_to_owner_for_approval_gating() -> None:
    plan = infer_manual_request_plan(
        "@KNI airtable context agent: update the tracker row for this case",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "airtable_context_agent"
    assert plan.intent == "business_system_write"
    assert plan.task_objective == "business_system_write"
    assert plan.expected_artifact_type == "business_system_write_plan"
    assert plan.side_effect_policy == "internal_write_approval_required"


def test_chief_scoped_airtable_lifecycle_is_not_misclassified_as_send() -> None:
    request = (
        "Using Airtable context, create one marked KBA test expense in the Business "
        "Expenses table, verify it, update the same record description, verify it "
        "again, and remove only that test record."
    )

    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    assert plan.requested_agent == "chief_of_staff"
    assert plan.target_agent == "airtable_context_agent"
    assert plan.intent == "business_system_write"
    assert plan.task_objective == "business_system_write"
    assert plan.expected_artifact_type == "business_system_write_plan"
    assert plan.side_effect_policy == "internal_write_approval_required"
    assert plan.planner_warnings == []


def test_workspace_lifecycle_ignores_negated_outbound_actions() -> None:
    request = (
        "Create one temporary Sheet named KBA_TEST_SHEET pair-control in KNIOps, "
        "add a marked KBA_TEST_ROW, read it back, update the same row, verify it, "
        "delete the marked row, move the same test Sheet to trash, and confirm "
        "cleanup. Do not share, send, post, or modify any unrelated file."
    )

    plan = infer_manual_request_plan(request, requested_agent="orchestrator")

    assert plan.target_agent == "google_workspace_context_agent"
    assert plan.intent == "business_system_write"
    assert plan.task_objective == "business_system_write"
    assert plan.expected_artifact_type == "business_system_write_plan"
    assert plan.side_effect_policy == "internal_write_approval_required"
    assert plan.planner_warnings == []


def test_workspace_save_plan_stays_read_only_until_execution_is_requested() -> None:
    plan = infer_manual_request_plan(
        "Prepare a save plan for the NeuroFlow research brief in Google Drive. "
        "Do not create the file.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "google_workspace_context_agent"
    assert plan.intent == "context_lookup"
    assert plan.side_effect_policy == "draft_or_read_only"


def test_context_agent_negated_side_effect_constraints_remain_read_only_context() -> None:
    plan = infer_manual_request_plan(
        "@KNI rss context agent: inspect announcement history. Do not post to Slack "
        "or write files.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "rss_context_agent"
    assert plan.intent == "context_lookup"
    assert plan.planner_warnings == []


def test_airtable_curly_apostrophe_negative_actions_never_grant_write_tools() -> None:
    plan = infer_manual_request_plan(
        "Could you check the finance base and summarize how many vendor expenses "
        "were logged during June 2026, along with total spend by category? Please "
        "inspect the fields first and only read the records; don’t add, alter, "
        "attach, or remove anything.",
        requested_agent="airtable_context_agent",
    )

    assert plan.target_agent == "airtable_context_agent"
    assert plan.intent == "context_lookup"
    assert plan.ask_shape.permission_state == "read_only"
    assert set(plan.provider_operations) <= {"read", "search", "verify"}


@pytest.mark.parametrize(
    "lead",
    [
        "I’m reviewing Ellipsis Health.",
        "I am assessing Ellipsis Health for a possible engagement.",
        "We’re looking at Ellipsis Health before planning next quarter.",
        "We are considering Ellipsis Health as a research partner.",
    ],
)
def test_conversational_company_research_lead_extracts_named_company(
    lead: str,
) -> None:
    plan = infer_manual_request_plan(
        f"{lead} Verify its current public evidence and partnership signals.",
        requested_agent="business_research_analyst",
    )

    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"
    assert plan.primary_target == "Ellipsis Health"


def test_slack_thread_sample_reply_is_draft_text_not_send_blocker() -> None:
    prompt = (
        "Using this context: Mindful Care is asking whether Keystone can help with "
        "measurement-based care workflow evaluation design. Write a short "
        "Slack-thread sample reply for review."
    )

    plan = infer_manual_request_plan(prompt, requested_agent="outreach_composer")
    result = route_request(prompt, manual_plan=plan)

    assert plan.target_agent == "outreach_composer"
    assert plan.intent == "outreach_draft"
    assert result.route == "outreach_composer"
    assert result.refused is False
    assert result.send_enabled is False


@pytest.mark.parametrize("spec_id", ["BR-1", "BR-4"])
def test_manual_plan_keeps_research_brief_prompts_with_outreach_mentions_on_research(
    spec_id: str,
) -> None:
    plan = infer_manual_request_plan(
        get_test_pack_spec(spec_id).natural_prompt,
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"
    assert plan.task_objective in {"entity_research", "source_research"}
    assert plan.expected_artifact_type in {"company_profile", "source_summary"}
    if spec_id == "BR-1":
        assert plan.primary_target == "Lindus Health"
    else:
        assert plan.primary_target == "Insert company"


def test_manual_plan_routes_company_comparison_to_business_research() -> None:
    plan = infer_manual_request_plan(
        get_test_pack_spec("BR-3").natural_prompt,
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"
    assert plan.primary_target == "Lindus Health vs Holmusk"


def test_manual_plan_routes_public_contact_discovery_without_outreach() -> None:
    plan = infer_manual_request_plan(
        "Find a public business-development contact for NeuroFlow, but do not draft outreach.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"
    assert plan.primary_target == "NeuroFlow"
    assert plan.target_type == "company"
    assert plan.task_objective == "contact_discovery"
    assert plan.expected_artifact_type == "contact_candidates"
    assert plan.side_effect_policy == "draft_or_read_only"


@pytest.mark.parametrize(
    ("request_text", "expected_query"),
    [
        (
            "CoS, what is the email address of the person from Acme Compute who "
            "set up my startup account?",
            '"Acme Compute"',
        ),
        (
            "What address should I use for the Acme Compute person who onboarded me?",
            '"Acme Compute"',
        ),
        (
            "I spoke with someone from Acme Compute during account activation; "
            "can you find their email?",
            '"Acme Compute"',
        ),
        (
            "Who handled my Acme Compute setup and how do I reach them?",
            '"Acme Compute"',
        ),
        (
            "CoS, who at Acme Compute helped set up my startup account, and "
            "what email address did they use? Check Gmail.",
            '"Acme Compute"',
        ),
        (
            "Which Acme Compute contact handled my onboarding?",
            '"Acme Compute"',
        ),
    ],
)
def test_manual_plan_routes_known_contact_recovery_to_gmail_evidence(
    request_text: str,
    expected_query: str,
) -> None:
    plan = infer_manual_request_plan(request_text, requested_agent="chief_of_staff")

    assert plan.requested_agent == "chief_of_staff"
    assert plan.target_agent == "gmail_triage"
    assert plan.intent == "gmail_triage"
    assert plan.provider_system == "gmail"
    assert plan.provider_operations == ["search", "read"]
    assert plan.provider_read_scope == "bounded_collection"
    assert plan.provider_result_mode == "items"
    assert plan.gmail_mailbox_direction == "any"
    assert plan.target_type == "gmail_message_collection"
    assert plan.task_objective == "contact_discovery"
    assert plan.expected_artifact_type == "contact_candidates"
    assert plan.primary_target == "Acme Compute"
    assert plan.gmail_query == expected_query


def test_manual_plan_keeps_public_contact_discovery_out_of_gmail() -> None:
    plan = infer_manual_request_plan(
        "Find the public partnerships lead and email address for Acme Compute.",
        requested_agent="chief_of_staff",
    )

    assert plan.target_agent != "gmail_triage"
    assert plan.provider_system != "gmail"


def test_manual_plan_preserves_business_research_smoke_with_context_edges() -> None:
    plan = infer_manual_request_plan(
        "LangGraph smoke 1: use preprints context agent history and Zotero context "
        "agent handoff, then run Business Research for NeuroFlow as an internal "
        "evidence-packet planning note. Live SDK is approved only for this bounded "
        "read-only smoke if the backend would normally use it; live web search is "
        "not approved. Use local/dry-run retrieval where possible. Do not send "
        "email, create drafts, post elsewhere, publish, schedule, or write external "
        "systems.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "research_brief"
    assert "NeuroFlow" in plan.primary_target
    assert plan.task_objective == "source_research"
    assert plan.expected_artifact_type == "source_summary"
    assert plan.requires_live_search is False


def test_manual_plan_routes_zotero_note_lifecycle_to_context_owner() -> None:
    plan = infer_manual_request_plan(
        "Create a temporary Zotero note for this research item, revise it to be clearer, "
        "verify the change, and remove the temporary note.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "zotero_context_agent"
    assert plan.intent == "business_system_write"
    assert plan.task_objective == "business_system_write"
    assert plan.expected_artifact_type == "business_system_write_plan"
    assert plan.side_effect_policy == "internal_write_approval_required"


def test_chief_delegates_complete_zotero_note_lifecycle_to_context_owner() -> None:
    plan = infer_manual_request_plan(
        "Create one marked standalone Zotero test note, verify it, revise the same "
        "note to be clearer, verify it again, and remove only that test note.",
        requested_agent="chief_of_staff",
    )

    assert plan.target_agent == "zotero_context_agent"
    assert plan.intent == "business_system_write"
    assert plan.side_effect_policy == "internal_write_approval_required"


def test_research_diligence_note_is_not_misclassified_as_provider_write() -> None:
    plan = infer_manual_request_plan(
        "@KNI business research analyst write a concise diligence note for CareNav AI "
        "with labeled fields for "
        "product, buyer, evidence, risk, and Keystone fit.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "business_research_analyst"
    assert plan.intent != "blocked_send"


@pytest.mark.parametrize(
    ("prompt", "target_agent", "intent"),
    [
        (
            "Research Lindus Health and recommend the next step.",
            "business_research_analyst",
            "company_research",
        ),
        (
            "Find behavioral health AI opportunities and recommend the next step.",
            "opportunity_scout",
            "opportunity_search",
        ),
        (
            "Draft outreach for Lindus Health and include the next step, do not send.",
            "outreach_composer",
            "outreach_draft",
        ),
        (
            "Summarize this email thread and list the next step.",
            "gmail_triage",
            "gmail_triage",
        ),
    ],
)
def test_manual_plan_keeps_next_step_output_language_on_specialist_routes(
    prompt: str,
    target_agent: str,
    intent: str,
) -> None:
    plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    assert plan.target_agent == target_agent
    assert plan.intent == intent
    if target_agent == "opportunity_scout":
        assert plan.primary_target == "behavioral health AI opportunities"


@pytest.mark.parametrize(
    "prompt",
    [
        "continue this WorkItem",
        "resume the workflow",
        "what's next for this work item?",
        "next step for this workflow",
    ],
)
def test_manual_plan_still_routes_explicit_continuations_to_orchestrator(prompt: str) -> None:
    plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    assert plan.target_agent == "orchestrator"
    assert plan.intent == "continue_work_item"


def test_manual_plan_routes_recent_remote_role_search_to_opportunity_scout() -> None:
    plan = infer_manual_request_plan(
        (
            "Find up to 5 active U.S.-based remote roles posted in the last 7 days "
            "for a physician-scientist with behavioral health, clinical research, "
            "and AI experience. Exclude AI tutor roles."
        ),
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_search"
    assert plan.target_type == "opportunity"
    assert plan.task_objective == "opportunity_discovery"
    assert plan.expected_artifact_type == "opportunity_record"
    assert plan.desired_count == 5
    assert "remote" in plan.constraints
    assert "exclude AI tutor roles" in plan.constraints


def test_manual_plan_preserves_opportunity_exclusion_constraints() -> None:
    plan = infer_manual_request_plan(
        (
            "Find opportunities, but exclude startups under 10 employees, exclude "
            "on-site roles, exclude unpaid roles, and exclude roles requiring a "
            "full-time practicing clinician."
        ),
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "opportunity_scout"
    assert {
        "exclude startups under 10 employees",
        "exclude on-site roles",
        "exclude unpaid roles",
        "exclude roles requiring a full-time practicing clinician",
    } <= set(plan.constraints)


def test_manual_plan_routes_multiline_no_result_role_search_to_opportunity_scout() -> None:
    prompt = get_test_pack_spec("OS-5").natural_prompt

    plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_search"
    assert "chief medical officer" in plan.primary_target
    assert "Adjacent but not exact matches" not in plan.primary_target
    assert plan.task_objective == "opportunity_discovery"


def test_manual_plan_distinguishes_named_meeting_summary_from_opportunity_record() -> None:
    plan = infer_manual_request_plan(
        "opportunity scout find summaries of APA 2026 meeting in San Francisco",
        requested_agent="opportunity scout",
    )

    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_search"
    assert plan.target_type == "conference"
    assert plan.task_objective == "source_research"
    assert plan.expected_artifact_type == "source_summary"
    assert "APA" in plan.required_entities
    assert {"APA", "2026", "San Francisco"} <= set(plan.required_terms)


@pytest.mark.parametrize(
    "prompt",
    [
        "who is Abridge and summarize the company in 20 words.",
        "summarize Abridge in 20 words.",
        "what does Abridge do?",
        "tell me about Abridge.",
    ],
)
def test_manual_plan_hands_company_profile_ask_from_scout_to_research_owner(
    prompt: str,
) -> None:
    plan = infer_manual_request_plan(prompt, requested_agent="opportunity_scout")

    assert plan.requested_agent == "opportunity_scout"
    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"
    assert plan.primary_target == "Abridge"
    assert plan.target_type == "company"
    assert plan.task_objective == "source_research"
    if "summar" in prompt:
        assert plan.expected_artifact_type == "source_summary"
    else:
        assert plan.expected_artifact_type == "research_brief"
    assert plan.required_entities == ["Abridge"]


@pytest.mark.parametrize(
    ("requested_agent", "prompt", "expected_owner", "expected_intent"),
    [
        (
            "opportunity_scout",
            "Research Abridge and summarize what the company does.",
            "business_research_analyst",
            "company_research",
        ),
        (
            "business_research_analyst",
            "Find current remote psychiatry grant and conference opportunities.",
            "opportunity_scout",
            "opportunity_search",
        ),
        (
            "opportunity_scout",
            "Triage my unread Gmail messages from the last 3 days.",
            "gmail_triage",
            "gmail_triage",
        ),
        (
            "gmail_triage",
            "Draft a short LinkedIn outreach note to Jane using approved facts.",
            "outreach_composer",
            "outreach_draft",
        ),
        (
            "outreach_composer",
            "Review our Slack workflow status and recommend next actions.",
            "chief_of_staff",
            "slack_operations",
        ),
        (
            "business_research_analyst",
            "Draft a short outreach email to the approved contact.",
            "outreach_composer",
            "outreach_draft",
        ),
        (
            "outreach_composer",
            "Find five remote advisory opportunities in behavioral health AI.",
            "opportunity_scout",
            "opportunity_search",
        ),
        (
            "gmail_triage",
            "Use Airtable to read the selected company record.",
            "airtable_context_agent",
            "context_lookup",
        ),
    ],
)
def test_manual_plan_delegates_wrong_explicit_specialist_to_clear_task_owner(
    requested_agent: str,
    prompt: str,
    expected_owner: str,
    expected_intent: str,
) -> None:
    plan = infer_manual_request_plan(prompt, requested_agent=requested_agent)

    assert plan.requested_agent == requested_agent
    assert plan.target_agent == expected_owner
    assert plan.intent == expected_intent


@pytest.mark.parametrize(
    "requested_agent",
    [
        "business_research_analyst",
        "opportunity_scout",
        "gmail_triage",
        "outreach_composer",
        "chief_of_staff",
    ],
)
def test_incidental_time_pressure_and_operational_facts_never_change_named_owner(
    requested_agent: str,
) -> None:
    request = (
        "I’m heading into a meeting. Using only these two facts, give me exactly "
        "two short bullets: the shared admission layer now requires semantic intent "
        "plus a provider-bound action before any provider handler runs; exact "
        "response-count constraints should suppress decorative Slack titles. "
        "First bullet: what is standardized. Second bullet: what this test confirms. "
        "Do not search, use tools or providers, create or modify anything, draft "
        "messages, or include routing or workflow metadata."
    )

    plan = infer_manual_request_plan(request, requested_agent=requested_agent)

    assert plan.target_agent == requested_agent
    assert (
        resolve_manual_request_owner(
            requested_agent,
            plan,
            request_text=request,
        )
        == requested_agent
    )


def test_owner_reconciliation_requires_bounded_slack_operation_evidence() -> None:
    incidental = (
        "I’m heading into a meeting. Using only these facts, return two bullets "
        "about why decorative Slack titles should be suppressed."
    )
    real_operation = "Review our Slack workflow status and recommend next actions."

    incidental_plan = ManualRequestPlan(
        requested_agent="business_research_analyst",
        target_agent="chief_of_staff",
        intent="route_request",
        objective=incidental,
    )
    operation_plan = infer_manual_request_plan(
        real_operation,
        requested_agent="business_research_analyst",
    )

    assert (
        resolve_manual_request_owner(
            "business_research_analyst",
            incidental_plan,
            request_text=incidental,
        )
        == "business_research_analyst"
    )
    assert operation_plan.intent == "slack_operations"
    assert (
        resolve_manual_request_owner(
            "business_research_analyst",
            operation_plan,
            request_text=real_operation,
        )
        == "chief_of_staff"
    )


def test_llm_plan_is_not_reclassified_from_provider_word_in_supplied_fact() -> None:
    request = (
        "Using only these supplied facts, summarize them in two bullets: Airtable "
        "reads require bounded record identity; provider receipts verify completion."
    )
    fallback = infer_manual_request_plan(
        request,
        requested_agent="business_research_analyst",
    )
    candidate = fallback.model_copy(
        update={
            "source": "llm",
            "target_agent": "business_research_analyst",
            "intent": "route_request",
            "provider_system": "unspecified",
        }
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert fallback.target_agent == "business_research_analyst"
    assert merged.target_agent == "business_research_analyst"
    assert merged.provider_system == "unspecified"


@pytest.mark.parametrize(
    ("prompt", "requested_agent", "expected_owner"),
    [
        (
            "opportunity scout: triage my unread Gmail messages from today",
            "opportunity_scout",
            "gmail_triage",
        ),
        (
            "gmail triage: draft a LinkedIn outreach note using approved facts",
            "gmail_triage",
            "outreach_composer",
        ),
        (
            "outreach composer: find current remote psychiatry grants",
            "outreach_composer",
            "opportunity_scout",
        ),
        (
            "business research analyst: review our Slack workflow status",
            "business_research_analyst",
            "chief_of_staff",
        ),
    ],
)
def test_manual_plan_parses_wrong_named_specialist_but_routes_to_task_owner(
    prompt: str,
    requested_agent: str,
    expected_owner: str,
) -> None:
    plan = infer_manual_request_plan(prompt)

    assert plan.requested_agent == requested_agent
    assert plan.target_agent == expected_owner


def test_manual_plan_keeps_ambiguous_ask_with_explicit_specialist() -> None:
    plan = infer_manual_request_plan(
        "Summarize this.",
        requested_agent="opportunity_scout",
    )

    assert plan.requested_agent == "opportunity_scout"
    assert plan.target_agent == "opportunity_scout"


def test_abridge_name_does_not_trigger_operational_bridge_route() -> None:
    plan = infer_manual_request_plan(
        "Research Abridge and summarize what the company does.",
    )

    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"


def test_manual_plan_preserves_scout_for_actionable_named_company_opportunity() -> None:
    plan = infer_manual_request_plan(
        "find current Abridge partnership opportunities",
        requested_agent="opportunity_scout",
    )

    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_search"
    assert plan.task_objective == "opportunity_discovery"


def test_manual_plan_captures_plain_in_word_limit_for_company_summary() -> None:
    plan = infer_manual_request_plan(
        "who is Abridge and summarize the company in 20 words.",
        requested_agent="opportunity_scout",
    )

    assert plan.ask_shape.stop_condition == "stop_after_20_word_summary"
    assert plan.ask_shape.output_constraints.scope == "answer"
    assert plan.ask_shape.output_constraints.word_count_mode == "exact"
    assert plan.ask_shape.output_constraints.word_count == 20


def test_manual_plan_captures_word_limit_after_described_outreach_artifact() -> None:
    plan = infer_manual_request_plan(
        "Outreach Composer, using only these supplied facts, draft a concise "
        "internal-ready outreach email under 100 words.",
        requested_agent="outreach_composer",
    )

    assert plan.ask_shape.output_constraints.scope == "draft_body"
    assert plan.ask_shape.output_constraints.word_count_mode == "under"
    assert plan.ask_shape.output_constraints.word_count == 100


@pytest.mark.parametrize(
    ("prompt", "expected_words", "expected_scope"),
    [
        ("Please prepare a 70-word outreach draft for internal review.", 70, "draft_body"),
        ("Could you give me a 90‑word brief for the meeting?", 90, "answer"),
        ("Keep the response to a 45-word note.", 45, "draft_body"),
    ],
)
def test_manual_plan_captures_attributive_exact_word_count(
    prompt: str,
    expected_words: int,
    expected_scope: str,
) -> None:
    plan = infer_manual_request_plan(prompt, requested_agent="outreach_composer")

    constraints = plan.ask_shape.output_constraints
    assert constraints.scope == expected_scope
    assert constraints.word_count_mode == "exact"
    assert constraints.word_count == expected_words


@pytest.mark.parametrize(
    "prompt",
    [
        "Summarize the policy containing a 70-word note.",
        "The source says it is a 70-word brief.",
        "Analyze a 70-word source excerpt and return three bullets.",
        "Review the 2026-08-15 deadline and GPT-5.4 model notes.",
        "Use the 70-word draft above to prepare two bullets.",
    ],
)
def test_manual_plan_does_not_promote_described_numeric_text_to_word_constraint(
    prompt: str,
) -> None:
    plan = infer_manual_request_plan(prompt, requested_agent="outreach_composer")

    constraints = plan.ask_shape.output_constraints
    assert constraints.word_count_mode == "unspecified"
    assert constraints.word_count is None


def test_manual_plan_captures_word_range_after_described_outreach_artifact() -> None:
    plan = infer_manual_request_plan(
        "Outreach Composer, prepare a 100-130 word first-contact email using "
        "only these approved facts.",
        requested_agent="outreach_composer",
    )

    constraints = plan.ask_shape.output_constraints
    assert constraints.scope == "answer"
    assert constraints.word_count_mode == "unspecified"
    assert constraints.word_count is None
    assert constraints.minimum_words == 100
    assert constraints.maximum_words == 130
    assert constraints.has_deterministic_requirements() is True


def test_manual_plan_keeps_meeting_speaking_request_as_opportunity_record() -> None:
    plan = infer_manual_request_plan(
        "Find APA 2026 meeting speaking or abstract opportunities in San Francisco",
        requested_agent="opportunity scout",
    )

    assert plan.target_agent == "opportunity_scout"
    assert plan.target_type == "conference"
    assert plan.task_objective == "opportunity_discovery"
    assert plan.expected_artifact_type == "opportunity_record"
    assert {"APA", "2026", "San Francisco"} <= set(plan.required_terms)


def test_manual_plan_routes_generic_opportunity_to_outreach_loop_to_workflow() -> None:
    plan = infer_manual_request_plan(
        "run one opportunity-to-outreach loop for behavioral health AI. "
        "Top 1 only. Post approval to this channel. Draft only, do not send.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_to_outreach_loop"
    assert plan.workflow == []
    assert plan.requires_durable_state is False
    assert plan.target_type == "opportunity"
    assert plan.task_objective == "opportunity_discovery"
    assert plan.expected_artifact_type == "opportunity_record"
    assert plan.primary_target == "behavioral health AI"
    assert plan.desired_count == 1
    assert plan.requires_live_search is True
    assert "behavioral health" in plan.constraints


@pytest.mark.parametrize(
    "prompt",
    [
        (
            "Could you find two currently open opportunities that a small "
            "neuroinformatics consultancy could realistically pursue this fall? "
            "Compare fit, eligibility, deadline, and evidence quality. If the best "
            "one depends on a company claim you can't verify, have that claim checked "
            "before you finalize. Don't save anything or draft outreach."
        ),
        (
            "Find three open grants for a small clinical AI consultancy and compare "
            "eligibility, timing, evidence strength, and KNI fit. Keep it read-only."
        ),
        (
            "Which two current partnership opportunities look most realistic? Compare "
            "sponsor, deadline, feasibility, and source quality before recommending one."
        ),
        (
            "I'm looking for one live U.S. non-dilutive funding or pilot opening that "
            "a small behavioral-health AI consultancy could pursue before early November. "
            "Compare the strongest current options, choose one, and give me its deadline, "
            "why it fits Keystone, the biggest eligibility concern, and the official URL. "
            "Keep this read-only."
        ),
        (
            "Could you look for a single open accelerator cohort or pilot program for a "
            "small clinical AI consultancy, then recommend the strongest current fit?"
        ),
        (
            "Between current grants and non-dilutive funding calls, which one looks most "
            "realistic for Keystone before November?"
        ),
        (
            "Surface one live pilot opening for a behavioral-health measurement company "
            "and explain the deadline, fit, and main eligibility risk."
        ),
        (
            "I'm looking for one live U.K. pilot opening for a small clinical AI "
            "consultancy. Choose the strongest current option and explain its deadline."
        ),
    ],
)
def test_manual_plan_keeps_opportunity_comparison_dimensions_with_scout(
    prompt: str,
) -> None:
    plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_search"
    assert plan.task_objective == "opportunity_discovery"


def test_explicit_scout_natural_funding_request_keeps_scout_owner() -> None:
    prompt = (
        "Opportunity Scout, I'm looking for one live U.S. non-dilutive funding or "
        "pilot opening that a small behavioral-health AI consultancy could pursue "
        "before early November. Compare the strongest current options, choose one, "
        "and give me its deadline, why it fits Keystone, the biggest eligibility "
        "concern, and the official URL. Keep this read-only."
    )

    plan = infer_manual_request_plan(prompt, requested_agent="opportunity_scout")

    assert plan.requested_agent == "opportunity_scout"
    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_search"
    assert plan.task_objective == "opportunity_discovery"


def test_explicit_scout_curly_apostrophe_funding_request_keeps_scout_owner() -> None:
    prompt = (
        "Opportunity Scout, I’m looking for one live U.S. pilot opening for a small "
        "behavioral-health AI consultancy. Choose the best current fit and keep it "
        "read-only."
    )

    plan = infer_manual_request_plan(prompt, requested_agent="opportunity_scout")

    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_search"


def test_opportunity_match_does_not_cross_an_ordinary_sentence_boundary() -> None:
    prompt = (
        "I'm looking for background on Northstar Care's U.S. market position. "
        "A pilot opening is mentioned in its materials, but summarize the company only."
    )

    plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"


def test_company_list_comparison_still_routes_to_business_research() -> None:
    plan = infer_manual_request_plan(
        "Compare Acme Health, Beta Labs, and Gamma Care and explain which company "
        "has the strongest evidence base.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"
    assert plan.primary_target == "Acme Health vs Beta Labs vs Gamma Care"


@pytest.mark.parametrize(
    ("company_prompt", "expected_target"),
    [
        (
            (
                "NeuroFlow has been coming up as a behavioral-health AI company with "
                "payer partnership and outcomes-evidence signals."
            ),
            "NeuroFlow",
        ),
        (
            (
                "Example Health has been coming up as a care-delivery analytics vendor "
                "with payer partnership signals."
            ),
            "Example Health",
        ),
        (
            (
                "Alto Neuroscience has been coming up as a precision-psychiatry company "
                "with biomarker-stratified trial signals."
            ),
            "Alto Neuroscience",
        ),
    ],
)
def test_manual_plan_keeps_chief_front_door_for_multi_step_research_request(
    company_prompt: str,
    expected_target: str,
) -> None:
    plan = infer_manual_request_plan(
        (
            f"{company_prompt} Do research, assess whether this is a real KNI "
            "advisory/research opportunity, identify what source-backed evidence is "
            "still missing, and decide whether it should stop at an approval checkpoint "
            "before any outreach. If the evidence supports pursuing it, include a "
            "draft-only Slack-thread sample outreach for review. Do not send email, "
            "create Gmail drafts, post outside this thread, schedule, publish, or write "
            "external systems."
        ),
        requested_agent="chief_of_staff",
    )

    assert plan.requested_agent == "chief_of_staff"
    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "route_request"
    assert plan.target_type == "company"
    assert plan.expected_artifact_type == "none"
    assert expected_target in plan.primary_target


def test_manual_plan_blocks_opportunity_to_outreach_loop_when_draft_outreach_is_negated() -> None:
    plan = infer_manual_request_plan(
        "Find 2 lightweight opportunity directions for Keystone related to behavioral health AI "
        "validation or safety review. Do not draft outreach, send, publish, schedule, write files, "
        "create CRM records, or post elsewhere.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_search"
    assert plan.task_objective == "opportunity_discovery"
    assert plan.side_effect_policy == "draft_or_read_only"
    assert plan.expected_artifact_type == "opportunity_record"


def test_manual_plan_preserves_safe_workflow_for_find_and_send_request() -> None:
    plan = infer_manual_request_plan(
        "Find and send outreach to the best three companies.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_search"
    assert plan.task_objective == "opportunity_discovery"
    assert plan.desired_count == 3
    assert plan.side_effect_policy == "draft_or_read_only"
    assert any("Send request blocked" in warning for warning in plan.planner_warnings)


def test_manual_plan_counts_compare_how_three_products() -> None:
    plan = infer_manual_request_plan(
        (
            "business research analyst: Compare how three public AI companion "
            "or chatbot products describe teen safety."
        ),
        requested_agent="business_research_analyst",
    )

    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"
    assert plan.desired_count == 3
    assert "comparison-format" in plan.constraints


def test_llm_plan_is_not_vetoed_by_fallback_workflow_recognizer() -> None:
    fallback = infer_manual_request_plan(
        "run one opportunity-to-outreach loop for behavioral health AI. Top 1 only.",
        requested_agent="orchestrator",
    )
    bad_candidate = ManualRequestPlan(
        source="llm",
        requested_agent="orchestrator",
        target_agent="business_research_analyst",
        intent="company_research",
        primary_target="AI. Top",
        target_type="company",
    )

    merged = merge_manual_request_plan(fallback, bad_candidate)

    assert merged.intent == "company_research"
    assert merged.target_agent == "business_research_analyst"
    assert merged.primary_target == "AI. Top"
    assert merged.workflow == []


def test_manual_plan_routes_named_specialist_source_operation_to_owner() -> None:
    fallback = infer_manual_request_plan(
        (
            "Use Zotero to select the most recently added journal article with a "
            "stored abstract and summarize it."
        ),
        requested_agent="business_research_analyst",
    )
    candidate = fallback.model_copy(
        update={
            "source": "llm",
            "target_agent": "zotero_context_agent",
            "target_type": "zotero_article",
        }
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert fallback.target_agent == "zotero_context_agent"
    assert merged.requested_agent == "business_research_analyst"
    assert merged.target_agent == "zotero_context_agent"
    assert not any("Ignored planner override" in warning for warning in merged.planner_warnings)


def test_manual_plan_does_not_treat_single_record_fields_as_item_count() -> None:
    request = (
        "Use Zotero to select the most recently added journal article. Return only "
        "its exact title, authors, and publication title."
    )
    fallback = infer_manual_request_plan(
        request,
        requested_agent="business_research_analyst",
    )
    candidate = fallback.model_copy(deep=True)
    candidate.source = "llm"
    candidate.required_terms = ["Zotero"]
    candidate.ask_shape.strict_filter_mode = "exact"
    candidate.ask_shape.output_form = "brief"
    candidate.ask_shape.output_constraints = InterpretedOutputConstraints(
        interpretation="Return the three requested fields for one article.",
        scope="answer",
        item_count_mode="exact",
        minimum_items=3,
        maximum_items=3,
        style_requirements=["exact fields only"],
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert fallback.provider_selection_order == "latest"
    assert fallback.zotero_requested_fields == [
        "title",
        "authors",
        "publication_title",
    ]
    assert merged.provider_selection_order == "latest"
    assert merged.zotero_requested_fields == [
        "title",
        "authors",
        "publication_title",
    ]
    assert merged.desired_count == 1
    assert merged.ask_shape.output_constraints.minimum_items is None
    assert merged.ask_shape.output_constraints.maximum_items is None
    assert merged.ask_shape.output_constraints.item_count_mode == "unspecified"
    assert merged.ask_shape.output_constraints.style_requirements == ["exact fields only"]
    assert cli._strict_requested_display_text(
        {
            "article_titles": ["Selected article"],
            "blockers": ["Publication title and authors are unavailable."],
            "diagnostics": [
                {"key": "authors", "value": "A. Researcher; B. Scientist"},
                {"key": "publication_title", "value": "Clinical AI Journal"},
            ],
        },
        merged,
    ) == (
        "Title: Selected article\n"
        "Authors: A. Researcher; B. Scientist\n"
        "Publication title: Clinical AI Journal"
    )
    assert "Authors: Unavailable in Zotero metadata" in cli._strict_requested_display_text(
        {"article_titles": ["Selected article"], "diagnostics": []},
        merged,
    )


def test_manual_plan_preserves_explicit_operator_item_count() -> None:
    fallback = infer_manual_request_plan(
        "Return exactly 3 bullets about the selected article.",
        requested_agent="business_research_analyst",
    )
    candidate = fallback.model_copy(deep=True)
    candidate.source = "llm"

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.ask_shape.output_constraints.minimum_items == 3
    assert merged.ask_shape.output_constraints.maximum_items == 3
    assert merged.ask_shape.output_constraints.item_count_mode == "exact"


@pytest.mark.parametrize(
    ("request_text", "expected_sentences", "expected_items"),
    [
        (
            "Turn those two bullets into one concise internal Slack sentence I can paste.",
            1,
            None,
        ),
        (
            "Using the three points above, write one short sentence for the team.",
            1,
            None,
        ),
        (
            "Combine the previous four items into two concise bullets.",
            None,
            2,
        ),
    ],
)
def test_current_turn_output_transform_does_not_inherit_source_cardinality(
    request_text: str,
    expected_sentences: int | None,
    expected_items: int | None,
) -> None:
    plan = infer_manual_request_plan(
        request_text,
        requested_agent="business_research_analyst",
    )

    constraints = plan.ask_shape.output_constraints
    assert constraints.sentence_count == expected_sentences
    assert constraints.minimum_items == expected_items
    assert constraints.maximum_items == expected_items
    assert plan.ask_shape.output_form == ("brief" if expected_sentences is not None else "bullets")


def test_conflicting_current_turn_item_counts_remain_advisory() -> None:
    plan = infer_manual_request_plan(
        "Return two bullets, then return three bullets.",
        requested_agent="chief_of_staff",
    )

    constraints = plan.ask_shape.output_constraints
    assert constraints.item_count_mode == "unspecified"
    assert constraints.minimum_items is None
    assert constraints.maximum_items is None


def test_current_turn_contract_reconciles_inconsistent_planner_fields() -> None:
    request = (
        "Now turn those two bullets into one concise internal Slack sentence I can "
        "paste to the team. Keep it in this thread only; don't use email, search, "
        "or any provider."
    )
    fallback = infer_manual_request_plan(
        request,
        requested_agent="business_research_analyst",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="outreach_composer",
        target_agent="outreach_composer",
        intent="outreach_draft",
        objective="Create one thread-local internal Slack sentence.",
        task_objective="outreach_draft",
        expected_artifact_type="outreach_draft",
        desired_count=1,
        outreach_channel="internal_slack",
        provider_system="unspecified",
        provider_operations=[],
        requires_approved_context=True,
        side_effect_policy="draft_or_read_only",
        ask_shape=AskShapePolicy(
            output_form="bullets",
            prior_context_dependency="required",
            permission_state="draft_only",
            output_constraints=InterpretedOutputConstraints(
                interpretation="Turn two bullets into one sentence.",
                scope="entire_response",
                item_count_mode="exact",
                minimum_items=2,
                maximum_items=2,
                style_requirements=["concise", "internal Slack"],
            ),
        ),
    )

    merged = merge_manual_request_plan(fallback, candidate)

    constraints = merged.ask_shape.output_constraints
    assert merged.target_agent == "outreach_composer"
    assert merged.ask_shape.output_form == "brief"
    assert constraints.sentence_count_mode == "exact"
    assert constraints.sentence_count == 1
    assert constraints.item_count_mode == "unspecified"
    assert constraints.minimum_items is None
    assert constraints.maximum_items is None
    assert constraints.style_requirements == ["concise", "internal Slack"]
    assert is_internal_slack_composition_plan(merged) is True
    assert merged.requires_approved_context is False
    assert any("authoritative current turn" in warning for warning in merged.planner_warnings)


def test_current_turn_internal_audience_overrides_planner_provider_drift() -> None:
    request = (
        "Combine those two points into a single paste-ready sentence for our "
        "internal team channel. Use only the Oakline note already in this thread; "
        "no search, email, or provider actions."
    )
    fallback = infer_manual_request_plan(
        request,
        requested_agent="business_research_analyst",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="outreach_composer",
        target_agent="outreach_composer",
        intent="outreach_draft",
        objective="Compose one internal team sentence from selected context.",
        task_objective="outreach_draft",
        expected_artifact_type="outreach_draft",
        outreach_channel="team_channel",
        provider_system="slack",
        provider_operations=[],
        requires_approved_context=True,
        side_effect_policy="draft_or_read_only",
        ask_shape=AskShapePolicy(
            output_form="draft",
            prior_context_dependency="selected_context",
            permission_state="draft_only",
            audience_scope="external",
        ),
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert fallback.ask_shape.audience_scope == "internal"
    assert merged.ask_shape.audience_scope == "internal"
    assert merged.provider_system == "unspecified"
    assert merged.provider_operations == []
    assert merged.requires_approved_context is False
    assert is_internal_slack_composition_plan(merged) is True


def test_task_local_singular_choice_is_not_global_item_count() -> None:
    request = (
        'CoS, find the Gmail email with subject "RUAIH Certification". Read that '
        "email in the context of its complete thread. Tell me briefly what warrants "
        "a response, then draft a concise response here in this Slack thread so I "
        "can copy it. Use your judgment: either ask one thoughtful question or make "
        "one useful point, whichever fits the email better. Do not create a Gmail "
        "draft, send anything, change labels, archive, or otherwise modify the mailbox."
    )

    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    constraints = plan.ask_shape.output_constraints
    assert constraints.item_count_mode == "unspecified"
    assert constraints.minimum_items is None
    assert constraints.maximum_items is None


def test_provider_action_prohibitions_cannot_become_forbidden_output_phrases() -> None:
    request = (
        'CoS, find the email titled "RUAIH Certification", tell me briefly whether '
        "it merits a response, and write a short reply here so I can paste it. Leave "
        "Gmail exactly as it is: no draft, send, label changes, or archive."
    )
    fallback = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    candidate = fallback.model_copy(deep=True)
    candidate.source = "llm"
    candidate.ask_shape.output_form = "draft"
    candidate.ask_shape.output_constraints = InterpretedOutputConstraints(
        interpretation=(
            "Return a brief reply suggestion based on the selected thread without modifying Gmail."
        ),
        scope="draft_body",
        forbidden_phrases=["draft", "send", "label", "archive"],
        style_requirements=["short", "paste-ready"],
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.ask_shape.output_constraints.forbidden_phrases == []
    assert merged.ask_shape.output_constraints.has_deterministic_requirements() is False
    assert merged.ask_shape.output_constraints.style_requirements == [
        "short",
        "paste-ready",
    ]


def test_explicit_quoted_forbidden_output_phrase_remains_enforceable() -> None:
    request = (
        'Give me exactly two bullets. Do not use the phrase "workflow metadata" in the answer.'
    )
    fallback = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    candidate = fallback.model_copy(deep=True)
    candidate.source = "llm"
    candidate.ask_shape.output_constraints = candidate.ask_shape.output_constraints.model_copy(
        update={
            "forbidden_phrases": ["workflow metadata", "routing"],
            "style_requirements": ["concise"],
        }
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.ask_shape.output_constraints.forbidden_phrases == ["workflow metadata"]
    assert merged.ask_shape.output_constraints.minimum_items == 2
    assert merged.ask_shape.output_constraints.maximum_items == 2


@pytest.mark.parametrize(
    "request_text",
    [
        "Return exactly one useful point.",
        "Give me one useful point.",
        "One useful point only.",
    ],
)
def test_explicit_singular_response_shape_remains_enforceable(
    request_text: str,
) -> None:
    plan = infer_manual_request_plan(
        request_text,
        requested_agent="chief_of_staff",
    )

    constraints = plan.ask_shape.output_constraints
    assert constraints.item_count_mode == "exact"
    assert constraints.minimum_items == 1
    assert constraints.maximum_items == 1


@pytest.mark.parametrize(
    "request_text",
    [
        (
            "Using only this note, give me three short bullets: one shared request "
            "contract; separate direct and graph execution; verified provider receipts."
        ),
        (
            "Using only these facts, summarize the plan in 3 concise bullets: one "
            "request contract; separate executors; verified receipts."
        ),
        (
            "Turn the following supplied text into three brief talking points: one "
            "request contract; separate execution paths; verified receipts."
        ),
    ],
)
def test_manual_plan_preserves_supplied_context_bullet_count(
    request_text: str,
) -> None:
    plan = infer_manual_request_plan(request_text, requested_agent="chief_of_staff")

    assert plan.target_agent == "chief_of_staff"
    assert plan.desired_count == 3
    assert plan.ask_shape.output_form == "bullets"
    assert plan.ask_shape.ask_breadth == "narrow"
    assert plan.ask_shape.output_constraints.item_count_mode == "exact"
    assert plan.ask_shape.output_constraints.minimum_items == 3
    assert plan.ask_shape.output_constraints.maximum_items == 3


def test_manual_plan_preserves_official_source_and_url_count_contract() -> None:
    plan = infer_manual_request_plan(
        (
            "Business Research Analyst, research Callyope and give me 4 concise "
            "bullets: what it does, which modalities it uses, which claims are "
            "directly verified, and what remains uncertain. Use exactly 2 official "
            "Callyope sources with URLs. Do not create or modify anything."
        ),
        requested_agent="business_research_analyst",
    )

    constraints = plan.ask_shape.output_constraints
    assert plan.ask_shape.source_type_preference == ["official"]
    assert plan.ask_shape.evidence_depth == "unspecified"
    assert plan.ask_shape.output_form == "bullets"
    assert constraints.item_count_mode == "exact"
    assert constraints.minimum_items == 4
    assert constraints.maximum_items == 4
    assert constraints.source_url_count_mode == "exact"
    assert constraints.source_url_count == 2
    assert constraints.include_source_urls is True


def test_manual_plan_preserves_bounded_comparison_output_contract() -> None:
    request = (
        "Compare Callyope and Kintsugi. Give me 5 concise but substantive bullets. "
        "Include one official source URL for each company."
    )
    plan = infer_manual_request_plan(
        request,
        requested_agent="business_research_analyst",
    )

    constraints = plan.ask_shape.output_constraints
    assert constraints.item_count_mode == "exact"
    assert constraints.minimum_items == 5
    assert constraints.maximum_items == 5
    assert constraints.source_url_count_mode == "exact"
    assert constraints.source_url_count == 2


def test_cached_planner_cannot_deepen_or_contextualize_fresh_bounded_comparison() -> None:
    request = (
        "Compare Callyope and Kintsugi. Give me 5 concise but substantive bullets. "
        "Include one official source URL for each company."
    )
    base = infer_manual_request_plan(
        request,
        requested_agent="business_research_analyst",
    )
    cached_candidate = base.model_copy(
        update={
            "source": "llm",
            "ask_shape": base.ask_shape.model_copy(
                update={
                    "evidence_depth": "deep",
                    "prior_context_dependency": "selected_context",
                }
            ),
        }
    )

    merged = merge_manual_request_plan(
        base,
        cached_candidate,
    )

    assert merged.ask_shape.evidence_depth == "unspecified"
    assert merged.ask_shape.prior_context_dependency == "unspecified"
    assert merged.ask_shape.output_constraints.minimum_items == 5
    assert merged.ask_shape.output_constraints.source_url_count == 2


@pytest.mark.parametrize(
    "source_request",
    [
        "Use exactly 2 recent official Callyope sources with URLs.",
        "Use exactly 2 Callyope official sources with URLs.",
        "Use exactly 2 first-party sources with URLs.",
        "Use exactly 2 official sources from Callyope with URLs.",
    ],
)
def test_manual_plan_preserves_natural_official_source_phrasings(
    source_request: str,
) -> None:
    plan = infer_manual_request_plan(
        f"Research Callyope. {source_request}",
        requested_agent="business_research_analyst",
    )

    constraints = plan.ask_shape.output_constraints
    assert plan.ask_shape.source_type_preference == ["official"]
    assert constraints.source_url_count_mode == "exact"
    assert constraints.source_url_count == 2
    assert constraints.include_source_urls is True


def test_manual_plan_deep_research_is_not_downgraded_by_concise_output() -> None:
    plan = infer_manual_request_plan(
        "Research Callyope deeply and return a concise four-bullet brief.",
        requested_agent="business_research_analyst",
    )

    assert plan.ask_shape.evidence_depth == "deep"


def test_manual_plan_does_not_turn_reason_count_into_source_url_count() -> None:
    plan = infer_manual_request_plan(
        "Give at least two reasons to use official sources with URLs.",
        requested_agent="chief_of_staff",
    )

    constraints = plan.ask_shape.output_constraints
    assert constraints.source_url_count_mode == "unspecified"
    assert constraints.source_url_count is None


def test_manual_plan_honors_negated_official_source_preference() -> None:
    plan = infer_manual_request_plan(
        "Research Callyope, but do not use official Callyope sources.",
        requested_agent="business_research_analyst",
    )

    assert "official" not in plan.ask_shape.source_type_preference


def test_supplied_context_fact_named_provider_write_is_not_outreach_command() -> None:
    plan = infer_manual_request_plan(
        (
            "Using only these facts, give me exactly three concise bullets: all entry "
            "surfaces should create the same request envelope; direct and stateful "
            "multi-agent execution may remain separate backends; a provider write is "
            "complete only after its receipt passes verification. Do not search, call "
            "providers, or change anything. Prior result for context: A longer note. "
            "Authoritative follow-up: Make that just the three bullets with no note "
            "after them."
        ),
        requested_agent="chief_of_staff",
    )

    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "route_request"


@pytest.mark.parametrize(
    "requested_agent",
    [
        "chief_of_staff",
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
        "gmail_triage",
    ],
)
def test_direct_agents_keep_supplied_fact_formatting_as_route_request(
    requested_agent: str,
) -> None:
    prompt = (
        "Using only these two facts, give me exactly two short bullets: the shared "
        "admission contract requires semantic intent plus a provider-bound action; "
        "exact response counts suppress decorative Slack titles. Do not search, use "
        "tools or providers, create or modify anything, draft messages, or include "
        "routing metadata."
    )

    plan = infer_manual_request_plan(prompt, requested_agent=requested_agent)
    routed = route_request(prompt, manual_plan=plan)

    assert plan.target_agent == requested_agent
    assert plan.intent == "route_request"
    assert plan.task_objective == "route_or_continue"
    assert plan.expected_artifact_type == "none"
    assert plan.target_type == "unknown"
    assert plan.workflow == []
    assert plan.requires_live_search is False
    assert plan.requires_approved_context is False
    assert plan.gmail_query == ""
    assert plan.draft_policy == ""
    assert plan.recipient == ""
    assert plan.outreach_channel == ""
    assert plan.ask_shape.output_constraints.item_count_mode == "exact"
    assert plan.ask_shape.output_constraints.maximum_items == 2
    assert routed.route == requested_agent
    assert routed.refused is False


def test_slack_correction_negated_capabilities_do_not_select_outreach() -> None:
    plan = infer_manual_request_plan(
        (
            "CoS, one more correction: reply with only the exact three bullets "
            "supported by my original request. No heading or closing note. Do not "
            "search, call tools or providers, draft anything, or change anything.\n"
            "Prior result for context: the prior run was blocked before a model call.\n"
            "Authoritative follow-up: please finish this correction now: return only "
            "the three bullets from my original request, with no heading, introduction, "
            "or closing note. Do not search, call tools or providers, draft anything, "
            "or change anything."
        ),
        requested_agent="chief_of_staff",
    )

    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "route_request"
    assert plan.requires_approved_context is False
    assert plan.desired_count == 3
    assert plan.ask_shape.output_form == "bullets"
    assert "do not search, call tools or providers, draft anything, or change anything" in (
        plan.constraints
    )


@pytest.mark.parametrize(
    "request_text",
    [
        (
            "Return only the same three bullets from my original request. "
            "Do not search, draft anything, or change anything."
        ),
        (
            "Correct the previous response; keep exactly its three requested bullet "
            "points and no extra note. Never search or write anything."
        ),
        ("Reformat the answer above as three bullets only. Don't call tools or create a draft."),
    ],
)
def test_prior_response_transformations_route_to_chief_without_blocking(
    request_text: str,
) -> None:
    plan = infer_manual_request_plan(request_text, requested_agent="orchestrator")

    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "route_request"
    assert plan.requires_approved_context is False


def test_llm_cannot_turn_negated_drafting_boundary_into_outreach_route() -> None:
    fallback = infer_manual_request_plan(
        (
            "Return only the same three bullets from my original request. "
            "Do not search, call tools, draft anything, or change anything."
        ),
        requested_agent="orchestrator",
    )
    candidate = fallback.model_copy(
        update={
            "source": "llm",
            "target_agent": "outreach_composer",
            "intent": "outreach_draft",
            "requires_approved_context": True,
        }
    )

    merged = merge_manual_request_plan(
        fallback,
        candidate,
        allow_contextual_delegation=True,
    )

    assert merged.target_agent == "chief_of_staff"
    assert merged.intent == "route_request"
    assert any("Removed outreach drafting" in warning for warning in merged.planner_warnings)


def test_negative_capabilities_never_become_required_owner_or_context() -> None:
    fallback = infer_manual_request_plan(
        (
            "CoS, return the same three bullets from the supplied facts. "
            "Do not search, call tools or providers, draft anything, or change anything."
        ),
        requested_agent="chief_of_staff",
    )
    candidate = fallback.model_copy(
        update={
            "source": "llm",
            "target_agent": "outreach_composer",
            "intent": "outreach_draft",
            "workflow": ["outreach_composer"],
            "requires_approved_context": True,
            "requires_live_search": True,
        }
    )

    merged = merge_manual_request_plan(
        fallback,
        candidate,
        allow_contextual_delegation=True,
    )

    assert merged.target_agent == "chief_of_staff"
    assert merged.intent == "route_request"
    assert merged.workflow == []
    assert merged.requires_approved_context is False
    assert merged.requires_live_search is False
    assert merged.constraints


@pytest.mark.parametrize(
    "candidate_update",
    [
        {
            "target_agent": "clarification",
            "intent": "clarification",
            "task_objective": "clarification",
        },
        {
            "target_agent": "orchestrator",
            "intent": "route_request",
            "workflow": ["business_research_analyst", "outreach_composer"],
        },
        {
            "target_agent": "chief_of_staff",
            "intent": "route_request",
            "requires_approved_context": True,
        },
    ],
)
def test_negative_constraints_cannot_become_blockers_or_prerequisites(
    candidate_update: dict[str, object],
) -> None:
    request = (
        "Without searching or using provider tools, use only these two facts: "
        "negative constraints narrow execution, and the offline suite passes. "
        "Give me exactly two short bullets. Do not draft outreach, create or "
        "modify records, or include routing metadata."
    )
    fallback = infer_manual_request_plan(request)
    candidate = fallback.model_copy(
        update={"source": "llm", **candidate_update},
    )

    merged = merge_manual_request_plan(
        fallback,
        candidate,
        allow_contextual_delegation=True,
    )

    assert merged.target_agent == "chief_of_staff"
    assert merged.intent == "route_request"
    assert merged.workflow == []
    assert merged.requires_approved_context is False
    assert merged.requires_live_search is False
    assert merged.planner_warnings


@pytest.mark.parametrize(
    ("request_text", "requested_agent", "expected_recovery_owner"),
    [
        (
            "Without provider tools, use only these facts and return two bullets. "
            "Do not draft outreach or modify records.",
            None,
            "chief_of_staff",
        ),
        (
            "Research NeuroFlow from the attached approved notes only. Do not search "
            "the web, draft outreach, or modify provider records.",
            None,
            "chief_of_staff",
        ),
        (
            "Identify the strongest collaboration opportunity in these supplied notes. "
            "Do not search the web, draft outreach, or create CRM records.",
            None,
            "chief_of_staff",
        ),
        (
            "Read the latest Gmail thread from the configured sender and summarize it. "
            "Do not draft a reply, change labels, or send anything.",
            None,
            "gmail_triage",
        ),
        (
            "CoS, using the supplied company packet, review what is known and unknown, "
            "identify the highest-value opportunity and validation gap, then prepare "
            "a concise internal Slack brief for review. Do not search the web, create "
            "provider records, send email, or post.",
            "chief_of_staff",
            "chief_of_staff",
        ),
    ],
)
def test_unexplained_clarification_recovers_without_phrase_restored_route(
    request_text: str,
    requested_agent: str | None,
    expected_recovery_owner: str,
) -> None:
    fallback = infer_manual_request_plan(
        request_text,
        requested_agent=requested_agent,
    )
    bad_candidate = fallback.model_copy(
        update={
            "source": "llm",
            "target_agent": "clarification",
            "intent": "clarification",
            "task_objective": "clarification",
            "workflow": [],
        }
    )

    merged = merge_manual_request_plan(
        fallback,
        bad_candidate,
        allow_contextual_delegation=True,
    )

    assert merged.target_agent == expected_recovery_owner
    assert merged.workflow == []
    assert merged.intent != "clarification"
    assert merged.requires_live_search is False
    assert any("No keyword route was restored" in item for item in merged.planner_warnings)


def test_conditional_agent_notation_is_not_execution_evidence() -> None:
    plan = infer_manual_request_plan(
        (
            "CoS, return a concise internal handoff from the supplied facts. "
            "If recommending another agent, use Chief of Staff -> Business Research "
            "Agent or Chief of Staff -> Airtable Context Agent notation as appropriate. "
            "Do not access Gmail, Airtable, Drive, Zotero, Slack history, web search, "
            "browser automation, or external tools."
        ),
        requested_agent="chief_of_staff",
    )

    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "route_request"
    assert plan.requires_approved_context is False
    assert plan.requires_live_search is False


@pytest.mark.parametrize(
    ("request_text", "requested_agent", "expected_owner"),
    [
        (
            "Research Acme Health. Do not search providers or draft anything.",
            "business_research_analyst",
            "business_research_analyst",
        ),
        (
            "Find three validation opportunities. Do not draft anything or change records.",
            "opportunity_scout",
            "opportunity_scout",
        ),
        (
            "Read the selected Airtable record. Do not draft anything or change it.",
            "airtable_context_agent",
            "airtable_context_agent",
        ),
        (
            "Do not search, but draft one internal Slack message from these approved facts.",
            "chief_of_staff",
            "outreach_composer",
        ),
    ],
)
def test_negated_capability_scope_is_not_positive_owner_evidence(
    request_text: str,
    requested_agent: str,
    expected_owner: str,
) -> None:
    plan = infer_manual_request_plan(request_text, requested_agent=requested_agent)

    assert plan.target_agent == expected_owner


@pytest.mark.parametrize(
    ("request_text", "expected_owner"),
    [
        (
            "No search. Summarize these supplied facts in three bullets.",
            "chief_of_staff",
        ),
        (
            "Without searching, draft one internal Slack message from these approved facts.",
            "outreach_composer",
        ),
        (
            "No provider tools, just return the answer from the supplied context.",
            "chief_of_staff",
        ),
    ],
)
def test_terse_negative_boundaries_do_not_block_positive_instruction(
    request_text: str,
    expected_owner: str,
) -> None:
    plan = infer_manual_request_plan(request_text, requested_agent="chief_of_staff")

    assert plan.target_agent == expected_owner
    assert plan.intent != "clarification"
    assert plan.requires_live_search is False
    assert plan.constraints


def test_unnamed_negative_prefix_preserves_supplied_facts_and_chief_route() -> None:
    request = (
        "I’m short on time. Without searching or using provider tools, use only "
        "these two facts: negative constraints now narrow execution instead of "
        "blocking it, and the full offline suite passes. Give me exactly two short "
        "bullets: what changed and what we still need to validate. Do not draft "
        "outreach, create or modify records, or include routing metadata."
    )

    plan = infer_manual_request_plan(request)

    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "route_request"
    assert plan.desired_count == 2
    assert plan.requires_live_search is False
    assert "without searching or using provider tools" in plan.constraints
    assert all("negative constraints now narrow" not in item for item in plan.constraints)
    assert "negative constraints now narrow execution" in positive_capability_text(request)
    assert "the full offline suite passes" in positive_capability_text(request)


def test_scoped_airtable_write_boundary_does_not_cancel_calendar_create() -> None:
    request = (
        "Do not change the existing Airtable record. Add Board prep to my Google Calendar tomorrow."
    )
    fallback = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    candidate = fallback.model_copy(
        update={
            "source": "llm",
            "target_agent": "chief_of_staff",
            "intent": "business_system_write",
            "provider_system": "google_calendar",
            "provider_operations": ["create", "verify"],
            "task_objective": "business_system_write",
            "expected_artifact_type": "business_system_write_plan",
            "ask_shape": AskShapePolicy(permission_state="approval_required"),
            "side_effect_policy": "internal_write_approval_required",
        }
    )

    merged = merge_manual_request_plan(
        fallback,
        candidate,
        allow_contextual_delegation=True,
    )

    assert merged.target_agent == "chief_of_staff"
    assert merged.intent == "business_system_write"
    assert merged.provider_system == "google_calendar"
    assert merged.provider_operations == ["create", "verify"]
    assert merged.ask_shape.permission_state == "approval_required"


def test_reported_no_search_text_does_not_cancel_requested_web_verification() -> None:
    request = (
        "The supplied note says 'do not search the web.' "
        "Check the web now to verify whether Acme Health still offers the program."
    )
    fallback = infer_manual_request_plan(request)
    candidate = fallback.model_copy(
        update={
            "source": "llm",
            "target_agent": "business_research_analyst",
            "intent": "company_research",
            "task_objective": "entity_research",
            "expected_artifact_type": "research_brief",
            "requires_live_search": True,
        }
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.target_agent == "business_research_analyst"
    assert merged.requires_live_search is True


def test_explicit_external_tool_boundary_still_disables_live_research() -> None:
    plan = infer_manual_request_plan(
        "Use only the supplied company facts. Do not use external tools.",
        requested_agent="business_research_analyst",
    )

    assert plan.requires_live_search is False


def test_inline_email_reply_with_no_gmail_boundary_stays_provider_free() -> None:
    plan = infer_manual_request_plan(
        (
            'Review this email excerpt and write a short reply here only: "Thanks for '
            'the update. Could you share the revised timeline?" Do not use Gmail, '
            "search, create a draft, or send anything."
        ),
        requested_agent="chief_of_staff",
    )

    assert plan.target_agent == "chief_of_staff"
    assert plan.workflow == []
    assert plan.provider_system == "unspecified"
    assert plan.provider_operations == []
    assert plan.requires_durable_state is False


@pytest.mark.parametrize(
    "provider_boundary",
    [
        "Don't create a Gmail draft or send it.",
        "No provider draft or send.",
        "Do not save this as a draft in Gmail.",
        "Don't create a draft or send anything.",
    ],
)
def test_provider_draft_boundary_does_not_cancel_requested_slack_composition(
    provider_boundary: str,
) -> None:
    request = (
        "Using only these approved facts, draft a short email here in Slack: "
        "Northstar Care sells referral-navigation software and its outcomes are "
        f"still being validated. {provider_boundary}"
    )
    fallback = infer_manual_request_plan(
        request,
        requested_agent="outreach_composer",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="outreach_composer",
        target_agent="outreach_composer",
        intent="outreach_draft",
        objective="Draft the requested email copy in the Slack response.",
        task_objective="outreach_draft",
        expected_artifact_type="outreach_draft",
        primary_target="Northstar Care",
        outreach_channel="slack_only",
        provider_system="unspecified",
        provider_operations=[],
        requires_approved_context=False,
        side_effect_policy="draft_or_read_only",
        ask_shape=AskShapePolicy(
            permission_state="draft_only",
            audience_scope="internal",
        ),
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.target_agent == "outreach_composer"
    assert merged.intent == "outreach_draft"
    assert merged.provider_system == "unspecified"
    assert merged.provider_operations == []


def test_natural_approved_synthetic_setup_admits_bounded_outreach_draft() -> None:
    request = (
        "Here’s an approved synthetic setup for a practice: Pine Harbor Behavioral "
        "Health says it runs measurement-based care across three community clinics and "
        "wants cleaner reporting; no person or email has been approved. Write a friendly "
        "organization-level introduction in 90-110 words for internal review, then add "
        "one separate Contact gap line. Stay within these facts. Don’t research, send, "
        "post, save, open an approval, or update tracking."
    )

    plan = infer_manual_request_plan(
        request,
        requested_agent="outreach_composer",
    )
    constraints = plan.ask_shape.output_constraints

    assert plan.target_agent == "outreach_composer"
    assert plan.intent == "outreach_draft"
    assert plan.primary_target == "Pine Harbor Behavioral Health"
    assert "approved_synthetic" in plan.ask_shape.source_type_preference
    assert plan.requires_approved_context is False
    assert plan.requires_live_search is False
    assert plan.ask_shape.audience_scope == "internal"
    assert constraints.minimum_words == 90
    assert constraints.maximum_words == 110
    assert constraints.scope == "draft_body"


def test_natural_give_me_introduction_is_a_section_scoped_outreach_draft() -> None:
    request = (
        "Here is an approved synthetic scenario: Cedar Grove Care Collaborative says "
        "it helps community clinics improve behavioral-health measurement and has not "
        "approved a contact. Give me an 85-105 word internal organization introduction, "
        "then put the contact gap on its own line. Use only those facts. Don't research, "
        "send, save, open an approval, or update a record."
    )

    plan = infer_manual_request_plan(request, requested_agent="outreach_composer")

    assert plan.target_agent == "outreach_composer"
    assert plan.intent == "outreach_draft"
    assert plan.expected_artifact_type == "outreach_draft"
    assert plan.ask_shape.output_form == "draft"
    assert plan.ask_shape.permission_state == "draft_only"
    assert plan.ask_shape.audience_scope == "internal"
    assert plan.ask_shape.output_constraints.minimum_words == 85
    assert plan.ask_shape.output_constraints.maximum_words == 105
    assert plan.ask_shape.output_constraints.scope == "draft_body"
    assert plan.ask_shape.output_constraints.required_sections == ["Contact gap"]
    assert plan.ask_shape.output_constraints.require_section_headings is True


def test_word_range_summary_of_a_note_remains_answer_scoped() -> None:
    plan = infer_manual_request_plan(
        "Write an 85-105 word summary of this supplied note for internal review.",
        requested_agent="chief_of_staff",
    )

    assert plan.ask_shape.output_constraints.minimum_words == 85
    assert plan.ask_shape.output_constraints.maximum_words == 105
    assert plan.ask_shape.output_constraints.scope == "answer"


def test_approved_fictional_setup_and_our_internal_review_keep_outreach_owner() -> None:
    request = (
        "Outreach Composer, here's an approved fictional setup: Riverbend Outcomes "
        "Network supports community practices with behavioral-health measurement and "
        "wants clearer reporting; no person or address is approved. Could you give me "
        "a 90-100 word organization introduction for our internal review, followed by "
        "a separate Contact gap line? Stay inside those facts. Please don't research, "
        "send, post, save, open an approval, or change tracking."
    )

    plan = infer_manual_request_plan(request, requested_agent="outreach_composer")

    assert plan.target_agent == "outreach_composer"
    assert plan.intent == "outreach_draft"
    assert plan.primary_target == "Riverbend Outcomes Network"
    assert plan.expected_artifact_type == "outreach_draft"
    assert plan.ask_shape.audience_scope == "internal"
    assert plan.ask_shape.permission_state == "draft_only"
    assert plan.ask_shape.output_constraints.scope == "draft_body"
    assert plan.ask_shape.output_constraints.required_sections == ["Contact gap"]
    assert plan.requires_live_search is False
    assert "comparison-format" not in plan.constraints


@pytest.mark.parametrize(
    "composition_boundary",
    [
        "Do not draft an email; summarize the facts instead.",
        "Never compose outreach from this note.",
        "No draft, just list the supported claims.",
    ],
)
def test_direct_composition_prohibition_still_removes_outreach_drafting(
    composition_boundary: str,
) -> None:
    fallback = infer_manual_request_plan(
        composition_boundary,
        requested_agent="chief_of_staff",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="outreach_composer",
        intent="outreach_draft",
        task_objective="outreach_draft",
        expected_artifact_type="outreach_draft",
        primary_target="Northstar Care",
        provider_system="unspecified",
        provider_operations=[],
        side_effect_policy="draft_or_read_only",
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.target_agent == "chief_of_staff"
    assert merged.intent == "route_request"
    assert merged.provider_operations == []


def test_gmail_mutation_boundary_does_not_cancel_permitted_read() -> None:
    plan = infer_manual_request_plan(
        (
            "Read the latest Gmail thread from Pat and summarize it here. "
            "Do not create a Gmail draft or change labels."
        ),
        requested_agent="chief_of_staff",
    )

    assert plan.target_agent == "gmail_triage"


@pytest.mark.parametrize(
    "request_text",
    [
        (
            "Summarize this policy sentence: exactly 50 words are required in "
            "formal submissions. Then give me a normal concise explanation."
        ),
        (
            "The source says that at most 3 sentences may appear in the abstract. "
            "Explain the rule normally."
        ),
    ],
)
def test_reported_counts_are_not_promoted_to_response_constraints(
    request_text: str,
) -> None:
    plan = infer_manual_request_plan(request_text)

    assert plan.ask_shape.output_constraints.has_deterministic_requirements() is False
    assert not plan.ask_shape.stop_condition.startswith("stop_after_")


@pytest.mark.parametrize(
    ("request_text", "field_name", "expected"),
    [
        ("Return exactly 50 words.", "word_count", 50),
        ("Answer in at most 3 sentences.", "sentence_count", 3),
        ("In exactly 40 words, summarize the supplied note.", "word_count", 40),
    ],
)
def test_explicit_response_counts_remain_deterministic(
    request_text: str,
    field_name: str,
    expected: int,
) -> None:
    constraints = infer_manual_request_plan(request_text).ask_shape.output_constraints

    assert getattr(constraints, field_name) == expected
    assert constraints.has_deterministic_requirements() is True


def test_llm_ask_depth_overrides_incidental_depth_words_in_source_content() -> None:
    request = (
        "Explain this supplied sentence: the vendor calls it a comprehensive deep "
        "research platform. Verify only the two claims I selected."
    )
    fallback = infer_manual_request_plan(
        request,
        requested_agent="business_research_analyst",
    )
    candidate = fallback.model_copy(
        update={
            "source": "llm",
            "ask_shape": AskShapePolicy(
                ask_breadth="narrow",
                evidence_depth="standard",
                cost_mode="balanced",
                prior_context_dependency="selected_context",
            ),
        }
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert fallback.ask_shape.evidence_depth == "deep"
    assert merged.ask_shape.ask_breadth == "narrow"
    assert merged.ask_shape.evidence_depth == "standard"
    assert merged.ask_shape.cost_mode == "balanced"


@pytest.mark.parametrize(
    ("request_text", "expected_owner"),
    [
        ("Use Airtable to read one expense record.", "airtable_context_agent"),
        ("Use Google Drive to list one matching document.", "google_workspace_context_agent"),
        ("Use Zotero to select one stored abstract.", "zotero_context_agent"),
        ("Use the RSS feed to list one announcement.", "rss_context_agent"),
        ("Use preprints context to read one abstract.", "preprints_context_agent"),
    ],
)
def test_direct_specialist_source_operations_keep_context_owner(
    request_text: str,
    expected_owner: str,
) -> None:
    plan = infer_manual_request_plan(
        request_text,
        requested_agent="business_research_analyst",
    )

    assert plan.requested_agent == "business_research_analyst"
    assert plan.target_agent == expected_owner


def test_chief_finance_summary_without_named_provider_stays_with_chief() -> None:
    plan = infer_manual_request_plan(
        (
            "Chief of Staff, total all income and expenses for Q1 and Q2 2026 "
            "and give me a clear summary."
        ),
        requested_agent="chief_of_staff",
    )

    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "route_request"
    assert plan.requires_live_search is False


def test_chief_named_airtable_finance_read_delegates_to_context_owner() -> None:
    plan = infer_manual_request_plan(
        ("Chief of Staff, read the Airtable financial tracker and summarize the current quarter."),
        requested_agent="chief_of_staff",
    )

    assert plan.target_agent == "airtable_context_agent"
    assert plan.intent == "context_lookup"
    assert plan.requires_live_search is False


def test_llm_intent_owner_contract_repairs_internal_mismatch() -> None:
    fallback = infer_manual_request_plan(
        "Research Acme Health and summarize its clinical AI product.",
        requested_agent="business_research_analyst",
    )
    candidate = fallback.model_copy(
        update={"source": "llm", "target_agent": "zotero_context_agent"}
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.target_agent == "business_research_analyst"
    assert any(
        "structured intent/provider contract" in warning for warning in merged.planner_warnings
    )


def test_llm_local_document_target_selects_chief_without_keyword_match() -> None:
    request_text = "Who handled our coverage?"
    fallback = infer_manual_request_plan(
        request_text,
        requested_agent="chief_of_staff",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="business_research_analyst",
        intent="context_lookup",
        task_objective="context_lookup",
        expected_artifact_type="context_summary",
        provider_system="unspecified",
        provider_operations=["read", "search"],
        primary_target="KNI company records",
        target_type="local_document_collection",
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.target_agent == "chief_of_staff"
    assert merged.intent == "context_lookup"
    assert merged.target_type == "local_document_collection"


@pytest.mark.parametrize(
    ("requested_agent", "request_text"),
    [
        (
            "rss_context_agent",
            "Check the saved RSS lifecycle checkpoint without advancing it.",
        ),
        (
            "preprints_context_agent",
            "Check the saved preprint lifecycle checkpoint without advancing it.",
        ),
    ],
)
def test_llm_local_document_mislabel_does_not_displace_explicit_signal_specialist(
    requested_agent: str,
    request_text: str,
) -> None:
    fallback = infer_manual_request_plan(
        request_text,
        requested_agent=requested_agent,
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent=requested_agent,
        target_agent="chief_of_staff",
        intent="context_lookup",
        task_objective="context_lookup",
        expected_artifact_type="context_summary",
        provider_system="unspecified",
        provider_operations=["read", "verify"],
        primary_target="saved signal lifecycle",
        target_type="local_document_collection",
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.target_agent == requested_agent
    assert merged.target_type == "article_collection"
    assert any(
        "explicitly addressed context specialist" in warning
        for warning in merged.planner_warnings
    )


def test_live_manual_plan_keeps_exact_operator_count_and_no_draft_safety() -> None:
    fallback = infer_manual_request_plan(
        "Summarize my top three unread emails from today and do not draft replies.",
        requested_agent="orchestrator",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="orchestrator",
        target_agent="gmail_triage",
        intent="gmail_triage",
        provider_system="gmail",
        provider_operations=["read", "create", "update"],
        provider_action_steps=[
            {"operation": "read", "resource_type": "gmail_message"},
            {"operation": "create", "resource_type": "gmail_draft"},
            {"operation": "update", "resource_type": "gmail_draft"},
        ],
        desired_count=8,
        gmail_query="newer_than:7d",
        lookback_days=7,
        draft_policy="draft_only_for_urgent",
        constraints=["model-added grouping suggestion"],
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.target_agent == "gmail_triage"
    assert merged.desired_count == 3
    assert merged.desired_count_explicit is True
    assert merged.lookback_days == 7
    assert merged.gmail_query == "newer_than:7d"
    assert merged.draft_policy == "no_drafts_requested"
    assert "create" not in merged.provider_operations
    assert "update" not in merged.provider_operations
    assert [
        (step.operation, step.resource_type) for step in merged.provider_action_steps
    ] == [("read", "gmail_message")]
    assert "model-added grouping suggestion" in merged.constraints


def test_manual_plan_distinguishes_review_copy_from_provider_draft() -> None:
    plan = infer_manual_request_plan(
        "Review the supplied Gmail thread and prepare one concise reply for review. "
        "Do not send, create a provider draft, search, post, schedule, share, or "
        "write externally.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "gmail_triage"
    assert plan.draft_policy == "draft_only_when_reply_needed"


def test_gmail_set_aside_wording_does_not_grant_provider_update_authority() -> None:
    plan = infer_manual_request_plan(
        "I had an interview at 10:30 this morning. Find the current Gmail "
        "conversation, set aside cancellations or old times, and give me a brief "
        "Slack-only follow-up saying thanks. Don't create a Gmail draft or send "
        "anything.",
        requested_agent="gmail_triage",
    )

    assert plan.provider_operations == ["read"]
    assert plan.draft_policy == "draft_only_when_reply_needed"


def test_manual_plan_direct_no_reply_instruction_remains_authoritative() -> None:
    plan = infer_manual_request_plan(
        "Summarize the selected Gmail thread and do not draft replies.",
        requested_agent="orchestrator",
    )

    assert plan.draft_policy == "no_drafts_requested"


def test_manual_plan_specific_agent_call_stays_on_requested_agent() -> None:
    plan = infer_manual_request_plan(
        "run one opportunity-to-outreach loop for behavioral health AI. Top 1 only.",
        requested_agent="opportunity scout",
    )

    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_search"


def test_manual_plan_chief_of_staff_reference_capture_is_not_slack_ops() -> None:
    plan = infer_manual_request_plan(
        (
            "keep this for future reference: NIH AI conference with virtual attendees: "
            "https://braininitiative.nih.gov/news-events/blog/register-now-nih-brain-neuroai-workshop"
        ),
        requested_agent="chief of staff",
    )

    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "reference_capture"
    assert plan.target_type == "operator_reference"
    assert plan.primary_target == "NIH AI conference with virtual attendees"


@pytest.mark.parametrize(
    ("prompt", "expected_intent"),
    [
        (
            "Review the business-agent architecture changes and recommend the next three implementation steps.",
            "route_request",
        ),
        (
            "Audit enabled automations and identify stale, duplicate, or unsafe schedules without changing them.",
            "route_request",
        ),
        (
            "Summarize the selected Slack thread, identify unresolved operator requests, "
            "and propose an internal follow-up plan.",
            "slack_operations",
        ),
    ],
)
def test_manual_plan_routes_operational_planning_to_chief_of_staff(
    prompt: str,
    expected_intent: str,
) -> None:
    plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    assert plan.requested_agent == "orchestrator"
    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == expected_intent
    assert plan.task_objective == (
        "slack_operations" if expected_intent == "slack_operations" else "route_or_continue"
    )
    assert plan.expected_artifact_type == (
        "slack_ops_summary" if expected_intent == "slack_operations" else "none"
    )
    assert plan.side_effect_policy == "draft_or_read_only"


@pytest.mark.parametrize(
    ("prompt", "requested_agent"),
    [
        (
            "@KNI chief of staff add a business expense to the airtable business expenses "
            "based on the receipt details which are: "
            "/tmp/example-business-cards-receipt.pdf",
            "orchestrator",
        ),
        (
            "Add a business expense to Airtable business expenses from "
            "/tmp/example-business-cards-receipt.pdf",
            "orchestrator",
        ),
    ],
)
def test_manual_plan_routes_finance_receipt_write_to_business_system_plan(
    prompt: str,
    requested_agent: str,
) -> None:
    plan = infer_manual_request_plan(prompt, requested_agent=requested_agent)

    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "business_system_write"
    assert plan.task_objective == "business_system_write"
    assert plan.target_type == "business_system_context"
    assert plan.expected_artifact_type == "business_system_write_plan"
    assert plan.side_effect_policy == "internal_write_approval_required"


def test_manual_plan_preserves_prefix_stripped_airtable_receipt_lifecycle() -> None:
    request = (
        "add /tmp/KBA_TEST_RECEIPT.pdf to Airtable Business Expenses. "
        "Use 2026-08-05 as Date of Expense and attach the exact PDF. "
        "This exact one-record live write and attachment upload is approved. "
        "Verify the record and attachment; do not create a duplicate."
    )

    plan = infer_manual_request_plan(
        request,
        requested_agent="airtable_context_agent",
    )

    assert plan.requested_agent == "airtable_context_agent"
    assert plan.target_agent == "airtable_context_agent"
    assert plan.intent == "business_system_write"
    assert plan.task_objective == "business_system_write"
    assert plan.primary_target == "Business Expenses"
    assert plan.target_type == "business_system_context"
    assert plan.provider_system == "airtable"
    assert plan.provider_operations == ["create", "attach", "verify"]
    assert plan.expected_artifact_type == "business_system_write_plan"
    assert plan.side_effect_policy == "internal_write_approval_required"


@pytest.mark.parametrize(
    "prompt",
    [
        (
            "Research Lindus Health. Return exactly: Summary, Evidence, "
            "Keystone relevance, Concerns, Suggested next step."
        ),
        "Research Lindus Health. Return as: Summary / Evidence / Concerns.",
        "Research Lindus Health. Use sections: Summary, Evidence, Next step.",
    ],
)
def test_manual_plan_strips_short_business_research_format_tail(prompt: str) -> None:
    plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"
    assert plan.primary_target == "Lindus Health"


def test_manual_plan_planning_first_workflow_uses_safe_scout_target() -> None:
    plan = infer_manual_request_plan(
        (
            "Plan the safest workflow to find companies, research the best candidate, "
            "and prepare outreach, but do not save or send anything."
        ),
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_search"
    assert plan.primary_target == "behavioral health AI clinical research"


@pytest.mark.parametrize(
    ("prompt", "target_agent", "intent"),
    [
        (
            "Create a LinkedIn variant only and include the facts used plus source ids used.",
            "outreach_composer",
            "outreach_draft",
        ),
        (
            "Label selected messages as follow-up candidates.",
            "gmail_triage",
            "gmail_triage",
        ),
        (
            "What should I follow up on this week from email? Draft replies only where needed.",
            "gmail_triage",
            "gmail_triage",
        ),
        (
            "Audit why a previous @KNI response felt unrelated and tell me which agent path should have handled it.",
            "chief_of_staff",
            "route_request",
        ),
    ],
)
def test_manual_plan_routes_agent_specific_variants_to_owning_agent(
    prompt: str,
    target_agent: str,
    intent: str,
) -> None:
    plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    assert plan.target_agent == target_agent
    assert plan.intent == intent


def test_manual_plan_routes_partnership_signal_discovery_to_opportunity_scout() -> None:
    plan = infer_manual_request_plan(
        (
            "Find 3 remote US behavioral health AI partnerships from the last 30 days, "
            "exclude staffing agencies, and keep hard filters in scoring."
        ),
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_search"
    assert plan.desired_count == 3
    assert "remote" in plan.constraints
    assert "us-relevant" in plan.constraints
    assert any("staffing agencies" in constraint for constraint in plan.constraints)


def test_manual_plan_extracts_business_research_target_from_direct_call() -> None:
    plan = infer_manual_request_plan(
        "research NeuroFlow recent partnerships and clinical AI relevance",
        requested_agent="business research analyst",
    )

    assert plan.requested_agent == "business_research_analyst"
    assert plan.target_agent == "business_research_analyst"
    assert "NeuroFlow" in plan.primary_target
    assert plan.task_objective == "source_research"
    assert plan.expected_artifact_type == "research_brief"
    assert "NeuroFlow" in plan.objective


def test_manual_plan_preserves_colon_named_business_research_mention() -> None:
    plan = infer_manual_request_plan(
        (
            "@KNI business research analyst: diagnostic case diag_business_research_abrdg_001 "
            "Read-only source-backed business research test for Abridge. Use live search if "
            "available. Identify up to 2 practical healthcare buyer-fit angles for Keystone, "
            "explain why each might fit, and state key caveats. Keep answer concise and "
            "include visible source URLs. Do not draft outreach, send, schedule, write files, "
            "create CRM records, publish, or post elsewhere."
        ),
        requested_agent="orchestrator",
    )

    assert plan.requested_agent == "orchestrator"
    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"
    assert plan.primary_target == "Abridge"
    assert plan.task_objective == "source_research"
    assert plan.requires_live_search is True


def test_manual_plan_routes_polite_business_research_agent_ask() -> None:
    plan = infer_manual_request_plan(
        (
            "@KNI could the business research analyst take a quick read-only look at "
            "Nabla as a clinical AI documentation company? Please give the short answer "
            "first, then a source-backed summary with visible URLs. No outreach, drafts, "
            "scheduling, file creation, CRM records, publishing, or posting elsewhere."
        ),
        requested_agent="orchestrator",
    )

    assert plan.requested_agent == "orchestrator"
    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"
    assert plan.primary_target == "Nabla"
    assert plan.target_type == "company"
    assert plan.task_objective == "source_research"
    assert plan.requires_live_search is True


def test_manual_plan_extracts_fit_check_company_for_business_research() -> None:
    plan = infer_manual_request_plan(
        (
            "@KNI business research analyst: Could you do a concise read-only Keystone "
            "fit check on Suki as a clinical AI assistant company? Focus on buyer fit "
            "and evidence signals."
        ),
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "business_research_analyst"
    assert plan.primary_target == "Suki"
    assert plan.target_type == "company"


def test_manual_plan_extracts_company_after_diagnostic_prefix() -> None:
    plan = infer_manual_request_plan(
        (
            "diagnostic case diag_business_research_abrdg_001 Research Abridge as a "
            "clinical documentation AI company. Return a concise Answer and Detailed Summary."
        ),
        requested_agent="business_research_analyst",
    )

    assert plan.target_agent == "business_research_analyst"
    assert plan.primary_target == "Abridge"
    assert plan.target_type == "company"


def test_manual_plan_prefers_company_label_after_source_context_instruction() -> None:
    plan = infer_manual_request_plan(
        (
            "@KNI business research analyst: Use only this source-provided context. "
            "Company: Northline Imaging. Context: Northline Imaging sells operational "
            "analytics for outpatient imaging centers and is preparing an internal "
            "capacity-planning pilot. No PHI is included. Return Answer and Detailed Summary."
        ),
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "business_research_analyst"
    assert plan.primary_target == "Northline Imaging"
    assert plan.target_type == "company"
    assert plan.requires_live_search is False


def test_manual_plan_routes_unnamed_multi_company_business_research_to_scout() -> None:
    plan = infer_manual_request_plan(
        (
            "business research analyst compare three software-first companies with "
            "measurement-based care tools for behavioral health clinics"
        ),
        requested_agent="business research analyst",
    )

    assert plan.requested_agent == "business_research_analyst"
    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_search"
    assert plan.target_type == "topic"
    assert (
        plan.primary_target
        == "software-first companies with measurement-based care tools for behavioral health clinics"
    )
    assert plan.desired_count == 3
    assert plan.requires_live_search is True


@pytest.mark.parametrize(
    "requested_agent",
    ["orchestrator", "chief_of_staff", "opportunity_scout"],
)
def test_anchor_plus_competitor_research_keeps_business_research_owner(
    requested_agent: str,
) -> None:
    plan = infer_manual_request_plan(
        (
            "CoS, deeply research Northstar Health as the anchor. First characterize "
            "its voice-based assessment product, then identify up to 2 closest "
            "evidence-backed competitors. Do not count Northstar Health as a competitor."
        ),
        requested_agent=requested_agent,
    )

    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"
    assert plan.primary_target == "Northstar Health"
    assert plan.target_type == "company"
    assert plan.desired_count == 2
    assert plan.desired_count_explicit is True
    assert plan.requires_live_search is True


def test_manual_plan_routes_source_backed_vendor_table_to_business_research() -> None:
    plan = infer_manual_request_plan(
        (
            "make a source-backed table comparing three behavioral-health workflow "
            "vendors for Keystone relevance"
        ),
        requested_agent="orchestrator",
    )

    assert plan.requested_agent == "orchestrator"
    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"
    assert plan.task_objective == "source_research"
    assert plan.expected_artifact_type == "source_summary"
    assert "comparison-format" in plan.constraints


def test_manual_plan_does_not_treat_comparison_smoke_as_output_format() -> None:
    plan = infer_manual_request_plan(
        (
            "chief of staff NeuroFlow has payer partnership signals. Do research and "
            "assess whether this is a KNI opportunity. Live SDK is approved only for "
            "this bounded comparison smoke; live web search is not approved."
        ),
        requested_agent="chief_of_staff",
    )

    assert "NeuroFlow" in plan.primary_target
    assert plan.desired_count == 1
    assert "comparison-format" not in plan.constraints


def test_manual_plan_routes_source_provided_vendor_summary_table_to_business_research() -> None:
    plan = infer_manual_request_plan(
        (
            "make a table of buyer, evidence, risk, and next safe action from these "
            "two vendor summaries: Vendor A sells behavioral-health workflow analytics. "
            "Vendor B sells employer mental-health navigation."
        ),
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"
    assert plan.task_objective == "source_research"
    assert plan.target_type == "company"


def test_manual_plan_routes_eval_scorecard_followup_to_chief_of_staff() -> None:
    plan = infer_manual_request_plan(
        (
            "make a scorecard for this eval case using these details: prompt tested "
            "source visibility, response cited one fixture source, human reviewer "
            "noted missing primary URL and unclear caveats. Return scores, missing "
            "evidence, and next fix; do not save the scorecard unless explicitly approved."
        ),
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "route_request"
    assert plan.task_objective == "route_or_continue"
    assert plan.expected_artifact_type == "none"


def test_manual_plan_cleans_quoted_direct_agent_discovery_prompt() -> None:
    plan = infer_manual_request_plan(
        (
            'business research analyst "Compare three software-first companies with '
            "measurement-based care or digital psychiatry tools for behavioral health clinics. "
            'Please give me a compact comparison table and visible source URLs."'
        ),
        requested_agent="business research analyst",
    )

    assert plan.target_agent == "opportunity_scout"
    assert plan.desired_count == 3
    assert (
        plan.primary_target
        == "software-first companies with measurement-based care or digital psychiatry tools "
        "for behavioral health clinics"
    )
    assert plan.required_entities == []


def test_manual_plan_routes_slack_normalized_research_analyst_discovery() -> None:
    plan = infer_manual_request_plan(
        (
            'research analyst "Compare three software-first companies with '
            "measurement-based care or digital psychiatry tools for behavioral health clinics. "
            'Please give me a compact comparison table and visible source URLs."'
        ),
        requested_agent=None,
    )

    assert plan.requested_agent == "business_research_analyst"
    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_search"
    assert (
        plan.primary_target
        == "software-first companies with measurement-based care or digital psychiatry tools "
        "for behavioral health clinics"
    )


def test_manual_plan_preserves_deep_search_output_constraints() -> None:
    plan = infer_manual_request_plan(
        (
            "business research analyst compare three software-first companies with "
            "measurement-based care tools for behavioral health clinics. Please do a "
            "deeper search, include visible source URLs, return Answer, Synthesis, "
            "a compact comparison table, and metadata with providers used."
        ),
        requested_agent="business research analyst",
    )

    assert plan.target_agent == "opportunity_scout"
    assert plan.requires_live_search is True
    assert {
        "deeper-search",
        "visible-source-urls",
        "metadata-section",
        "comparison-format",
        "answer-and-synthesis",
    } <= set(plan.constraints)
    assert plan.required_entities == []


def test_manual_plan_bounded_business_research_smoke_ignores_negated_scouting() -> None:
    plan = infer_manual_request_plan(
        (
            "Research smoke: research NeuroFlow for a short internal read-only company note. "
            "Stay on Business Research only; do not scout opportunities or draft outreach. "
            "Live SDK is approved only for this bounded read-only smoke if the backend would "
            "normally use it; live web search is not approved. Use local/dry-run retrieval "
            "where possible. Do not send email, create drafts, post elsewhere, publish, "
            "schedule, or write external systems."
        ),
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"
    assert plan.primary_target == "NeuroFlow"
    assert plan.requires_live_search is False
    assert plan.expected_artifact_type == "research_brief"


def test_llm_company_plan_is_not_vetoed_by_discovery_keyword_fallback() -> None:
    fallback = infer_manual_request_plan(
        (
            'research analyst "Compare three software-first companies with '
            "measurement-based care or digital psychiatry tools for behavioral health clinics. "
            'Please give me a compact comparison table and visible source URLs."'
        ),
        requested_agent=None,
    )
    bad_candidate = ManualRequestPlan(
        source="llm",
        requested_agent="business_research_analyst",
        target_agent="business_research_analyst",
        intent="company_research",
        primary_target=(
            "software-first companies with measurement-based care or digital psychiatry tools "
            "for behavioral health clinics"
        ),
        target_type="company",
        task_objective="entity_research",
        expected_artifact_type="research_brief",
        desired_count=3,
        required_entities=[
            "Compare three software-first companies with measurement-based care or digital "
            "psychiatry tools for behavioral health clinics."
        ],
    )

    merged = merge_manual_request_plan(fallback, bad_candidate)

    assert merged.target_agent == "business_research_analyst"
    assert merged.intent == "company_research"
    assert merged.target_type == "company"
    assert merged.required_entities == bad_candidate.required_entities


def test_explicit_named_agent_is_advice_not_llm_owner_authority() -> None:
    fallback = infer_manual_request_plan(
        "research NeuroFlow recent partnerships and clinical AI relevance",
        requested_agent="business research analyst",
    )
    bad_candidate = ManualRequestPlan(
        source="llm",
        requested_agent="business_research_analyst",
        target_agent="opportunity_scout",
        intent="opportunity_search",
        primary_target="NeuroFlow partnerships",
        target_type="opportunity",
    )

    merged = merge_manual_request_plan(fallback, bad_candidate)

    assert merged.requested_agent == "business_research_analyst"
    assert merged.target_agent == "opportunity_scout"
    assert merged.intent == "opportunity_search"
    assert merged.primary_target == "NeuroFlow partnerships"


def test_llm_owner_is_not_replaced_by_fallback_source_keyword_route() -> None:
    fallback = infer_manual_request_plan(
        "Triage my unread Gmail messages from today.",
        requested_agent="opportunity_scout",
    )
    bad_candidate = ManualRequestPlan(
        source="llm",
        requested_agent="opportunity_scout",
        target_agent="opportunity_scout",
        intent="opportunity_search",
        primary_target="unread Gmail messages",
    )

    merged = merge_manual_request_plan(fallback, bad_candidate)

    assert merged.requested_agent == "opportunity_scout"
    assert merged.target_agent == "opportunity_scout"
    assert merged.intent == "opportunity_search"


@pytest.mark.parametrize(
    ("requested_agent", "bad_target"),
    [
        ("business research analyst", "opportunity_scout"),
        ("opportunity scout", "business_research_analyst"),
        ("gmail_triage", "chief_of_staff"),
        ("outreach composer", "opportunity_scout"),
        ("chief of staff", "business_research_analyst"),
    ],
)
def test_llm_owner_can_refine_all_explicit_named_agent_mentions(
    requested_agent: str,
    bad_target: str,
) -> None:
    fallback = infer_manual_request_plan(
        "review the current request and prepare the right next step",
        requested_agent=requested_agent,
    )
    bad_candidate = ManualRequestPlan(
        source="llm",
        requested_agent=fallback.requested_agent,
        target_agent=bad_target,
        intent="company_research" if bad_target == "business_research_analyst" else "route_request",
        primary_target="wrong target",
    )

    merged = merge_manual_request_plan(fallback, bad_candidate)

    assert merged.requested_agent == fallback.requested_agent
    assert merged.target_agent == bad_target
    assert merged.intent == bad_candidate.intent


def test_unexplained_llm_clarification_does_not_restore_keyword_owner() -> None:
    request = (
        "Research is mentioned in this note, but decide the right next step now; "
        "the prior Slack reply also mentioned Gmail and Calendar."
    )
    fallback = infer_manual_request_plan(request, requested_agent="orchestrator")
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="orchestrator",
        target_agent="clarification",
        intent="clarification",
        objective="Interpret and complete the operator's supplied task.",
        missing_required_information=[],
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.target_agent == "chief_of_staff"
    assert merged.intent == "route_request"
    assert merged.task_objective == "route_or_continue"
    assert merged.workflow == []
    assert any("No keyword route was restored" in item for item in merged.planner_warnings)


@pytest.mark.parametrize(
    "requested_agent",
    [
        "business_research_analyst",
        "opportunity_scout",
        "gmail_triage",
        "outreach_composer",
        "chief_of_staff",
    ],
)
def test_unexplained_llm_clarification_preserves_explicit_owner_contract(
    requested_agent: str,
) -> None:
    fallback = ManualRequestPlan(
        requested_agent=requested_agent,
        target_agent="clarification",
        objective="Handle the request through the named entry surface.",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent=requested_agent,
        target_agent="clarification",
        intent="clarification",
        objective=fallback.objective,
        missing_required_information=[],
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.target_agent == requested_agent
    assert merged.intent == "route_request"
    assert merged.missing_required_information == []


def test_unexplained_llm_clarification_recovers_typed_calendar_read() -> None:
    fallback = infer_manual_request_plan(
        "Is the referenced session on my schedule?",
        requested_agent="chief_of_staff",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="clarification",
        intent="clarification",
        provider_system="google_calendar",
        provider_operations=["read", "verify"],
        primary_target="referenced session",
        objective="Verify whether the referenced session exists on the calendar.",
        missing_required_information=[],
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.target_agent == "chief_of_staff"
    assert merged.intent == "context_lookup"
    assert merged.task_objective == "context_lookup"
    assert merged.expected_artifact_type == "context_summary"
    assert merged.provider_operations == ["read", "verify"]


def test_llm_provider_read_scope_survives_canonical_plan_merge() -> None:
    fallback = infer_manual_request_plan(
        "What is next calendar event for today?",
        requested_agent="chief_of_staff",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="context_lookup",
        task_objective="context_lookup",
        expected_artifact_type="context_summary",
        provider_system="google_calendar",
        provider_operations=["read"],
        provider_read_scope="bounded_collection",
        primary_target="",
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.provider_system == "google_calendar"
    assert merged.provider_operations == ["read"]
    assert merged.provider_read_scope == "bounded_collection"


def test_internal_gmail_collection_draft_stays_in_one_agent_owned_loop() -> None:
    request = (
        "CoS, find one email from today that seems worth following up on for KNI "
        "and draft a brief reply here in this Slack thread. Don't send it or "
        "create a Gmail draft."
    )
    fallback = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="gmail_triage",
        intent="gmail_triage",
        primary_target="today's email inbox",
        target_type="gmail_message_collection",
        provider_system="gmail",
        provider_operations=["search", "read"],
        provider_read_scope="bounded_collection",
        provider_result_mode="items",
        gmail_mailbox_direction="inbound",
        gmail_date_scope="today",
        gmail_requested_fields=["subject", "sender", "date", "snippet"],
        objective=(
            "Find one inbound email from today that looks worth following up on, "
            "then draft a brief reply in this Slack thread."
        ),
        task_objective="gmail_triage",
        expected_artifact_type="gmail_triage_report",
        desired_count=1,
        desired_count_explicit=True,
        draft_policy="no_drafts_requested",
        ask_shape=AskShapePolicy(
            output_form="draft",
            audience_scope="internal",
            permission_state="read_only",
        ),
    )

    merged = merge_manual_request_plan(fallback, candidate)
    execution = resolve_gmail_execution_plan(request, manual_plan=merged)

    assert merged.target_agent == "gmail_triage"
    assert merged.workflow == []
    assert merged.intent == "gmail_triage"
    assert merged.task_objective == "gmail_triage"
    assert merged.expected_artifact_type == "gmail_triage_report"
    assert merged.outreach_channel == "internal_slack"
    assert merged.requires_durable_state is False
    assert merged.requires_approved_context is False
    assert merged.draft_policy == "no_drafts_requested"
    assert execution.operation == "draft_reply"
    assert execution.create_gmail_drafts is False
    assert execution.draft_replies_in_output is True


def test_manual_plan_maps_business_research_summary_to_source_summary() -> None:
    plan = infer_manual_request_plan(
        "research and summarize highlights from the APA 2026 meeting",
        requested_agent="business research analyst",
    )

    assert plan.target_agent == "business_research_analyst"
    assert plan.target_type == "conference"
    assert plan.task_objective == "source_research"
    assert plan.expected_artifact_type == "source_summary"


def test_manual_plan_routes_named_research_browser_diagnostics_to_chief_of_staff() -> None:
    plan = infer_manual_request_plan(
        (
            "Use backend browser diagnostics to check https://example.com for "
            "rendered-page, console, and network issues."
        ),
        requested_agent="business research analyst",
    )

    assert plan.requested_agent == "business_research_analyst"
    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "browser_diagnostics"
    assert plan.target_type == "url"
    assert plan.primary_target == "https://example.com"
    assert plan.requires_live_search is False


def test_llm_research_plan_is_not_vetoed_by_browser_keyword_fallback() -> None:
    fallback = infer_manual_request_plan(
        "Use backend browser diagnostics to check https://example.com for console issues.",
        requested_agent="business research analyst",
    )
    bad_candidate = ManualRequestPlan(
        source="llm",
        requested_agent="business_research_analyst",
        target_agent="business_research_analyst",
        intent="company_research",
        primary_target="https://example.com",
        target_type="company",
    )

    merged = merge_manual_request_plan(fallback, bad_candidate)

    assert merged.intent == "company_research"
    assert merged.target_agent == "business_research_analyst"
    assert merged.target_type == "company"


def test_manual_plan_keeps_research_route_when_browser_diagnostics_are_optional() -> None:
    plan = infer_manual_request_plan(
        (
            "Use backend browser diagnostics only if needed. Research https://example.com "
            "and explain whether it is a real operating company."
        ),
        requested_agent="business research analyst",
    )

    assert plan.requested_agent == "business_research_analyst"
    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"
    assert plan.target_type == "url"
    assert plan.primary_target == "https://example.com"


def test_manual_plan_preserves_outreach_approval_gate_in_orchestrator() -> None:
    plan = infer_manual_request_plan(
        "draft outreach to NeuroFlow about clinical AI evaluation",
        requested_agent="orchestrator",
    )
    result = route_request("draft outreach to NeuroFlow", manual_plan=plan)

    assert plan.requires_approved_context is True
    assert result.route == "outreach_composer"
    assert result.approval_state == ApprovalState.PENDING
    assert result.refused is True
    assert result.send_enabled is False


def test_manual_plan_routes_oc5_send_boundary_prompt_to_outreach_composer() -> None:
    prompt = get_test_pack_spec("OC-5").natural_prompt

    plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    assert plan.target_agent == "outreach_composer"
    assert plan.intent == "outreach_draft"
    assert plan.task_objective == "outreach_draft"
    assert plan.expected_artifact_type == "outreach_draft"
    assert plan.requires_approved_context is True
    assert plan.side_effect_policy == "draft_or_read_only"


def test_manual_plan_routes_direct_outreach_send_to_outreach_gate() -> None:
    plan = infer_manual_request_plan(
        "Send the strongest version to the CEO.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "outreach_composer"
    assert plan.intent == "blocked_send"
    assert plan.task_objective == "blocked_side_effect"
    assert plan.requires_approved_context is True
    assert plan.side_effect_policy == "draft_or_read_only"


def test_manual_plan_preserves_google_workspace_read_with_negated_change_scope() -> None:
    plan = infer_manual_request_plan(
        (
            "In KNIOps, find README.doc, report its character count, and count the "
            "direct child folders. Read only. Do not search outside Google Drive or "
            "change anything. Answer with only the two requested values."
        ),
        requested_agent="google_workspace_context_agent",
    )

    assert plan.target_agent == "google_workspace_context_agent"
    assert plan.intent == "context_lookup"
    assert plan.task_objective == "context_lookup"
    assert plan.requires_approved_context is False
    assert plan.ask_shape.permission_state == "read_only"


@pytest.mark.parametrize(
    ("spec_id", "expected_agent", "expected_target"),
    [
        ("OR-1", "business_research_analyst", "Lindus Health"),
        ("OR-2", "business_research_analyst", "Lindus Health"),
        ("OR-3", "opportunity_scout", ""),
        ("OR-5", "business_research_analyst", "Lindus Health"),
    ],
)
def test_manual_plan_starts_orchestrator_workflows_with_safe_specialist(
    spec_id: str,
    expected_agent: str,
    expected_target: str,
) -> None:
    prompt = (
        get_test_pack_spec(spec_id)
        .natural_prompt.replace("[Company]", "Lindus Health")
        .replace(
            "[CEO, Head of Clinical Operations, Head of Partnerships, or Medical Director]",
            "Head of Partnerships",
        )
    )

    plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    assert plan.target_agent == expected_agent
    assert plan.target_agent != "outreach_composer"
    assert plan.requires_approved_context is False
    assert plan.side_effect_policy == "draft_or_read_only"
    if expected_target:
        assert plan.primary_target == expected_target


def test_orchestrator_uses_manual_plan_before_broad_company_fallback() -> None:
    plan = infer_manual_request_plan(
        "Find 5 researchers in psychiatry AI with recent grants",
        requested_agent="orchestrator",
    )
    result = route_request(
        "Find 5 researchers in psychiatry AI with recent grants",
        manual_plan=plan,
    )

    assert result.route == "opportunity_scout"
    assert result.retrieval_hint is not None
    assert any("Manual request plan used" in note for note in result.audit_notes)


def test_manual_plan_extracts_gmail_scope_hints() -> None:
    plan = infer_manual_request_plan(
        "triage unread Gmail threads from the last 3 days and draft replies only for urgent items",
        requested_agent="gmail_triage",
    )

    assert plan.target_agent == "gmail_triage"
    assert plan.gmail_query == "is:unread newer_than:3d"
    assert plan.lookback_days == 3
    assert plan.draft_policy == "draft_only_for_urgent"


def test_manual_plan_routes_sent_mail_style_learning_to_gmail() -> None:
    request = (
        "Review a small sample of my sent emails and draft replies in a similar style "
        "without copying them exactly."
    )

    manual = infer_manual_request_plan(request, requested_agent="orchestrator")
    execution = infer_gmail_execution_plan(request)

    assert manual.target_agent == "gmail_triage"
    assert manual.intent == "gmail_triage"
    assert execution.operation == "style_profile"
    assert execution.source_label == "SENT"
    assert execution.live_read_required is True
    assert execution.create_gmail_drafts is False
    assert "aggregate_email_style_profile_build" in execution.candidate_helpers


def test_gmail_tone_instruction_does_not_become_sent_style_learning() -> None:
    execution = infer_gmail_execution_plan(
        "Find the recent insurance email and draft a reply. Keep the tone natural and professional."
    )

    assert execution.operation == "draft_reply"
    assert execution.source_label != "SENT"


@pytest.mark.parametrize(
    "spec_id",
    ["GT-1", "GT-2", "GT-3", "GT-4", "GT-5"],
)
def test_manual_plan_routes_gmail_test_pack_prompts_to_gmail_triage(spec_id: str) -> None:
    prompt = get_test_pack_spec(spec_id).natural_prompt

    plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    assert plan.target_agent == "gmail_triage"
    assert plan.intent == "gmail_triage"
    assert plan.task_objective == "gmail_triage"
    assert plan.expected_artifact_type == "gmail_triage_report"


def test_manual_planner_configs_try_target_provider_then_openai(monkeypatch) -> None:
    monkeypatch.setenv("KEYSTONE_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test")
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("KEYSTONE_MANUAL_PLANNER_PROVIDER_POLICY", "target_with_openai_fallback")

    configs = _planner_model_configs(requested_agent="gmail_triage")

    assert [config.provider for config in configs] == ["gemini", "openai"]


def test_manual_planner_openai_policy_explicitly_pins_planner_provider(
    monkeypatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test")
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("KEYSTONE_MANUAL_PLANNER_MODEL", "gpt-5.4-mini")
    monkeypatch.setenv("KEYSTONE_MANUAL_PLANNER_PROVIDER_POLICY", "openai")

    configs = _planner_model_configs(requested_agent="gmail_triage")

    assert [(config.provider, config.model) for config in configs] == [
        ("openai", "gpt-5.4-mini")
    ]


def test_manual_planner_defaults_to_dedicated_openai_profile(
    monkeypatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test")
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("KEYSTONE_ORCHESTRATOR_MODEL", "orchestrator-experiment")
    monkeypatch.delenv("KEYSTONE_MANUAL_PLANNER_PROVIDER_POLICY", raising=False)
    monkeypatch.delenv("KEYSTONE_MANUAL_PLANNER_MODEL", raising=False)

    configs = _planner_model_configs(requested_agent="gmail_triage")

    assert [(config.provider, config.model) for config in configs] == [
        ("openai", "gpt-5.4-mini")
    ]


def test_manual_planner_unknown_policy_fails_safe_to_dedicated_openai(
    monkeypatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test")
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("KEYSTONE_MANUAL_PLANNER_PROVIDER_POLICY", "typo")

    configs = _planner_model_configs(requested_agent="gmail_triage")

    assert [(config.provider, config.model) for config in configs] == [
        ("openai", "gpt-5.4-mini")
    ]


def test_cli_live_opportunity_scout_uses_script_retrieval_path(monkeypatch, capsys) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"records": [], "send_enabled": False}),
            stderr="",
        )

    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _offline_orchestrator_preflight)

    assert (
        cli.main(
            [
                "ask",
                "--agent",
                "opportunity_scout",
                "--live-sdk",
                "--json",
                (
                    "Find 2 U.S.-relevant academic institutes. Use live SDK and live "
                    "search. No outreach."
                ),
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["selected_agent"] == "opportunity_scout"
    assert output["manual_request_plan"]["desired_count"] == 2
    assert "scripts/run_opportunity_scout.py" in captured["command"]
    assert "--live-search" in captured["command"]
    assert captured["command"][captured["command"].index("--max-results") + 1] == "2"
    assert captured["command"][captured["command"].index("--topic") + 1] == (
        "Find 2 U.S.-relevant academic institutes. Use live SDK and live search. No outreach."
    )
    assert "--live-search-plan" not in captured["command"]


def test_cli_live_browser_diagnostics_named_research_reroutes_to_chief(monkeypatch, capsys) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "output_type": "ChiefOfStaffResult",
                    "output": {"summary": "Rendered page diagnostics completed."},
                    "send_enabled": False,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _offline_orchestrator_preflight)

    assert (
        cli.main(
            [
                "ask",
                "--agent",
                "business_research_analyst",
                "--live-sdk",
                "--json",
                (
                    "Use backend browser diagnostics to check https://example.com "
                    "for rendered-page, console, and network issues."
                ),
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["selected_agent"] == "chief_of_staff"
    assert output["manual_request_plan"]["intent"] == "browser_diagnostics"
    assert output["manual_request_plan"]["primary_target"] == "https://example.com"
    assert "scripts/run_chief_of_staff.py" in captured["command"]


def test_cli_live_business_research_url_target_passes_company_url(monkeypatch, capsys) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "output_type": "CompanyResearchFocusedBrief",
                    "output": {"company_name": "example.com", "source_ids_used": []},
                    "send_enabled": False,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _offline_orchestrator_preflight)

    assert (
        cli.main(
            [
                "ask",
                "--agent",
                "business_research_analyst",
                "--live-sdk",
                "--json",
                (
                    "Use backend browser diagnostics only if needed. Research https://example.com "
                    "and explain whether it is a real operating company."
                ),
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["selected_agent"] == "business_research_analyst"
    assert output["manual_request_plan"]["target_type"] == "url"
    assert captured["command"][captured["command"].index("--company") + 1] == "example.com"
    assert captured["command"][captured["command"].index("--company-url") + 1] == (
        "https://example.com"
    )


def test_cli_live_outreach_blocks_without_approved_context(capsys) -> None:
    assert (
        cli.main(
            [
                "ask",
                "--agent",
                "outreach_composer",
                "--live-sdk",
                "--json",
                "draft outreach to NeuroFlow",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "blocked"
    assert output["requires_approved_context"] is True
    assert output["send_enabled"] is False
    assert "source-backed company or opportunity context" in output["message"]


def test_gmail_execution_plan_maps_recent_actionable_threads_to_priority_grouping() -> None:
    plan = infer_gmail_execution_plan(
        "review recent Gmail threads from the last 7 days related to Keystone opportunities "
        "or follow-ups. Identify the top 3 actionable threads, summarize each, and draft "
        "replies only where a reply is needed. Do not send."
    )

    assert plan.operation == "priority_grouping"
    assert plan.lookback_days == 7
    assert plan.live_read_required is True
    assert plan.create_gmail_drafts is False
    assert plan.draft_replies_in_output is True
    assert plan.gmail_query == "newer_than:7d"
    assert "gmail_priority_grouping_sdk" in plan.candidate_helpers


def test_gmail_execution_plan_keeps_one_conditional_reply_agent_owned() -> None:
    request = (
        "Can you help me find a recent message where someone was waiting for my "
        "feedback on a deck, report, or proposal? Please compare up to four plausible "
        "current conversations, decide whether one clearly needs an answer, and write "
        "a short reply for me here if it does. If none is convincing, say what you "
        "checked and why you're unsure. Keep this read-only—don't draft in Gmail, "
        "send, label, archive, or modify anything."
    )

    plan = infer_gmail_execution_plan(request)

    assert plan.operation == "draft_reply"
    assert plan.read_scope == "collection"
    assert plan.max_messages == 4
    assert plan.create_gmail_drafts is False
    assert plan.draft_replies_in_output is True
    assert plan.candidate_helpers == [
        "query_gmail_message_summaries",
        "read_gmail_context",
        "gmail_triage_sdk",
    ]


def test_natural_today_email_summary_preserves_calendar_scope_and_batch_shape() -> None:
    request = "Can you summarize my emails from today?"
    manual = infer_manual_request_plan(request, requested_agent="orchestrator")
    execution = infer_gmail_execution_plan(request)
    today = datetime.now(ZoneInfo("America/New_York")).date().strftime("%Y/%m/%d")

    assert manual.target_agent == "gmail_triage"
    assert manual.lookback_days == 1
    assert manual.gmail_query == f"after:{today}"
    assert execution.operation == "priority_grouping"
    assert execution.lookback_days == 1
    assert execution.gmail_query == f"after:{today}"
    assert execution.max_messages == 25


def test_natural_find_email_and_draft_preserves_sender_search_hint() -> None:
    request = (
        "Can you find the latest email from Example Health and draft a reply for me? "
        "Do not send it."
    )
    manual = infer_manual_request_plan(request, requested_agent="orchestrator")
    execution = infer_gmail_execution_plan(request)

    assert manual.target_agent == "gmail_triage"
    assert manual.gmail_query == '"Example Health"'
    assert execution.operation == "draft_reply"
    assert execution.gmail_query == 'newer_than:3d "example health"'
    assert execution.create_gmail_drafts is False
    assert execution.draft_replies_in_output is True


def test_calendar_thread_followup_switches_to_read_only_gmail_reply_copy() -> None:
    request = (
        "Can you find the email thread associated with the interview you just listed "
        "and write a short reply here saying I’m looking forward to it? Please don’t "
        "send it or create a Gmail draft—just give me the copy in this thread."
    )

    plan = infer_manual_request_plan(request, requested_agent="orchestrator")
    execution = resolve_gmail_execution_plan(request, manual_plan=plan)

    assert plan.target_agent == "gmail_triage"
    assert plan.provider_system == "gmail"
    assert plan.provider_operations == ["read"]
    assert plan.ask_shape.prior_context_dependency == "selected_context"
    assert execution.operation == "draft_reply"
    assert execution.read_scope == "thread"
    assert execution.create_gmail_drafts is False
    assert execution.draft_replies_in_output is True
    assert execution.side_effect_policy == "read_only_or_draft_only"


@pytest.mark.parametrize(
    ("request_text", "query"),
    [
        (
            "Use the G2i interview on tomorrow’s calendar to identify the related "
            "email conversation, read that thread, and give me a brief reply here. "
            "Keep it in Slack—don’t send anything or save a Gmail draft.",
            '"G2i"',
        ),
        (
            "Find the email thread associated with the Acme Health meeting and write "
            "a concise response here. Don't send it or create a Gmail draft.",
            '"Acme Health"',
        ),
        (
            "Using my Northstar call from Calendar, locate the related email "
            "conversation and return reply copy in this Slack thread only.",
            '"Northstar"',
        ),
        (
            "Use the G2i interview already identified on tomorrow’s calendar to find "
            "the related Gmail conversation. Read that thread and give me a concise "
            "reply here. Keep this as Slack copy only; don’t send it or save a Gmail "
            "draft.",
            '"G2i"',
        ),
    ],
)
def test_source_provider_selector_does_not_override_gmail_action_owner(
    request_text: str,
    query: str,
) -> None:
    plan = infer_manual_request_plan(request_text, requested_agent="orchestrator")
    execution = resolve_gmail_execution_plan(request_text, manual_plan=plan)

    assert plan.target_agent == "gmail_triage"
    assert plan.provider_system == "gmail"
    assert set(plan.provider_operations) <= {"read", "search", "verify"}
    assert plan.gmail_query == query
    assert execution.operation == "draft_reply"
    assert execution.gmail_query == query
    assert execution.create_gmail_drafts is False
    assert execution.draft_replies_in_output is True


def test_calendar_remains_owner_when_email_is_only_event_description_content() -> None:
    request = (
        "List tomorrow's Google Calendar events and include any email address in the "
        "event descriptions. Read-only; do not modify Calendar or Gmail."
    )

    plan = infer_manual_request_plan(request, requested_agent="orchestrator")

    assert plan.target_agent == "chief_of_staff"
    assert plan.provider_system == "google_calendar"
    assert plan.provider_operations == ["read"]


@pytest.mark.parametrize(
    "mailbox_category",
    [
        "event notices",
        "calendar invite emails",
        "meeting reminders",
        "appointment confirmations",
    ],
)
def test_gmail_message_categories_do_not_require_calendar_context(
    mailbox_category: str,
) -> None:
    request = (
        "Gmail Triage, find the recent MassChallenge conversation where a real "
        "person followed up about an application or next step. Compare it with "
        f"automated confirmations, {mailbox_category}, and older scheduling "
        "messages, then decide whether I owe a reply. If yes, give me exactly two "
        "sentences I can paste here; if no, explain why in one sentence. Keep "
        "Gmail unchanged—no draft, labels, archive, or send."
    )

    plan = infer_manual_request_plan(request, requested_agent="gmail_triage")

    assert plan.target_agent == "gmail_triage"
    assert plan.provider_system == "gmail"
    assert plan.provider_context_requirements == []


@pytest.mark.parametrize(
    ("request_text", "expected_provider", "expected_operations"),
    [
        (
            "My Calendar has a team meeting tomorrow. Separately, find the correct "
            "invoice email in Gmail and give me a short reply here without sending "
            "or creating a provider draft.",
            "gmail",
            ["read"],
        ),
        (
            "The email already includes the appointment details and is the only "
            "evidence to use. Read that Gmail thread and write reply copy here. Do "
            "not check Calendar or create a provider draft.",
            "gmail",
            ["read"],
        ),
        (
            "The correct invoice email is not associated with tomorrow's Calendar "
            "meeting. Find the invoice email in Gmail and give me reply copy here "
            "without sending or creating a provider draft.",
            "gmail",
            ["read"],
        ),
        (
            "Do not access Calendar. Find the Gmail thread related to the meeting "
            "details already selected in this thread and give me reply copy here "
            "without sending or saving a provider draft.",
            "gmail",
            ["read"],
        ),
        (
            "Using only the meeting and email details already supplied in this "
            "thread, return a one-sentence reply. Do not access Calendar or Gmail.",
            "unspecified",
            [],
        ),
        (
            "Using only this supplied excerpt—\"Use my next Calendar meeting to "
            "choose the correct Gmail thread.\"—return one sentence. Do not access "
            "Calendar or Gmail.",
            "unspecified",
            [],
        ),
    ],
)
def test_unrelated_or_forbidden_calendar_context_is_not_admitted(
    request_text: str,
    expected_provider: str,
    expected_operations: list[str],
) -> None:
    plan = infer_manual_request_plan(request_text)

    assert plan.provider_system == expected_provider
    assert plan.provider_context_requirements == []
    assert plan.provider_operations == expected_operations
    _assert_no_google_calendar_access(plan)


def _assert_no_google_calendar_access(plan: ManualRequestPlan) -> None:
    authority = ExecutionIntentAuthority.from_value(plan)

    assert plan.provider_system != "google_calendar"
    assert authority.effective_provider_operations("google_calendar") == ()
    assert all(
        requirement.provider_system != "google_calendar"
        for requirement in plan.provider_context_requirements
    )
    assert all(
        step.resource_type != "calendar_event"
        for step in plan.provider_action_steps
    )
    assert "google_calendar" not in plan.ask_shape.source_type_preference


@pytest.mark.parametrize(
    "request_text",
    [
        (
            "My Calendar has a Zephyr meeting tomorrow. KBA_TEST_VENDOR Vela billed "
            "me twice; find the related email in Gmail and draft a reply here "
            "without sending."
        ),
        (
            "My Calendar has an Orion meeting tomorrow. Find the correct "
            "KBA_TEST_INVOICE email in Gmail and draft a reply here without sending."
        ),
        (
            "Find the KBA_TEST_VENDOR Vela billing email in Gmail and write a reply "
            "here without sending. My Calendar also has an unrelated Quartz meeting "
            "tomorrow."
        ),
        (
            "Use my Calendar meeting tomorrow only as background. KBA_TEST_CASE Nova "
            "is a different billing issue; find the email related to that issue in "
            "Gmail and return reply copy here without sending."
        ),
        (
            "My Calendar has a Vega meeting tomorrow; KBA_TEST_VENDOR Luma charged "
            "twice and I need the related email in Gmail. Draft a reply here without "
            "sending."
        ),
        (
            "My Calendar has a Vega meeting tomorrow and KBA_TEST_ACCOUNT Luma has "
            "duplicate charges so find the related email in Gmail and draft a reply "
            "here without sending."
        ),
        (
            "My Calendar has a Vega meeting tomorrow. KBA_TEST_ACCOUNT Luma has "
            "duplicate charges and I need the related email in Gmail. Draft a reply "
            "here without sending."
        ),
        (
            "My Calendar has a Vega meeting tomorrow and KBA_TEST_VENDOR Luma charged "
            "twice, so find its email in Gmail and draft a reply here without sending."
        ),
        (
            "My Calendar has a Vega meeting tomorrow, then find KBA_TEST_CASE Nova "
            "and choose the related email in Gmail. Return reply copy here without "
            "sending."
        ),
    ],
)
def test_calendar_background_does_not_authorize_calendar_access_for_gmail_reply(
    request_text: str,
) -> None:
    plan = infer_manual_request_plan(request_text)

    assert plan.target_agent == "gmail_triage"
    assert plan.provider_system == "gmail"
    assert plan.provider_operations == ["read"]
    assert plan.draft_policy == "draft_only_when_reply_needed"
    assert plan.ask_shape.permission_state in {"read_only", "draft_only"}
    _assert_no_google_calendar_access(plan)


def test_calendar_only_inline_wording_does_not_manufacture_gmail_ownership() -> None:
    plan = infer_manual_request_plan(
        "Read tomorrow's Calendar meeting details and give me a response here "
        "without sending email."
    )

    assert plan.target_agent == "chief_of_staff"
    assert plan.provider_system == "google_calendar"
    assert plan.provider_operations == ["read"]
    assert plan.provider_context_requirements == []


def test_calendar_modification_is_not_converted_to_gmail_context() -> None:
    plan = infer_manual_request_plan(
        "Move my next KBA_TEST_CLIENT Calendar appointment to 11:00 AM and show me the "
        "staged details. Do not send email."
    )

    assert plan.target_agent == "chief_of_staff"
    assert plan.provider_system == "google_calendar"
    assert plan.provider_operations == ["update"]
    assert plan.provider_context_requirements == []


@pytest.mark.parametrize(
    "request_text",
    [
        "Use the event at 10 tomorrow to locate the matching Gmail thread and reply here.",
        "Use the appointment tomorrow to find its related email and give me reply copy here.",
        (
            "I have an interview tomorrow morning; find its corresponding inbox "
            "conversation and give me reply copy here."
        ),
        (
            "Check my Google Calendar for the next G2i meeting, then find its Gmail "
            "thread and give me reply copy here."
        ),
    ],
)
def test_concrete_calendar_selector_still_requires_calendar_context(
    request_text: str,
) -> None:
    plan = infer_manual_request_plan(request_text, requested_agent="gmail_triage")

    assert any(
        requirement.provider_system == "google_calendar"
        and requirement.resource_type == "calendar_event"
        for requirement in plan.provider_context_requirements
    )


@pytest.mark.parametrize("clue", [
    "a meeting in Nevada next year",
    "the upcoming interview event for developers",
    "an appointment reminder for the software onboarding call tomorrow",
])
def test_event_described_in_email_does_not_require_calendar_lookup(clue):
    request = (
        f"I remember a recent email about {clue}. Can you find it in "
        "reader@example.test and explain the details? Use the email and reply here."
    )
    plan = infer_manual_request_plan(request, requested_agent="gmail_triage")
    assert plan.provider_context_requirements == []


def _explicit_calendar_to_gmail_merge_plans(
    requirements: list[ProviderContextRequirement],
) -> tuple[ManualRequestPlan, ManualRequestPlan]:
    request = (
        "Using selected Calendar context, choose the associated Gmail conversation "
        "and return reply copy here. Do not send or create a Gmail draft."
    )
    fallback = ManualRequestPlan(
        source="heuristic",
        requested_agent="orchestrator",
        target_agent="chief_of_staff",
        intent="context_lookup",
        objective=request,
        task_objective="context_lookup",
        expected_artifact_type="context_summary",
        target_type="business_system_context",
        provider_system="google_calendar",
        provider_operations=["read"],
        provider_action_steps=[
            ManualProviderActionStep(
                operation="read",
                resource_type="calendar_event",
            )
        ],
        ask_shape=AskShapePolicy(permission_state="read_only"),
        side_effect_policy="draft_or_read_only",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="orchestrator",
        target_agent="gmail_triage",
        intent="gmail_triage",
        objective=request,
        task_objective="gmail_triage",
        expected_artifact_type="gmail_triage_report",
        target_type="gmail_thread",
        provider_system="gmail",
        provider_operations=["search", "read", "verify"],
        provider_action_steps=[
            ManualProviderActionStep(
                operation="search",
                resource_type="gmail_thread",
            ),
            ManualProviderActionStep(
                operation="read",
                resource_type="gmail_thread",
            ),
        ],
        provider_context_requirements=requirements,
        provider_read_scope="single_item",
        constraints=["Do not send or create a Gmail draft."],
        draft_policy="no_drafts_requested",
        outreach_channel="internal_slack",
        requires_approved_context=False,
        requires_durable_state=False,
        ask_shape=AskShapePolicy(
            permission_state="read_only",
            output_form="draft",
            audience_scope="internal",
        ),
        side_effect_policy="draft_or_read_only",
    )
    return fallback, candidate


@pytest.mark.parametrize("candidate_as_dict", [False, True])
def test_merge_preserves_required_provider_context_for_downstream_admission(
    candidate_as_dict: bool,
) -> None:
    requirement = ProviderContextRequirement(
        provider_system="google_calendar",
        operations=["search", "read", "verify"],
        resource_type="calendar_event",
        purpose="Select one verified event before Gmail chooses the conversation.",
        required=True,
    )
    fallback, candidate = _explicit_calendar_to_gmail_merge_plans([requirement])

    merged = merge_manual_request_plan(
        fallback,
        candidate.model_dump(mode="json") if candidate_as_dict else candidate,
    )
    admission = provider_context_requirements_satisfied(merged, [])

    assert merged.target_agent == "gmail_triage"
    assert merged.provider_system == "gmail"
    assert merged.provider_operations == ["search", "read", "verify"]
    assert merged.draft_policy == "no_drafts_requested"
    assert any(
        constraint.lower().rstrip(".") == "do not send or create a gmail draft"
        for constraint in merged.constraints
    )
    assert all(
        isinstance(step, ManualProviderActionStep)
        for step in merged.provider_action_steps
    )
    assert merged.provider_context_requirements == [requirement]
    assert isinstance(
        merged.provider_context_requirements[0],
        ProviderContextRequirement,
    )
    assert admission == (
        False,
        ["google_calendar:calendar_event"],
    )


@pytest.mark.parametrize("optional_requirement", [None, False])
def test_merge_preserves_empty_and_optional_provider_context(
    optional_requirement: bool | None,
) -> None:
    requirements = (
        []
        if optional_requirement is None
        else [
            ProviderContextRequirement(
                provider_system="google_calendar",
                operations=["read"],
                resource_type="calendar_event",
                purpose="Use Calendar only when optional context is available.",
                required=optional_requirement,
            )
        ]
    )
    fallback, candidate = _explicit_calendar_to_gmail_merge_plans(requirements)

    merged = merge_manual_request_plan(fallback, candidate)

    assert provider_context_requirements_satisfied(merged, []) == (True, [])
    assert len(merged.provider_context_requirements) == len(requirements)
    if requirements:
        assert merged.provider_context_requirements[0] == requirements[0]
        assert merged.provider_context_requirements[0].required is False


def test_merged_provider_context_accepts_matching_verified_stage() -> None:
    requirement = ProviderContextRequirement(
        provider_system="google_calendar",
        operations=["read"],
        resource_type="calendar_event",
        purpose="Select the verified event before Gmail chooses the conversation.",
    )
    fallback, candidate = _explicit_calendar_to_gmail_merge_plans([requirement])
    merged = merge_manual_request_plan(fallback, candidate)
    stage = ProviderContextStageResult(
        provider_system="google_calendar",
        resource_type="calendar_event",
        selected_object={"event_id": "event-synthetic"},
        decision=AgentDecisionRecord(
            decision_owner="chief_of_staff",
            decision_stage="calendar_context_selection",
            selected_candidate_id="event-synthetic",
            reasoning="Selected the only verified event.",
        ),
        validator_outcome=DecisionValidatorOutcome(
            status="accepted",
            decision_stage="calendar_context_selection",
            selected_candidate_id="event-synthetic",
            candidate_count=1,
            selected_identity_in_candidate_set=True,
            selected_identity_was_read=True,
            reason_code="manager_selection_bound_to_calendar_result_set",
        ),
        read_only=True,
    )

    assert provider_context_requirements_satisfied(merged, [stage]) == (True, [])


def test_cos_gmail_read_and_slack_copy_delegates_to_one_gmail_owner() -> None:
    request = (
        'CoS, find the Gmail email with subject "Why Healthtech Needs a New Kind '
        'of Product Leader". Read its complete thread, tell me briefly what warrants '
        "a response, then draft a concise response here in this Slack thread so I "
        "can copy it. Do not create a Gmail draft, send anything, change labels, "
        "archive, or otherwise modify the mailbox."
    )

    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    execution = infer_gmail_execution_plan(request)

    assert is_single_owner_gmail_reply_request(request) is True
    assert plan.requested_agent == "chief_of_staff"
    assert plan.target_agent == "gmail_triage"
    assert plan.intent == "gmail_triage"
    assert plan.workflow == []
    assert execution.operation == "draft_reply"
    assert execution.read_scope == "thread"
    assert execution.gmail_query == ('subject:"Why Healthtech Needs a New Kind of Product Leader"')
    assert execution.create_gmail_drafts is False
    assert execution.draft_replies_in_output is True


def test_cos_gmail_read_and_paste_copy_rephrase_keeps_one_gmail_owner() -> None:
    request = (
        'CoS, I need a quick reply I can paste. Find the email titled "RUAIH '
        'Certification", read the whole conversation, and tell me briefly whether it '
        "merits a response. Then write a short reply here—choose either one sensible "
        "question or one useful observation. Leave Gmail exactly as it is: no draft, "
        "send, label changes, or archive."
    )

    fallback = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    execution = infer_gmail_execution_plan(request)
    candidate = fallback.model_copy(
        update={
            "source": "llm",
            "target_agent": "gmail_triage",
            "workflow": [],
            "intent": "gmail_triage",
            "provider_system": "gmail",
            "provider_operations": ["read"],
        }
    )
    merged = merge_manual_request_plan(fallback, candidate)

    assert is_single_owner_gmail_reply_request(request) is True
    assert fallback.target_agent == "gmail_triage"
    assert fallback.intent == "gmail_triage"
    assert fallback.workflow == []
    assert merged.target_agent == "gmail_triage"
    assert merged.intent == "gmail_triage"
    assert merged.workflow == []
    assert execution.operation == "draft_reply"
    assert execution.read_scope == "thread"
    assert execution.gmail_query == 'subject:"RUAIH Certification"'
    assert execution.create_gmail_drafts is False
    assert execution.draft_replies_in_output is True


def test_cos_gmail_reply_with_additional_kni_context_is_not_collapsed() -> None:
    request = (
        'CoS, find the Gmail email with subject "Example". Use approved KNI context '
        "from my CV and resume, then draft a response here in this Slack thread so I "
        "can copy it. Do not create a Gmail draft or send anything."
    )

    assert is_single_owner_gmail_reply_request(request) is False
    assert (
        infer_manual_request_plan(request, requested_agent="chief_of_staff").target_agent
        == "chief_of_staff"
    )


def test_llm_plan_keeps_gmail_read_and_slack_copy_with_one_owner() -> None:
    request = (
        'CoS, find the Gmail email with subject "Example". Read its complete thread '
        "and draft a response here in this Slack thread so I can copy it. Do not "
        "create a Gmail draft or send anything."
    )
    fallback = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    candidate = fallback.model_copy(
        update={
            "source": "llm",
            "target_agent": "gmail_triage",
            "workflow": [],
            "intent": "gmail_triage",
            "provider_system": "gmail",
            "provider_operations": ["read"],
        }
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.target_agent == "gmail_triage"
    assert merged.intent == "gmail_triage"
    assert merged.workflow == []


def test_llm_plan_preserves_only_grounded_explicit_heading_requirements() -> None:
    request = (
        "CoS, use headings named Decision brief and Paste-ready internal Slack "
        "note using only this supplied context."
    )
    fallback = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    candidate_constraints = fallback.ask_shape.output_constraints.model_copy(
        update={
            "interpretation": (
                "Return a decision brief and a separate paste-ready internal Slack note."
            ),
            "required_sections": [
                "Decision brief",
                "Paste-ready internal Slack note",
            ],
            "require_section_headings": True,
        }
    )
    candidate = fallback.model_copy(
        update={
            "source": "llm",
            "ask_shape": fallback.ask_shape.model_copy(
                update={"output_constraints": candidate_constraints}
            ),
        }
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.ask_shape.output_constraints.required_sections == [
        "Decision brief",
        "Paste-ready internal Slack note",
    ]
    assert merged.ask_shape.output_constraints.require_section_headings is True

    ungrounded = candidate.model_copy(
        update={
            "ask_shape": candidate.ask_shape.model_copy(
                update={
                    "output_constraints": candidate_constraints.model_copy(
                        update={
                            "required_sections": [
                                "Decision brief",
                                "Legal analysis",
                            ]
                        }
                    )
                }
            )
        }
    )

    ungrounded_merged = merge_manual_request_plan(fallback, ungrounded)

    assert ungrounded_merged.ask_shape.output_constraints.required_sections == []
    assert ungrounded_merged.ask_shape.output_constraints.require_section_headings is False


def test_llm_chief_plan_can_collapse_heuristic_graph_for_supplied_context() -> None:
    request = (
        "CoS, here is all I know: Harbor Bridge Health sells care-navigation "
        "software but has not shared outcomes or an evaluation design. Give me one "
        "decision brief with what is supported, the strongest KNI fit, the first "
        "validation question, and a short internal Slack note I can paste. Use only "
        "this note; do not search, create or modify anything, send email, or post."
    )
    fallback = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    candidate = fallback.model_copy(
        update={
            "source": "llm",
            "target_agent": "chief_of_staff",
            "workflow": [],
            "intent": "route_request",
            "requires_approved_context": True,
            "requires_live_search": False,
            "side_effect_policy": "draft_or_read_only",
            "rationale": (
                "The operator supplied sufficient context for one read-only Chief response."
            ),
        }
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert len(fallback.workflow) > 1
    assert fallback.workflow[0] == "business_research_analyst"
    assert fallback.workflow[-1] == "outreach_composer"
    assert merged.target_agent == "chief_of_staff"
    assert merged.workflow == []
    assert merged.requires_approved_context is False


def test_llm_internal_slack_copy_is_not_treated_as_external_outreach() -> None:
    request = (
        "Outreach Composer: Based only on this fact—Northstar Care has no audited "
        "outcomes—write one sentence for our internal Slack recommending the next "
        "step. Don't send email, create a provider draft, or search."
    )
    fallback = infer_manual_request_plan(request, requested_agent="outreach_composer")
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="outreach_composer",
        target_agent="outreach_composer",
        intent="outreach_draft",
        objective="Write one internal Slack recommendation from the supplied fact.",
        task_objective="outreach_draft",
        expected_artifact_type="outreach_draft",
        outreach_channel="internal_slack",
        provider_system="unspecified",
        provider_operations=[],
        requires_approved_context=True,
        side_effect_policy="draft_or_read_only",
        ask_shape=AskShapePolicy(
            output_form="draft",
            prior_context_dependency="selected_context",
            permission_state="draft_only",
        ),
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert is_internal_slack_composition_plan(merged) is True
    assert merged.requires_approved_context is False
    assert merged.provider_system == "unspecified"
    assert merged.provider_operations == []


def test_llm_cannot_turn_incidental_email_context_into_forbidden_gmail_owner() -> None:
    request = (
        "CoS, I have a note from an email about Cedar Vale Analytics: it says, "
        "\u201cDo not search until the record costs are approved.\u201d The company claims its "
        "care-coordination dashboard flags missed follow-ups, but the note provides "
        "no customer data or validation results. Using only these facts, give me "
        "what is supported, the first question to ask, and one sentence for this "
        "Slack thread. Do not use Gmail, search, or change any provider."
    )
    fallback = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="gmail_triage",
        target_agent="gmail_triage",
        intent="route_request",
        objective="Assess the supplied email note.",
        provider_system="unspecified",
        provider_operations=[],
        side_effect_policy="draft_or_read_only",
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.requested_agent == "chief_of_staff"
    assert merged.target_agent == "chief_of_staff"
    assert merged.intent == "route_request"
    assert merged.provider_system == "unspecified"
    assert merged.provider_operations == []
    assert merged.workflow == []
    assert merged.requires_durable_state is False
    assert any("provider owner" in item for item in merged.planner_warnings)


def test_natural_business_research_alias_normalizes_to_specialist() -> None:
    plan = infer_manual_request_plan(
        "Business Research, assess this supplied note without search.",
        requested_agent="business research",
    )

    assert plan.requested_agent == "business_research_analyst"
    assert plan.target_agent == "business_research_analyst"


def test_explicit_gmail_owner_is_retained_without_forbidden_provider_access() -> None:
    request = (
        "Using only the pasted note, summarize the supported facts. "
        "Do not use Gmail or any provider tools."
    )
    fallback = infer_manual_request_plan(request, requested_agent="gmail_triage")
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="gmail_triage",
        target_agent="gmail_triage",
        workflow=["gmail_triage", "outreach_composer"],
        intent="gmail_triage",
        objective="Read Gmail and summarize the note.",
        provider_system="gmail",
        provider_operations=["read"],
        provider_action_steps=[
            {"operation": "read", "resource_type": "gmail_message"}
        ],
        requires_durable_state=True,
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.requested_agent == "gmail_triage"
    assert merged.target_agent == "gmail_triage"
    assert merged.intent == "gmail_triage"
    assert merged.provider_system == "unspecified"
    assert merged.provider_operations == []
    assert merged.provider_action_steps == []
    assert merged.workflow == []
    assert merged.requires_durable_state is False
    assert any(
        "retaining the explicitly named Gmail Triage owner" in item
        for item in merged.planner_warnings
    )


def test_provider_scope_confinement_does_not_remove_google_workspace_owner() -> None:
    request = (
        "In KNIOps, find README.doc and count its characters. Read only. "
        "Do not search outside Google Drive or change anything."
    )
    fallback = infer_manual_request_plan(
        request,
        requested_agent="google_workspace_context_agent",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="google_workspace_context_agent",
        target_agent="google_workspace_context_agent",
        intent="context_lookup",
        primary_target="README.doc",
        target_type="business_system_context",
        provider_system="google_workspace",
        provider_operations=["read", "search"],
        provider_action_steps=[
            {"operation": "search", "resource_type": "google_drive_file"},
            {"operation": "read", "resource_type": "google_drive_file"},
        ],
        side_effect_policy="draft_or_read_only",
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.target_agent == "google_workspace_context_agent"
    assert merged.intent == "context_lookup"
    assert merged.provider_system == "google_workspace"
    assert merged.provider_operations == ["read", "search"]
    assert not any("Removed a provider owner" in item for item in merged.planner_warnings)


def test_llm_external_outreach_still_requires_approved_context() -> None:
    fallback = infer_manual_request_plan(
        "Draft a concise email to Maya about Northstar Care.",
        requested_agent="outreach_composer",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="outreach_composer",
        target_agent="outreach_composer",
        intent="outreach_draft",
        objective="Draft an external email.",
        task_objective="outreach_draft",
        expected_artifact_type="outreach_draft",
        recipient="Maya",
        outreach_channel="email",
        requires_approved_context=False,
        side_effect_policy="draft_or_read_only",
        ask_shape=AskShapePolicy(permission_state="draft_only"),
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert is_internal_slack_composition_plan(merged) is False
    assert merged.requires_approved_context is True


def test_resumable_supplied_context_uses_typed_durable_state_not_forced_workflow() -> None:
    request = (
        "CoS, track this as a resumable internal review. Use only these approved "
        "facts: Northstar Care sells referral-navigation software; it has not "
        "supplied audited outcomes or an evaluation design. Assess what is supported, "
        "decide the highest-value validation gap, and prepare a paste-ready internal "
        "Slack recommendation. Preserve the assessment and recommendation together so "
        "I can revise the recommendation later. Do not search, use provider tools, "
        "create or modify records, send email, or post."
    )
    fallback = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    candidate = fallback.model_copy(
        update={
            "source": "llm",
            "target_agent": "chief_of_staff",
            "workflow": [],
            "intent": "route_request",
            "requires_approved_context": False,
            "requires_live_search": False,
            "side_effect_policy": "draft_or_read_only",
        }
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert len(fallback.workflow) > 1
    assert merged.workflow == []
    assert merged.requires_durable_state is True
    assert merged.requires_live_search is False
    assert merged.requires_approved_context is False
    assert merged.ask_shape.output_constraints.required_sections == []
    assert merged.ask_shape.output_constraints.require_section_headings is False


def test_natural_multi_part_deliverables_do_not_become_literal_heading_contracts() -> None:
    request = (
        "Give me one decision brief covering what is supported and the first "
        "validation question, and a short internal Slack note I can paste to the team."
    )

    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    assert plan.ask_shape.output_constraints.required_sections == []
    assert plan.ask_shape.output_constraints.require_section_headings is False
    assert plan.intent == "route_request"


def test_short_human_cos_note_request_stays_direct_and_provider_free() -> None:
    request = (
        "CoS: From this note only, Northstar Care sells referral-navigation software "
        "and has no audited outcomes. Give me one supported fact and the first "
        "validation question. No search or writes."
    )

    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    assert plan.target_agent == "chief_of_staff"
    assert plan.workflow == []
    assert plan.intent == "route_request"
    assert plan.requires_live_search is False
    assert plan.requires_approved_context is False
    assert plan.side_effect_policy == "draft_or_read_only"
    assert plan.ask_shape.prior_context_dependency == "selected_context"
    assert looks_like_supplied_context_synthesis_request(request) is True


def test_short_human_cos_stateful_review_accepts_llm_direct_durable_plan() -> None:
    request = (
        "CoS: Track this review. Northstar Care sells referral-navigation software "
        "but has no audited outcomes. Assess what is supported, choose the first "
        "validation gap, and give me a paste-ready internal Slack recommendation. "
        "No search or external actions."
    )

    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    assert plan.target_agent == "chief_of_staff"
    assert plan.workflow == [
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
    ]
    assert plan.intent == "route_request"
    assert plan.requires_live_search is False
    assert plan.requires_approved_context is False
    assert plan.ask_shape.prior_context_dependency == "selected_context"
    assert looks_like_supplied_context_synthesis_request(request) is True
    assert looks_like_stateful_work_request(request) is True
    assert plan.ask_shape.output_constraints.required_sections == []
    assert plan.ask_shape.output_constraints.require_section_headings is False
    candidate = plan.model_copy(
        update={
            "source": "llm",
            "workflow": [],
            "intent": "route_request",
            "requires_live_search": False,
            "requires_approved_context": False,
            "side_effect_policy": "draft_or_read_only",
        }
    )

    merged = merge_manual_request_plan(plan, candidate)

    assert merged.workflow == []
    assert merged.requires_durable_state is True


def test_no_search_question_without_asserted_facts_is_not_selected_context() -> None:
    request = (
        "CoS: Assess whether Northstar Care sells referral-navigation software. "
        "No search or external actions."
    )

    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    assert plan.ask_shape.prior_context_dependency == "unspecified"
    assert looks_like_supplied_context_synthesis_request(request) is False


@pytest.mark.parametrize(
    ("request_text", "expected_agent"),
    [
        (
            "Find the latest Zotero paper about clinical AI and summarize it.",
            "business_research_analyst",
        ),
        ("What recent RSS announcements matter to Keystone?", "rss_context_agent"),
        ("Find three recent psychiatry AI preprints worth reading.", "preprints_context_agent"),
        (
            "Act as my chief of staff and tell me what needs my attention today.",
            "chief_of_staff",
        ),
    ],
)
def test_natural_human_context_and_manager_asks_keep_expected_owner(
    request_text: str,
    expected_agent: str,
) -> None:
    plan = infer_manual_request_plan(request_text, requested_agent="orchestrator")

    assert plan.target_agent == expected_agent
    assert plan.intent != "clarification"


def test_natural_three_company_comparison_routes_to_business_research() -> None:
    plan = infer_manual_request_plan(
        "Compare NeuroFlow, Headway, and Spring Health and explain the important differences.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "company_research"
    assert plan.primary_target == "NeuroFlow vs Headway vs Spring Health"


def test_natural_multistep_opportunity_workflow_owns_airtable_context_handoff() -> None:
    plan = infer_manual_request_plan(
        "Find the best opportunity, research it, create an Airtable record plan, and draft "
        "outreach for review without sending.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_to_outreach_loop"
    assert plan.primary_target == "best opportunity"
    assert plan.workflow == [
        "opportunity_scout",
        "business_research_analyst",
        "airtable_context_agent",
        "outreach_composer",
    ]
    assert plan.requires_durable_state is True
    assert plan.provider_system == "airtable"
    assert plan.provider_operations == []
    assert plan.expected_artifact_type == "business_system_write_plan"
    assert plan.desired_count == 1
    assert plan.desired_count_explicit is False
    assert plan.ask_shape.permission_state == "draft_only"


def test_gmail_execution_plan_keeps_inline_email_context_off_live_gmail() -> None:
    plan = infer_gmail_execution_plan(
        "Use only this inline, non-sensitive email context: Subject: Partnership follow-up "
        "for remote patient monitoring validation. Return a human-useful Gmail triage "
        "answer: priority, why it matters, whether a reply is needed, suggested next action, "
        "and any caveats. Do not access Gmail live."
    )

    assert plan.operation == "single_message_triage"
    assert plan.live_read_required is False
    assert plan.source_label == "inline_context"
    assert plan.gmail_query == ""
    assert "inline_email_context_triage" in plan.candidate_helpers


def test_gmail_execution_plan_distinguishes_latest_thread_from_latest_email() -> None:
    thread_plan = infer_gmail_execution_plan(
        "Read the latest Gmail thread from the configured sender and suggest a reply."
    )
    email_plan = infer_gmail_execution_plan(
        "Read the latest Gmail email from the configured sender and suggest a reply."
    )

    assert thread_plan.read_scope == "thread"
    assert email_plan.read_scope == "message"


@pytest.mark.parametrize(
    ("ask", "expected_subject"),
    [
        (
            'Find the Gmail email with subject "Why Healthtech Needs a New Kind of '
            'Product Leader." Read the complete thread and suggest a reply.',
            "Why Healthtech Needs a New Kind of Product Leader.",
        ),
        (
            "Find the Gmail email with subject “Why Healthtech Needs a New Kind of "
            "Product Leader.” Read the complete thread and suggest a reply.",
            "Why Healthtech Needs a New Kind of Product Leader.",
        ),
        (
            "Find the email titled: Why Healthtech Needs a New Kind of Product Leader. "
            "Read the complete thread and suggest a reply.",
            "Why Healthtech Needs a New Kind of Product Leader",
        ),
    ],
)
def test_gmail_execution_plan_preserves_exact_subject_without_default_recency(
    ask: str,
    expected_subject: str,
) -> None:
    plan = infer_gmail_execution_plan(
        f"{ask} Return draft text here only. Do not create a Gmail draft or send."
    )

    assert plan.operation == "draft_reply"
    assert plan.gmail_query == f'subject:"{expected_subject}"'
    assert "newer_than:" not in plan.gmail_query
    assert plan.max_messages == 1
    assert plan.live_read_required is True
    assert plan.create_gmail_drafts is False
    assert plan.draft_replies_in_output is True
    assert plan.artifact_policy == "draft_text_in_output"


def test_gmail_execution_plan_combines_explicit_date_scope_with_exact_subject() -> None:
    plan = infer_gmail_execution_plan(
        'Find the email titled "Why Healthtech Needs a New Kind of Product Leader" '
        "from the last 30 days and suggest a response here only."
    )

    assert plan.gmail_query == (
        'newer_than:30d subject:"Why Healthtech Needs a New Kind of Product Leader"'
    )


def test_outreach_execution_plan_keeps_drafts_gated_and_tracks_replies() -> None:
    plan = infer_outreach_execution_plan(
        "draft a concise follow-up email for a source-backed Keystone opportunity, and "
        "include a plan for how future replies should be tracked or summarized. Do not send."
    )

    assert plan.operation == "draft_follow_up"
    assert plan.approved_context_required is True
    assert plan.use_default_approved_fixture_for_backend_test is True
    assert plan.include_follow_up_schedule is True
    assert plan.include_reply_tracking_plan is True
    assert plan.side_effect_policy == "draft_only_never_send"


def test_outreach_execution_plan_accepts_approved_inline_context_labels() -> None:
    plan = infer_outreach_execution_plan(
        "outreach composer agent: diagnostic case diag_outreach flexible labels. "
        "Prepare a draft-only email paragraph. Target contact: Alex Rivera at "
        "Example Health. Approved evidence: Example Health asked whether Keystone "
        "could review its remote patient monitoring AI validation workflow. "
        "Do not send email or create a Gmail draft."
    )

    assert plan.approved_inline_context_available is True
    assert plan.approved_context_required is True
    assert plan.use_default_approved_fixture_for_backend_test is False


def test_outreach_curly_unsent_request_is_draft_only_and_extracts_company() -> None:
    request = (
        "I want a thoughtful first touch to the person who currently leads clinical "
        "partnerships at Fort Health. Please verify the best public contact or role, "
        "then draft a concise email and LinkedIn note. Keep both drafts unsent, and "
        "don’t create a Gmail draft or contact record."
    )

    plan = infer_manual_request_plan(request, requested_agent="outreach_composer")

    assert plan.target_agent == "outreach_composer"
    assert plan.intent == "outreach_draft"
    assert plan.task_objective == "outreach_draft"
    assert plan.ask_shape.permission_state == "draft_only"
    assert plan.primary_target == "Fort Health"
    assert not any("Send request blocked" in warning for warning in plan.planner_warnings)


def test_cli_live_gmail_priority_grouping_uses_agent_execution_plan(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    captured: dict[str, list[str]] = {}
    database_url = f"sqlite:///{tmp_path / 'gmail-priority.db'}"

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "output_type": "GmailPriorityGroupingResult",
                    "output": {"buckets": {}},
                    "send_enabled": False,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _offline_orchestrator_preflight)

    assert (
        cli.main(
            [
                "ask",
                "--agent",
                "gmail_triage",
                "--live-sdk",
                "--database-url",
                database_url,
                "--json",
                (
                    "review recent Gmail threads from the last 7 days related to "
                    "Keystone opportunities"
                ),
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["selected_agent"] == "gmail_triage"
    assert output["agent_execution_plan"]["operation"] == "priority_grouping"
    assert "--live-gmail" in captured["command"]
    assert "--priority-grouping" in captured["command"]
    assert captured["command"][captured["command"].index("--lookback-days") + 1] == "7"
    assert captured["command"][captured["command"].index("--gmail-query") + 1] == "newer_than:7d"


@pytest.mark.parametrize(
    ("request_text", "expected_query"),
    [
        (
            "Review Gmail threads from the last 7 days related to Keystone opportunities.",
            "newer_than:7d",
        ),
        (
            "Triage messages from the past 14 days about possible partnerships.",
            "newer_than:14d",
        ),
        (
            "Summarize email conversations from today connected to current opportunities.",
            None,
        ),
    ],
)
def test_gmail_time_window_prose_is_not_misread_as_a_sender(
    request_text: str,
    expected_query: str | None,
) -> None:
    plan = infer_manual_request_plan(request_text, requested_agent="gmail_triage")

    if expected_query is None:
        assert plan.gmail_query.startswith("after:")
        assert '"today connected"' not in plan.gmail_query
    else:
        assert plan.gmail_query == expected_query


def test_cli_live_outreach_backend_fixture_request_stops_at_preflight(monkeypatch, capsys) -> None:
    captured: dict[str, list[str]] = {}

    def fake_preflight(request_text: str, **_kwargs: object):
        plan = infer_manual_request_plan(
            request_text,
            requested_agent="outreach_composer",
        ).model_copy(update={"source": "llm"})
        return cli.OrchestratorPreflight(
            request_text=request_text,
            requested_agent="outreach_composer",
            advisory_only=True,
            selected_agent="outreach_composer",
            blocked_by_orchestrator=True,
            execution_allowed=False,
            block_kind="approval",
            block_reason="Outreach drafting requires approved context.",
            manual_request_plan=plan,
            route_result=cli.route_request(request_text, manual_plan=plan),
            sdk_usage_events=[{"usage": {"requests": 1}}],
        )

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "output_type": "OutreachDraft",
                    "output": {"company_name": "Curebase", "send_enabled": False},
                    "send_enabled": False,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight)

    assert (
        cli.main(
            [
                "ask",
                "--agent",
                "outreach_composer",
                "--live-sdk",
                "--json",
                (
                    "draft a concise follow-up email for a source-backed Keystone opportunity, "
                    "and include a plan for how future replies should be tracked or summarized. "
                    "Do not send."
                ),
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["manual_request_plan"]["target_agent"] == "outreach_composer"
    assert output["status"] == "blocked"
    assert output["requires_approved_context"] is True
    assert output["send_enabled"] is False
    assert captured == {}


def test_internal_ai_agents_workflow_message_mutation_routes_to_chief() -> None:
    plan = infer_manual_request_plan(
        "Post a marked test update in ai-agents-workflow, edit the same message, then delete it.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "chief_of_staff"


def test_external_outreach_message_still_routes_to_outreach() -> None:
    plan = infer_manual_request_plan(
        "Draft an outreach message to the NeuroFlow partnerships lead for review.",
        requested_agent="orchestrator",
    )

    assert plan.target_agent == "outreach_composer"


def test_chief_multi_source_read_compiles_context_owners_without_research_route() -> None:
    request = (
        "Act as my chief of staff: review today's Gmail, open WorkItems, and current "
        "Airtable context, then recommend my top three actions. Don't change anything."
    )

    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    assert plan.requested_agent == "chief_of_staff"
    assert plan.target_agent == "chief_of_staff"
    assert plan.workflow == ["gmail_triage", "airtable_context_agent"]
    assert "business_research_analyst" not in plan.workflow
    assert "opportunity_scout" not in plan.workflow
    assert plan.intent == "context_lookup"
    assert plan.task_objective == "context_lookup"
    assert plan.expected_artifact_type == "context_summary"
    assert plan.target_type == "business_system_context"
    assert plan.provider_operations == ["read"]
    assert plan.gmail_date_scope == "today"
    assert plan.ask_shape.permission_state == "read_only"
    assert plan.ask_shape.source_type_preference == [
        "gmail",
        "airtable",
        "work_items",
    ]
    assert plan.requires_durable_state is True


def test_chief_read_only_work_item_inventory_requires_receipt_state() -> None:
    request = (
        "Chief of Staff, among all active, non-archived WorkItems, identify the one "
        "that has waited longest without a new event, report its age, current route, "
        "blocker or next action, and count pending approvals. Read-only; do not "
        "continue, approve, or modify anything."
    )

    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "continue_work_item"
    assert plan.requires_durable_state is True
    assert plan.ask_shape.permission_state == "read_only"


def test_chief_work_item_inventory_preserves_candidate_count_and_owner() -> None:
    request = (
        "Chief of Staff, among the three most recently updated blocked, non-archived "
        "WorkItems, show each candidate's current route and last verified stage, choose "
        "the one with the clearest safe next action, and report pending approvals. "
        "Do not continue, approve, or change anything."
    )
    fallback = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="clarification",
        intent="clarification",
        objective=request,
        missing_required_information=["exact WorkItem id"],
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert fallback.desired_count == 3
    assert fallback.desired_count_explicit is True
    assert merged.requested_agent == "chief_of_staff"
    assert merged.target_agent == "chief_of_staff"
    assert merged.intent == "continue_work_item"
    assert merged.task_objective == "context_lookup"
    assert merged.expected_artifact_type == "context_summary"
    assert merged.desired_count == 3
    assert merged.ask_shape.permission_state == "read_only"
    assert merged.requires_durable_state is True


def test_pending_approval_inventory_does_not_grant_approval_authority() -> None:
    request = (
        "Chief of Staff, inspect blocked WorkItems, report whether each has a pending "
        "approval, and count the pending approvals. Read-only; do not continue, "
        "approve, or modify anything."
    )

    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    assert plan.intent == "continue_work_item"
    assert plan.requires_durable_state is True
    assert plan.ask_shape.permission_state == "read_only"


@pytest.mark.parametrize(
    ("request_text", "expected_target"),
    [
        (
            "Business Research Analyst, start with Exa and assess Brightline's "
            "pediatric behavioral-health offering. Verify one outcome claim and "
            "one payer signal from public sources. Read-only; no outreach or saved "
            "artifacts.",
            "Brightline",
        ),
        (
            "Business Research Analyst, begin with Tavily and evaluate Hazel Health's "
            "school-based behavioral-health offering. Show me the public evidence. "
            "Read-only; do not save anything.",
            "Hazel Health",
        ),
        (
            "Could you use OpenAI web search to take a look at Lyra Health's employer "
            "offering and tell me what the evidence supports? Read-only.",
            "Lyra Health",
        ),
        (
            "Please try Exa first for Spring Health's payer partnerships and flag "
            "anything that is only a company claim. Keep this read-only and don't "
            "save anything.",
            "Spring Health",
        ),
    ],
)
def test_provider_first_company_research_keeps_possessive_company_target(
    request_text: str,
    expected_target: str,
) -> None:
    plan = infer_manual_request_plan(
        request_text,
        requested_agent="business_research_analyst",
    )

    assert plan.intent == "company_research"
    assert plan.primary_target == expected_target
    assert plan.requires_live_search is True
    assert plan.ask_shape.permission_state == "read_only"


def test_llm_context_lookup_cannot_drop_read_only_work_item_receipts() -> None:
    request = (
        "Chief of Staff, inspect active WorkItems and pending approvals. Read-only; "
        "do not continue, approve, or modify anything."
    )
    fallback = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    candidate = fallback.model_copy(
        deep=True,
        update={
            "source": "llm",
            "intent": "route_request",
            "task_objective": "context_lookup",
            "expected_artifact_type": "context_summary",
            "target_type": "business_system_context",
            "requires_durable_state": False,
        },
    )

    merged = merge_manual_request_plan(
        fallback,
        candidate,
        allow_contextual_delegation=True,
    )

    assert merged.target_agent == "chief_of_staff"
    assert merged.intent == "continue_work_item"
    assert merged.task_objective == "context_lookup"
    assert merged.expected_artifact_type == "context_summary"
    assert merged.requires_durable_state is True
    assert merged.ask_shape.permission_state == "read_only"
    assert any(
        "read-only WorkItem inspection contract" in warning
        for warning in merged.planner_warnings
    )


def test_llm_cannot_collapse_chief_multi_source_read_to_one_provider() -> None:
    request = (
        "CoS, review today's Gmail, open WorkItems, and current Airtable context, "
        "then recommend my top three actions. Don't change anything."
    )
    fallback = infer_manual_request_plan(
        request,
        requested_agent="chief_of_staff",
    )
    candidate = fallback.model_copy(
        deep=True,
        update={
            "source": "llm",
            "requested_agent": "airtable_context_agent",
            "target_agent": "airtable_context_agent",
            "workflow": [],
            "provider_system": "airtable",
            "provider_action_steps": [
                ManualProviderActionStep(
                    operation="read",
                    resource_type="gmail_message",
                ),
                ManualProviderActionStep(
                    operation="read",
                    resource_type="airtable_record",
                ),
            ],
            "requires_durable_state": False,
            "rationale": (
                "Airtable is the owner; Gmail and WorkItems are context sources."
            ),
        },
    )

    merged = merge_manual_request_plan(
        fallback,
        candidate,
        allow_contextual_delegation=True,
    )

    assert merged.requested_agent == "chief_of_staff"
    assert merged.target_agent == "chief_of_staff"
    assert merged.workflow == ["gmail_triage", "airtable_context_agent"]
    assert merged.provider_system == "unspecified"
    assert merged.provider_operations == ["read"]
    assert merged.provider_action_steps == []
    assert merged.gmail_mailbox_direction == "inbound"
    assert merged.gmail_date_scope == "today"
    assert merged.requires_durable_state is True
    assert merged.requires_approved_context is False
    assert merged.ask_shape.permission_state == "read_only"
    assert merged.ask_shape.source_type_preference == [
        "gmail",
        "airtable",
        "work_items",
    ]
    assert any(
        "Chief-owned multi-source read contract" in warning
        for warning in merged.planner_warnings
    )


def test_chief_multi_source_repair_preserves_typed_base_requirements() -> None:
    request = (
        "CoS, review today's Gmail, open WorkItems, and current Airtable context, "
        "then recommend my top three actions. Don't change anything."
    )
    fallback = infer_manual_request_plan(
        request,
        requested_agent="chief_of_staff",
    ).model_copy(
        update={
            "provider_action_steps": [
                ManualProviderActionStep(
                    operation="read",
                    resource_type="gmail_message",
                ),
                ManualProviderActionStep(
                    operation="read",
                    resource_type="airtable_record",
                ),
            ],
            "requires_approved_context": True,
        }
    )
    candidate = fallback.model_copy(
        update={
            "source": "llm",
            "target_agent": "airtable_context_agent",
            "workflow": [],
            "provider_system": "airtable",
            "provider_action_steps": [],
            "requires_approved_context": False,
            "requires_durable_state": False,
        }
    )

    merged = merge_manual_request_plan(
        fallback,
        candidate,
        allow_contextual_delegation=True,
    )

    assert merged.provider_action_steps == fallback.provider_action_steps
    assert merged.requires_approved_context is True


def test_explicit_zotero_read_retains_context_owner_and_typed_read_shape() -> None:
    request = (
        "@KNI Zotero Context Agent, read the abstract of the most recently added "
        "journal article in my library and tell me the one finding that matters "
        "most for KNI. Don't change anything."
    )

    plan = infer_manual_request_plan(request)

    assert plan.requested_agent == "zotero_context_agent"
    assert plan.target_agent == "zotero_context_agent"
    assert plan.workflow == []
    assert plan.intent == "context_lookup"
    assert plan.provider_system == "zotero"
    assert plan.provider_operations == ["read"]
    assert plan.provider_selection_order == "latest"
    assert plan.zotero_requested_fields == ["abstract"]
    assert plan.ask_shape.permission_state == "read_only"
    assert plan.requires_live_search is False
    assert plan.side_effect_policy == "draft_or_read_only"


def test_adapter_stripped_zotero_read_retains_captured_context_owner() -> None:
    request = (
        "read the abstract of the most recently added journal article in my "
        "library and tell me the one finding that matters most for KNI. "
        "Don't change anything."
    )

    plan = infer_manual_request_plan(
        request,
        requested_agent="zotero_context_agent",
    )

    assert plan.requested_agent == "zotero_context_agent"
    assert plan.target_agent == "zotero_context_agent"
    assert plan.provider_system == "zotero"
    assert plan.provider_operations == ["read"]
    assert plan.provider_selection_order == "latest"
    assert plan.zotero_requested_fields == ["abstract"]
    assert plan.requires_live_search is False


@pytest.mark.parametrize(
    ("request_text", "expected_rank", "expected_fields"),
    [
        (
            "Use Zotero to select the second-most-recent journal article and "
            "give me its title and authors only. Don't change anything.",
            2,
            ["title", "authors"],
        ),
        (
            "Use Zotero to select the third newest journal article and return "
            "its title only. Don't change anything.",
            3,
            ["title"],
        ),
        (
            "Use Zotero to select the item before the latest journal article and "
            "return its title only. Don't change anything.",
            2,
            ["title"],
        ),
    ],
)
def test_zotero_ordinal_selection_keeps_typed_provider_ownership(
    request_text: str,
    expected_rank: int,
    expected_fields: list[str],
) -> None:
    plan = infer_manual_request_plan(request_text)

    assert plan.target_agent == "zotero_context_agent"
    assert plan.workflow == []
    assert plan.intent == "context_lookup"
    assert plan.provider_system == "zotero"
    assert plan.provider_operations == ["read"]
    assert plan.target_type == "zotero_article"
    assert plan.provider_read_scope == "bounded_collection"
    assert plan.provider_result_mode == "items"
    assert plan.provider_selection_order == "latest"
    assert plan.provider_selection_rank == expected_rank
    assert plan.zotero_requested_fields == expected_fields
    assert plan.ask_shape.permission_state == "read_only"
    assert plan.requires_live_search is False


def test_zotero_ordinal_followup_uses_verified_provider_affinity_not_web_search() -> None:
    followup = (
        "No, use the second-most-recent journal article instead; give me its "
        "title and authors only. Don't change anything."
    )
    plan = resolve_manual_request_plan(
        followup,
        live=False,
        workflow_state={
            "execution_continuation": {
                "provider_affinity": "zotero",
                "prior_agent": "zotero_context_agent",
                "prior_request": (
                    "Use Zotero to select the latest journal article and return "
                    "its title."
                ),
            }
        },
    )

    assert plan.target_agent == "zotero_context_agent"
    assert plan.provider_system == "zotero"
    assert plan.provider_operations == ["read"]
    assert plan.target_type == "zotero_article"
    assert plan.provider_selection_order == "latest"
    assert plan.provider_selection_rank == 2
    assert plan.zotero_requested_fields == ["title", "authors"]
    assert plan.requires_live_search is False


def test_zotero_ordinal_followup_derives_affinity_from_verified_owner_scope() -> None:
    followup = (
        "Actually, not the newest one-the second-most-recent journal article. "
        "Give me its title and authors only. Don't change anything."
    )
    plan = resolve_manual_request_plan(
        followup,
        live=False,
        workflow_state={
            "execution_continuation": {
                "provider_affinity": "",
                "prior_agent": "zotero_context_agent",
                "prior_request": (
                    "Use Zotero to select the latest journal article and return "
                    "its title."
                ),
            },
            "prior_provider_result_scope": ManualProviderResultSetScope(
                source_run_id="run-zotero-latest",
                provider_system="zotero",
                provider_read_scope="bounded_collection",
                target_type="zotero_article",
                item_count=1,
                complete=True,
                verified=True,
            ).model_dump(mode="json"),
        },
    )

    assert plan.target_agent == "zotero_context_agent"
    assert plan.provider_system == "zotero"
    assert plan.provider_operations == ["read"]
    assert plan.provider_selection_order == "latest"
    assert plan.provider_selection_rank == 2
    assert plan.zotero_requested_fields == ["title", "authors"]
    assert plan.requires_live_search is False


@pytest.mark.parametrize(
    ("provider_system", "prior_owner", "followup", "expected_owner"),
    [
        (
            "airtable",
            "airtable_context_agent",
            "Which amount was recorded?",
            "airtable_context_agent",
        ),
        (
            "gmail",
            "gmail_triage",
            "What does the sender need?",
            "gmail_triage",
        ),
        (
            "google_workspace",
            "google_workspace_context_agent",
            "What is the document title?",
            "google_workspace_context_agent",
        ),
    ],
)
def test_verified_provider_scope_and_matching_owner_restore_typed_affinity(
    provider_system: str,
    prior_owner: str,
    followup: str,
    expected_owner: str,
) -> None:
    plan = resolve_manual_request_plan(
        followup,
        live=False,
        workflow_state={
            "execution_continuation": {
                "provider_affinity": "",
                "prior_agent": prior_owner,
                "prior_request": "Prior provider-owned request",
            },
            "prior_provider_result_scope": ManualProviderResultSetScope(
                source_run_id=f"run-{provider_system}",
                provider_system=provider_system,
                provider_read_scope="bounded_collection",
                target_type="business_system_context",
                item_count=1,
                complete=True,
                verified=True,
            ).model_dump(mode="json"),
        },
    )

    assert plan.target_agent == expected_owner
    assert plan.provider_system == provider_system
    assert plan.workflow == []


@pytest.mark.parametrize(
    ("scope_updates", "prior_owner"),
    [
        ({"verified": False}, "zotero_context_agent"),
        ({"complete": False}, "zotero_context_agent"),
        ({}, "gmail_triage"),
    ],
)
def test_provider_affinity_is_not_derived_from_untrusted_or_mismatched_evidence(
    scope_updates: dict[str, bool],
    prior_owner: str,
) -> None:
    scope = ManualProviderResultSetScope(
        source_run_id="run-zotero-latest",
        provider_system="zotero",
        provider_read_scope="bounded_collection",
        target_type="zotero_article",
        item_count=1,
        complete=True,
        verified=True,
    ).model_copy(update=scope_updates)
    plan = resolve_manual_request_plan(
        "Use the second-most-recent one instead.",
        live=False,
        workflow_state={
            "execution_continuation": {
                "provider_affinity": "",
                "prior_agent": prior_owner,
                "prior_request": "Prior provider-owned request",
            },
            "prior_provider_result_scope": scope.model_dump(mode="json"),
        },
    )

    assert plan.provider_selection_rank is None
    assert plan.zotero_requested_fields == []


def test_explicit_current_provider_switch_overrides_verified_prior_affinity() -> None:
    plan = resolve_manual_request_plan(
        "Gmail Triage, summarize the latest email without drafting or changing anything.",
        live=False,
        workflow_state={
            "execution_continuation": {
                "provider_affinity": "",
                "prior_agent": "zotero_context_agent",
                "prior_request": "Use Zotero to select the latest journal article.",
            },
            "prior_provider_result_scope": ManualProviderResultSetScope(
                source_run_id="run-zotero-latest",
                provider_system="zotero",
                provider_read_scope="bounded_collection",
                target_type="zotero_article",
                item_count=1,
                complete=True,
                verified=True,
            ).model_dump(mode="json"),
        },
    )

    assert plan.target_agent == "gmail_triage"
    assert plan.provider_system == "gmail"
    assert plan.provider_system != "zotero"


def test_zotero_selection_rank_is_bounded_in_the_schema() -> None:
    with pytest.raises(ValueError, match="less than or equal to 10"):
        ManualRequestPlan(
            source="llm",
            target_agent="zotero_context_agent",
            intent="context_lookup",
            provider_system="zotero",
            provider_operations=["read"],
            provider_selection_order="latest",
            provider_selection_rank=11,
        )


def test_explicit_zotero_latest_abstract_reserves_bounded_recovery() -> None:
    request = (
        "@KNI Zotero Context Agent, read the abstract of the most recently added "
        "journal article in my library and tell me the one finding that matters "
        "most for KNI. Don't change anything."
    )
    plan = infer_manual_request_plan(request)
    args = SimpleNamespace(
        context_file="",
        agent="zotero_context_agent",
        max_manager_steps=4,
        live_search=False,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
        requested_route="zotero_context_agent",
        manual_plan=plan,
        effective_live_search=False,
    )

    assert estimate["min"] == 3
    assert estimate["max"] == 5
    assert estimate["stages"] == [
        "manual_request_planner",
        "orchestrator_preflight",
        "zotero_context_agent_direct_sdk",
        "conditional_zotero_context_agent_bounded_recovery",
    ]


def test_explicit_zotero_agent_can_delegate_clear_web_research() -> None:
    plan = infer_manual_request_plan(
        "research NeuroFlow as a company using public web sources and return a "
        "source-backed company profile.",
        requested_agent="zotero_context_agent",
    )

    assert plan.requested_agent == "zotero_context_agent"
    assert plan.target_agent == "business_research_analyst"
    assert plan.intent in {"company_research", "research_brief"}
    assert plan.provider_system == "unspecified"
    assert plan.provider_operations == []
    assert plan.requires_live_search is True


def test_live_planner_cannot_erase_read_only_work_item_inspection_contract() -> None:
    request = (
        "Hey Chief of Staff, review the three most recently updated blocked WorkItems, "
        "show each owner and last verified stage, and tell me whether an approval is "
        "waiting. Keep this read-only; do not continue, rerun, approve, or change anything."
    )
    fallback = infer_manual_request_plan(
        request,
        requested_agent="chief_of_staff",
    )
    generic_live_candidate = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="route_request",
        provider_system="unspecified",
        provider_operations=["read"],
        requires_durable_state=False,
        ask_shape={"permission_state": "read_only"},
        objective=request,
    )

    merged = merge_manual_request_plan(fallback, generic_live_candidate)

    assert merged.intent == "continue_work_item"
    assert merged.requires_durable_state is True
    assert merged.provider_operations == []
    assert merged.ask_shape.permission_state == "read_only"


@pytest.mark.parametrize(
    "prompt_text",
    [
        (
            "I have two possible stages: a market scan and, much later, a possible "
            "introduction. No company, recipient, or approved claims has been selected. "
            "Before any work begins, give me only the safe sequence and the first "
            "decision I need to make. Do not research, hand off, save, or draft anything."
        ),
        (
            "I need an evidence-backed vendor landscape and, after review, perhaps a "
            "partnership note. I do not know which specialist should own the first step, "
            "and there is no selected company or recipient. Give me the safe order and "
            "the first clarification you need. Do not run tools, create artifacts, or draft."
        ),
    ],
)
def test_nonexecuting_ambiguous_workflow_design_stays_with_clarification(
    prompt_text: str,
) -> None:
    plan = infer_manual_request_plan(prompt_text, requested_agent="orchestrator")

    assert plan.target_agent == "clarification"
    assert plan.intent == "clarification"
    assert plan.workflow == []
    assert plan.requires_live_search is False
    assert plan.requires_durable_state is False
    assert plan.provider_operations == []
