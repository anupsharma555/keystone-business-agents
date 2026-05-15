"""Declarative tool-access policy for Keystone specialist agents."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentToolPolicy:
    """Allowed retrieval/tool classes for one agent."""

    agent_name: str
    allowed_tool_names: frozenset[str]
    rationale: str


AGENT_TOOL_POLICIES: dict[str, AgentToolPolicy] = {
    "business_research_analyst": AgentToolPolicy(
        agent_name="business_research_analyst",
        allowed_tool_names=frozenset(
            {
                "load_contact_context",
                "load_crm_account_context",
                "load_approved_contact_context",
                "load_approved_crm_context",
                "list_local_context_sources",
                "search_local_context",
                "read_local_context_file",
                "retrieve_memory",
                "check_workflow_duplicate",
                "search_web",
                "file_search",
                "fetch_company_page",
                "fetch_linkedin_or_profile_placeholder",
                "extract_company_signals",
                "dedupe_and_rank_sources",
                "build_source_bundle_for_synthesis",
                "synthesize_company_profile_from_source_bundle",
                "compare_company_profiles_for_decision",
                "save_company_profile_memory",
                "save_retrieval_tool_performance_memory",
            }
        ),
        rationale="Research agents may retrieve source-backed public and approved local context.",
    ),
    "opportunity_scout": AgentToolPolicy(
        agent_name="opportunity_scout",
        allowed_tool_names=frozenset(
            {
                "list_local_context_sources",
                "search_local_context",
                "read_local_context_file",
                "retrieve_memory",
                "check_workflow_duplicate",
                "search_web",
                "search_opportunity_sources_placeholder",
                "load_existing_opportunity_state",
                "search_funding_news_sources",
                "search_job_posting_sources",
                "search_clinical_trials_sources",
                "search_grant_sources",
                "search_conference_publication_sources",
                "search_journal_call_sources",
                "search_contract_rfp_sources",
                "search_company_page_sources",
                "score_opportunity",
                "handoff_to_business_research_analyst_placeholder",
                "save_opportunity_placeholder",
                "save_entity_memory",
                "save_opportunity_memory",
            }
        ),
        rationale=(
            "Scout agents may discover opportunities but should hand off research "
            "before outreach."
        ),
    ),
    "outreach_composer": AgentToolPolicy(
        agent_name="outreach_composer",
        allowed_tool_names=frozenset(
            {
                "load_company_profile",
                "load_research_brief_profile",
                "load_opportunity_record",
                "load_contact_context",
                "load_crm_account_context",
                "load_style_profile",
                "list_local_context_sources",
                "search_local_context",
                "read_local_context_file",
                "list_outreach_templates",
                "load_outreach_template",
                "retrieve_outreach_example_guidance",
                "load_email_style_profile",
                "retrieve_memory",
                "retrieve_outreach_examples",
                "check_workflow_duplicate",
                "load_approved_contact_context",
                "load_approved_crm_context",
                "load_approved_outreach_examples",
                "check_unsupported_claims",
                "build_approved_outreach_drafting_context",
                "compose_outreach_draft_llm_constrained",
                "build_call_prep_artifact",
                "build_follow_up_schedule_record",
                "save_outreach_dedup_memory",
                "learn_email_style_profile",
                "create_approval_queue_item",
                "create_approval_request_placeholder",
            }
        ),
        rationale="Outreach agents draft from approved context and must not run open web search.",
    ),
    "gmail_triage": AgentToolPolicy(
        agent_name="gmail_triage",
        allowed_tool_names=frozenset(
            {
                "get_gmail_message",
                "apply_gmail_labels",
                "create_gmail_draft_reply",
                "load_email_style_profile",
                "list_local_context_sources",
                "search_local_context",
                "read_local_context_file",
                "create_approval_queue_item",
            }
        ),
        rationale="Gmail agents inspect approved email/thread context and remain draft-only.",
    ),
    "orchestrator": AgentToolPolicy(
        agent_name="orchestrator",
        allowed_tool_names=frozenset(
            {
                "list_local_context_sources",
                "search_local_context",
                "read_local_context_file",
                "route_request_placeholder",
                "load_orchestrator_workflow_state",
                "load_pending_approval_items",
                "business_research_analyst_research_brief",
            }
        ),
        rationale="The orchestrator routes, reviews, and sets retrieval hints instead of browsing.",
    ),
    "chief_of_staff": AgentToolPolicy(
        agent_name="chief_of_staff",
        allowed_tool_names=frozenset(
            {
                "list_chief_of_staff_context_sources",
                "summarize_slack_runtime_config",
                "search_slack_repo_context",
                "read_slack_repo_context_file",
                "lookup_slack_workflow_capability",
                "search_official_operations_docs",
                "file_search",
                "list_local_context_sources",
                "search_local_context",
                "read_local_context_file",
                "list_automation_specs",
                "list_recent_automation_runs",
                "list_channel_automation_bindings",
                "summarize_automation_health",
                "list_pending_automation_approvals",
                "inspect_active_work_items",
                "publish_document_report",
                "publish_table_mirror",
                "publish_slack_summary",
            }
        ),
        rationale=(
            "The Chief of Staff may inspect operations state and coordinate bounded "
            "internal review writes through typed tools."
        ),
    ),
}


def tool_policy_for_agent(agent_name: str) -> AgentToolPolicy | None:
    """Return the declared tool policy for an agent name."""

    return AGENT_TOOL_POLICIES.get(str(agent_name or "").strip())


def disallowed_tool_names(agent_name: str, tool_names: list[str]) -> list[str]:
    """Return tool names not allowed for the agent under the local policy."""

    policy = tool_policy_for_agent(agent_name)
    if policy is None:
        return []
    return sorted(name for name in tool_names if name not in policy.allowed_tool_names)
