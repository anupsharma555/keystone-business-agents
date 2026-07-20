from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
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
    infer_gmail_execution_plan,
    resolve_gmail_execution_plan,
)
from keystone_agents.manual_request import (
    infer_manual_request_plan,
    is_single_owner_gmail_reply_request,
    looks_like_stateful_work_request,
    looks_like_supplied_context_synthesis_request,
    merge_manual_request_plan,
    positive_capability_text,
    resolve_manual_request_owner,
)
from keystone_agents.outreach_composer.execution_plan import infer_outreach_execution_plan
from keystone_agents.schemas.approval import ApprovalState
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.output_constraints import InterpretedOutputConstraints
from keystone_agents.test_pack_specs import get_test_pack_spec


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
        target_type="gmail_thread",
        gmail_query="in:inbox",
        desired_count=1,
    )

    plan = resolve_gmail_execution_plan(
        request_text,
        manual_plan=manual_plan,
    )

    assert plan.source == "llm_manual_plan"
    assert plan.operation == "thread_summary"
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

    assert natural.workflow == explicit.workflow == [
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
    ]


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


def test_llm_workflow_cannot_drop_explicit_goal_deliverables_or_execute_cos() -> None:
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
    assert merged.workflow == [
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
    ]


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
        "Provide the exact title and summarize the stored abstract in no more than "
        "50 words.",
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
    candidate.ask_shape.output_constraints = (
        candidate.ask_shape.output_constraints.model_copy(
            update={
                "interpretation": "maximum 20-word answer because the operator used it as a cap",
                "word_count_mode": "maximum",
            }
        )
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.ask_shape.output_constraints.word_count == 20
    assert merged.ask_shape.output_constraints.word_count_mode == "maximum"
    assert "because the operator" in merged.ask_shape.output_constraints.interpretation


def test_manual_planner_prompt_preserves_distinct_named_deliverables() -> None:
    prompt = Path("src/keystone_agents/prompts/manual_request_planner.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(prompt.split())

    assert "two or more distinct named deliverables" in normalized
    assert "decision brief plus a paste-ready internal note" in normalized
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
                {"id": f"run-{index}", "route": "chief_of_staff"}
                for index in range(7)
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

    context = _compact_manual_planner_context(
        {"slack_thread_transcript": transcript}
    )
    bounded = context["slack_thread_transcript"]

    assert len(bounded) <= 6000
    assert bounded.startswith("ROOT: create the first bounded object.")
    assert bounded.endswith("LATEST: actually use the Gmail draft and do not send it.")
    assert "middle of Slack thread omitted" in bounded
    assert context["context_compaction"]["transcript_compacted"] is True
    assert context["context_compaction"]["transcript_original_chars"] == len(transcript)


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
    assert (
        context["current_work_item"]["selected_artifacts"][0]["artifact_id"]
        == "draftExact123"
    )
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
    assert plan.workflow == []
    assert not any(
        "did not contain enough information" in item
        for item in plan.planner_warnings
    )


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
                    "summary": (
                        "CoS, using only these facts, give me exactly three bullets."
                    ),
                },
                {
                    "role": "agent",
                    "source_agent": "kni",
                    "summary": (
                        "Business Research Analyst article summary from a stale route."
                    ),
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
            "recent_slack_thread": [
                {"summary": "Airtable record created and provider verified."}
            ],
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
                "thread_messages": [
                    {"text": "One exact Zotero article was resolved."}
                ],
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
    assert plan.target_type == "opportunity"
    assert plan.task_objective == "opportunity_discovery"
    assert plan.expected_artifact_type == "opportunity_record"
    assert plan.primary_target == "behavioral health AI"
    assert plan.desired_count == 1
    assert plan.requires_live_search is True
    assert "behavioral health" in plan.constraints


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

    assert merged.desired_count == 1
    assert merged.ask_shape.output_constraints.minimum_items is None
    assert merged.ask_shape.output_constraints.maximum_items is None
    assert merged.ask_shape.output_constraints.item_count_mode == "unspecified"
    assert merged.ask_shape.output_constraints.style_requirements == [
        "exact fields only"
    ]
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
            "Return a brief reply suggestion based on the selected thread without "
            "modifying Gmail."
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
        'Give me exactly two bullets. Do not use the phrase "workflow metadata" '
        "in the answer."
    )
    fallback = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    candidate = fallback.model_copy(deep=True)
    candidate.source = "llm"
    candidate.ask_shape.output_constraints = (
        candidate.ask_shape.output_constraints.model_copy(
            update={
                "forbidden_phrases": ["workflow metadata", "routing"],
                "style_requirements": ["concise"],
            }
        )
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.ask_shape.output_constraints.forbidden_phrases == [
        "workflow metadata"
    ]
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
        (
            "Reformat the answer above as three bullets only. Don't call tools or "
            "create a draft."
        ),
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
    assert any(
        "Removed outreach drafting" in warning
        for warning in merged.planner_warnings
    )


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
    ("request_text", "requested_agent", "expected_owner", "expected_workflow"),
    [
        (
            "Without provider tools, use only these facts and return two bullets. "
            "Do not draft outreach or modify records.",
            None,
            "chief_of_staff",
            [],
        ),
        (
            "Research NeuroFlow from the attached approved notes only. Do not search "
            "the web, draft outreach, or modify provider records.",
            None,
            "business_research_analyst",
            [],
        ),
        (
            "Identify the strongest collaboration opportunity in these supplied notes. "
            "Do not search the web, draft outreach, or create CRM records.",
            None,
            "opportunity_scout",
            [],
        ),
        (
            "Read the latest Gmail thread from the configured sender and summarize it. "
            "Do not draft a reply, change labels, or send anything.",
            None,
            "gmail_triage",
            [],
        ),
        (
            "CoS, using the supplied company packet, review what is known and unknown, "
            "identify the highest-value opportunity and validation gap, then prepare "
            "a concise internal Slack brief for review. Do not search the web, create "
            "provider records, send email, or post.",
            "chief_of_staff",
            "chief_of_staff",
            [
                "business_research_analyst",
                "opportunity_scout",
                "outreach_composer",
            ],
        ),
    ],
)
def test_constraint_pruning_never_blocks_direct_specialist_or_graph_routes(
    request_text: str,
    requested_agent: str | None,
    expected_owner: str,
    expected_workflow: list[str],
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

    assert fallback.target_agent == expected_owner
    assert fallback.workflow == expected_workflow
    assert merged.target_agent == expected_owner
    assert merged.workflow == expected_workflow
    assert merged.intent != "clarification"
    assert merged.requires_live_search is False


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
            "Without searching, draft one internal Slack message from these "
            "approved facts.",
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
        (
            "Chief of Staff, read the Airtable financial tracker and summarize "
            "the current quarter."
        ),
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
        "structured intent/provider contract" in warning
        for warning in merged.planner_warnings
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


def test_live_manual_plan_owns_mailbox_scope_while_no_draft_safety_is_preserved() -> None:
    fallback = infer_manual_request_plan(
        "Summarize my top three unread emails from today and do not draft replies.",
        requested_agent="orchestrator",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="orchestrator",
        target_agent="gmail_triage",
        intent="gmail_triage",
        desired_count=8,
        gmail_query="newer_than:7d",
        lookback_days=7,
        draft_policy="draft_only_for_urgent",
        constraints=["model-added grouping suggestion"],
    )

    merged = merge_manual_request_plan(fallback, candidate)

    assert merged.target_agent == "gmail_triage"
    assert merged.desired_count == 8
    assert merged.lookback_days == 7
    assert merged.gmail_query == "newer_than:7d"
    assert merged.draft_policy == "no_drafts_requested"
    assert "create" not in merged.provider_operations
    assert "update" not in merged.provider_operations
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
        "slack_operations"
        if expected_intent == "slack_operations"
        else "route_or_continue"
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
    assert result.route == "clarification"
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
        "Find 2 U.S.-relevant academic institutes. Use live SDK and live search. "
        "No outreach."
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
    assert execution.max_messages == 10


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
    assert execution.gmail_query == (
        'subject:"Why Healthtech Needs a New Kind of Product Leader"'
    )
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


def test_llm_plan_preserves_only_grounded_multi_deliverable_sections() -> None:
    request = (
        "CoS, give me a decision brief and a separate paste-ready internal Slack "
        "note using only this supplied context."
    )
    fallback = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    candidate_constraints = (
        fallback.ask_shape.output_constraints.model_copy(
            update={
                "interpretation": (
                    "Return a decision brief and a separate paste-ready internal "
                    "Slack note."
                ),
                "required_sections": [
                    "Decision brief",
                    "Paste-ready internal Slack note",
                ],
            }
        )
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

    assert ungrounded_merged.ask_shape.output_constraints.required_sections == [
        "Decision brief",
        "Paste-ready internal Slack note",
    ]


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
                "The operator supplied sufficient context for one read-only Chief "
                "response."
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
    assert merged.ask_shape.output_constraints.required_sections == [
        "Assessment",
        "Paste-ready internal Slack recommendation",
    ]


def test_named_multi_part_deliverables_become_output_sections_not_routes() -> None:
    request = (
        "Give me one decision brief covering what is supported and the first "
        "validation question, and a short internal Slack note I can paste to the team."
    )

    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    assert plan.ask_shape.output_constraints.required_sections == [
        "Decision brief",
        "Internal Slack note",
    ]
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
    assert plan.ask_shape.output_constraints.required_sections == [
        "Assessment",
        "Paste-ready internal Slack recommendation",
    ]
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
