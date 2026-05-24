"""Declarative tool-access policy for Keystone specialist agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from keystone_agents.tools.internal_data_tools import GOOGLE_WORKSPACE_TOOL_NAMES

GOOGLE_WORKSPACE_ALLOWED_TOOLS = frozenset(GOOGLE_WORKSPACE_TOOL_NAMES)
AIRTABLE_READ_ALLOWED_TOOLS = frozenset({"airtable_get_base_schema", "airtable_read_records"})
AIRTABLE_WRITE_ALLOWED_TOOLS = frozenset({"airtable_write_record"})
WEB_STRUCTURING_ALLOWED_TOOLS = frozenset({"structure_web_data_for_schema"})
PLAYWRIGHT_RESEARCH_ALLOWED_TOOLS = frozenset({"render_page"})
BROWSER_DIAGNOSTIC_ALLOWED_TOOLS = frozenset(
    {"capture_browser_diagnostics", "summarize_rendered_page_diagnostics"}
)


class AgentToolPolicyError(RuntimeError):
    """Raised when an SDK agent is built with tools outside its declared policy."""


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
                "extract_research_claims_from_html",
                "fetch_linkedin_or_profile_placeholder",
                "extract_company_signals",
                "dedupe_and_rank_sources",
                "build_source_bundle_for_synthesis",
                "synthesize_company_profile_from_source_bundle",
                "compare_company_profiles_for_decision",
                "save_company_profile_memory",
                "save_retrieval_tool_performance_memory",
            }
        )
        | GOOGLE_WORKSPACE_ALLOWED_TOOLS
        | AIRTABLE_READ_ALLOWED_TOOLS
        | AIRTABLE_WRITE_ALLOWED_TOOLS
        | WEB_STRUCTURING_ALLOWED_TOOLS
        | PLAYWRIGHT_RESEARCH_ALLOWED_TOOLS
        | BROWSER_DIAGNOSTIC_ALLOWED_TOOLS,
        rationale=(
            "Research agents may retrieve source-backed public and approved local context, "
            "then perform scoped internal Airtable/Google writes through typed tools."
        ),
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
                "extract_research_claims_from_html",
                "score_opportunity",
                "handoff_to_business_research_analyst_placeholder",
                "save_opportunity_placeholder",
                "save_entity_memory",
                "save_opportunity_memory",
            }
        )
        | GOOGLE_WORKSPACE_ALLOWED_TOOLS
        | AIRTABLE_READ_ALLOWED_TOOLS
        | AIRTABLE_WRITE_ALLOWED_TOOLS
        | WEB_STRUCTURING_ALLOWED_TOOLS
        | PLAYWRIGHT_RESEARCH_ALLOWED_TOOLS
        | BROWSER_DIAGNOSTIC_ALLOWED_TOOLS,
        rationale=(
            "Scout agents may discover opportunities but should hand off research before outreach."
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
                "search_web",
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
                "save_initial_outreach_tracking_record",
                "list_outreach_tracking_records",
            }
        )
        | GOOGLE_WORKSPACE_ALLOWED_TOOLS
        | AIRTABLE_READ_ALLOWED_TOOLS
        | AIRTABLE_WRITE_ALLOWED_TOOLS
        | WEB_STRUCTURING_ALLOWED_TOOLS,
        rationale=(
            "Outreach agents may use source lookup and approved context for draft-only "
            "work, but must not send email or bypass approval."
        ),
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
                "retrieve_memory",
                "search_web",
                "list_outreach_tracking_records",
                "create_approval_queue_item",
            }
        )
        | GOOGLE_WORKSPACE_ALLOWED_TOOLS
        | AIRTABLE_READ_ALLOWED_TOOLS
        | AIRTABLE_WRITE_ALLOWED_TOOLS
        | WEB_STRUCTURING_ALLOWED_TOOLS,
        rationale=(
            "Gmail agents inspect approved email/thread, memory, local, web, and Airtable "
            "context, can update internal trackers, and remain draft-only."
        ),
    ),
    "orchestrator": AgentToolPolicy(
        agent_name="orchestrator",
        allowed_tool_names=frozenset(
            {
                "list_local_context_sources",
                "search_local_context",
                "read_local_context_file",
                "retrieve_memory",
                "route_request_placeholder",
                "load_orchestrator_workflow_state",
                "load_pending_approval_items",
                "search_web",
                "extract_research_claims_from_html",
                "business_research_analyst_research_brief",
            }
        )
        | GOOGLE_WORKSPACE_ALLOWED_TOOLS
        | AIRTABLE_READ_ALLOWED_TOOLS
        | AIRTABLE_WRITE_ALLOWED_TOOLS
        | WEB_STRUCTURING_ALLOWED_TOOLS
        | PLAYWRIGHT_RESEARCH_ALLOWED_TOOLS
        | BROWSER_DIAGNOSTIC_ALLOWED_TOOLS,
        rationale=(
            "The orchestrator routes, reviews, and may coordinate scoped internal "
            "Airtable/Google writes and read-only rendered-page diagnostics without "
            "bypassing specialist ownership."
        ),
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
                "retrieve_chief_of_staff_memory",
                "file_search",
                "list_local_context_sources",
                "search_local_context",
                "read_local_context_file",
                "extract_research_claims_from_html",
                "list_automation_specs",
                "list_recent_automation_runs",
                "list_channel_automation_bindings",
                "summarize_automation_health",
                "list_pending_automation_approvals",
                "inspect_active_work_items",
                "publish_document_report",
                "publish_internal_artifact",
                "publish_table_mirror",
                "publish_slack_summary",
                "read_linked_article",
                "search_web",
                "airtable_get_base_schema",
                "airtable_read_records",
                "airtable_write_record",
            }
        )
        | GOOGLE_WORKSPACE_ALLOWED_TOOLS
        | WEB_STRUCTURING_ALLOWED_TOOLS
        | PLAYWRIGHT_RESEARCH_ALLOWED_TOOLS
        | BROWSER_DIAGNOSTIC_ALLOWED_TOOLS,
        rationale=(
            "The Chief of Staff may inspect operations state and coordinate bounded "
            "internal review writes through typed tools."
        ),
    ),
}


def tool_policy_for_agent(agent_name: str) -> AgentToolPolicy | None:
    """Return the declared tool policy for an agent name."""

    return AGENT_TOOL_POLICIES.get(str(agent_name or "").strip())


def tool_name_for_policy(tool: Any) -> str:
    """Return the stable policy name for a local or hosted SDK tool."""

    explicit_name = getattr(tool, "name", None)
    if explicit_name:
        return str(explicit_name)

    function_name = getattr(tool, "__name__", None)
    if function_name:
        return str(function_name)

    class_name = tool.__class__.__name__
    if class_name in {"FileSearchTool", "HostedFileSearchTool"}:
        return "file_search"
    if class_name in {"WebSearchTool", "HostedWebSearchTool"}:
        return "search_web"

    return f"<unnamed_tool:{class_name}>"


def disallowed_tool_names(agent_name: str, tool_names: list[str]) -> list[str]:
    """Return tool names not allowed for the agent under the local policy."""

    policy = tool_policy_for_agent(agent_name)
    if policy is None:
        return []
    return sorted(name for name in tool_names if name not in policy.allowed_tool_names)


def validate_agent_tool_policy(
    agent_name: str,
    tools: list[Any],
    *,
    strict: bool = True,
) -> tuple[str, ...]:
    """Validate tool access for one agent and return the resolved tool names."""

    resolved_agent_name = str(agent_name or "").strip()
    tool_names = tuple(tool_name_for_policy(tool) for tool in tools)
    policy = tool_policy_for_agent(resolved_agent_name)
    if policy is None:
        if strict:
            raise AgentToolPolicyError(
                f"No AgentToolPolicy is declared for agent {resolved_agent_name!r}."
            )
        return tool_names

    disallowed = sorted(name for name in tool_names if name not in policy.allowed_tool_names)
    if disallowed:
        raise AgentToolPolicyError(
            "Agent "
            f"{resolved_agent_name!r} attempted to attach disallowed tool(s): "
            f"{', '.join(disallowed)}."
        )
    return tool_names
