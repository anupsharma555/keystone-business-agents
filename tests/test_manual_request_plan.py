from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from keystone_agents import cli
from keystone_agents.agents.manual_request_planner import _planner_model_configs
from keystone_agents.agents.orchestrator import route_request
from keystone_agents.gmail_triage.execution_plan import infer_gmail_execution_plan
from keystone_agents.manual_request import infer_manual_request_plan, merge_manual_request_plan
from keystone_agents.outreach_composer.execution_plan import infer_outreach_execution_plan
from keystone_agents.schemas.approval import ApprovalState
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.test_pack_specs import get_test_pack_spec


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


def test_manual_plan_preserves_workflow_when_llm_candidate_misreads_company() -> None:
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

    assert merged.intent == "opportunity_to_outreach_loop"
    assert merged.target_agent == "opportunity_scout"
    assert merged.primary_target == "behavioral health AI"
    assert any("Ignored planner override" in warning for warning in merged.planner_warnings)


def test_manual_plan_merge_preserves_explicit_user_scope_over_llm_candidate() -> None:
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
    assert merged.desired_count == 3
    assert merged.lookback_days == 1
    assert merged.gmail_query.startswith("is:unread after:")
    assert merged.draft_policy == "no_drafts_requested"
    assert "model-added grouping suggestion" in merged.constraints


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
    "prompt",
    [
        "Review the business-agent architecture changes and recommend the next three implementation steps.",
        "Audit enabled automations and identify stale, duplicate, or unsafe schedules without changing them.",
        (
            "Summarize the selected Slack thread, identify unresolved operator requests, "
            "and propose an internal follow-up plan."
        ),
    ],
)
def test_manual_plan_routes_operational_planning_to_chief_of_staff(prompt: str) -> None:
    plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    assert plan.requested_agent == "orchestrator"
    assert plan.target_agent == "chief_of_staff"
    assert plan.intent == "route_request"
    assert plan.task_objective == "route_or_continue"
    assert plan.expected_artifact_type == "none"
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


def test_manual_plan_merge_preserves_unnamed_company_discovery_over_live_company_plan() -> None:
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

    assert merged.target_agent == "opportunity_scout"
    assert merged.intent == "opportunity_search"
    assert merged.target_type == "topic"
    assert merged.required_entities == []
    assert any("explicit named-agent request" in warning for warning in merged.planner_warnings)


def test_manual_plan_merge_preserves_explicit_named_agent_when_llm_misroutes() -> None:
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
    assert merged.target_agent == "business_research_analyst"
    assert merged.intent == "company_research"
    assert merged.primary_target == "NeuroFlow"
    assert any("explicit named-agent request" in warning for warning in merged.planner_warnings)


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
def test_manual_plan_merge_preserves_all_explicit_named_agents(
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
    assert merged.target_agent == fallback.target_agent
    assert merged.intent == fallback.intent
    assert any("explicit named-agent request" in warning for warning in merged.planner_warnings)


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


def test_manual_plan_preserves_browser_diagnostics_when_llm_candidate_misroutes() -> None:
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

    assert merged.intent == "browser_diagnostics"
    assert merged.target_agent == "chief_of_staff"
    assert merged.target_type == "url"
    assert any("Ignored planner override" in warning for warning in merged.planner_warnings)


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
        "U.S.-relevant academic institutes"
    )


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
    assert output["selected_agent"] == "outreach_composer"
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
