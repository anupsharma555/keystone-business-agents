"""Declarative tool-access policy for Keystone specialist agents."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from keystone_agents.specialist_tool_names import is_specialist_agent_tool_name
from keystone_agents.tools.internal_data_tools import GOOGLE_WORKSPACE_TOOL_NAMES
from keystone_agents.tools.zotero_context_tools import (
    ZOTERO_CONTEXT_TOOL_NAMES,
    ZOTERO_IMPORT_TOOL_NAMES,
    ZOTERO_READ_CONTEXT_TOOL_NAMES,
    ZOTERO_TEST_LIBRARY_TOOL_NAMES,
    ZOTERO_TEST_NOTE_TOOL_NAMES,
)

GOOGLE_WORKSPACE_ALLOWED_TOOLS = frozenset(GOOGLE_WORKSPACE_TOOL_NAMES)
GOOGLE_WORKSPACE_READ_TOOLS = frozenset(
    {
        "google_doc_read",
        "google_drive_list_folder",
        "google_drive_search_files",
        "google_drive_get_file_metadata",
        "google_slide_deck_read",
        "presentation_search_local",
        "presentation_read_local",
        "google_sheet_list",
        "google_sheet_read_table",
    }
)
GOOGLE_WORKSPACE_WRITE_TOOLS = GOOGLE_WORKSPACE_ALLOWED_TOOLS - GOOGLE_WORKSPACE_READ_TOOLS
AIRTABLE_READ_ALLOWED_TOOLS = frozenset({"airtable_get_base_schema", "airtable_read_records"})
AIRTABLE_WRITE_ALLOWED_TOOLS = frozenset(
    {
        "airtable_write_record",
        "airtable_upload_attachment",
        "airtable_link_attachment",
        "airtable_create_expense_from_receipt",
    }
)
AIRTABLE_TEST_CLEANUP_TOOLS = frozenset({"airtable_delete_test_record"})
AIRTABLE_TEST_LIFECYCLE_TOOLS = frozenset({"airtable_test_record_lifecycle"})
CALENDAR_READ_TOOL_NAMES = frozenset({"read_google_calendar_window"})
WEB_STRUCTURING_ALLOWED_TOOLS = frozenset({"structure_web_data_for_schema"})
PLAYWRIGHT_RESEARCH_ALLOWED_TOOLS = frozenset({"render_page"})
BROWSER_DIAGNOSTIC_ALLOWED_TOOLS = frozenset(
    {"capture_browser_diagnostics", "summarize_rendered_page_diagnostics"}
)
LOCAL_KNI_DOCUMENT_TOOL_NAMES = frozenset(
    {
        "list_kni_document_sources",
        "list_kni_document_folder",
        "search_kni_documents",
        "read_kni_document_file",
    }
)


class AgentToolPolicyError(RuntimeError):
    """Raised when an SDK agent is built with tools outside its declared policy."""


class ToolTier(IntEnum):
    """Ordered tool tiers for runtime attachment and review."""

    CORE_READ = 10
    WEB_SEARCH = 20
    DEEP_RETRIEVAL = 30
    DIAGNOSTIC = 40
    INTERNAL_WRITE = 50
    PUBLISH = 60


@dataclass(frozen=True)
class AgentToolPolicy:
    """Allowed retrieval/tool classes for one agent."""

    agent_name: str
    allowed_tool_names: frozenset[str]
    rationale: str


SOURCE_LAYER_POLICIES: tuple[dict[str, object], ...] = (
    {
        "layer": "local_kni_documents",
        "tools": [
            "list_kni_document_sources",
            "search_kni_documents",
            "read_kni_document_file",
        ],
        "use_for": [
            "Keystone Neuroinformatics local folder evidence",
            "formation records, insurance/COI files, operating guides, policies, templates",
            "questions asking for local evidence paths or internal document context",
        ],
        "not_for": [
            "current public company, market, news, funding, or opportunity facts",
            "OpenAI Agents SDK reference docs unless those docs are present in the local KNI folder",
            "approval to send, post, publish, submit, or share content externally",
        ],
        "reasoning_contract": (
            "Use as bounded local-only evidence. Re-rank candidate documents against the "
            "latest user question and distinguish roles such as organizer, signer, "
            "registered agent, insurer, coverholder, broker, producer, and agency."
        ),
    },
    {
        "layer": "hosted_file_search",
        "tools": ["file_search"],
        "use_for": [
            "stable approved reference corpora",
            "OpenAI Agents SDK/API docs, LangGraph docs, Slack/Gmail API contracts",
            "Keystone operating policy only when that corpus was deliberately configured",
        ],
        "not_for": [
            "local KNI folder evidence",
            "fresh public facts, market signals, news, opportunities, or product claims",
            "private messages, PHI, credentials, secrets, local databases, or raw sensitive artifacts",
        ],
        "reasoning_contract": (
            "Use only when the reference corpus is relevant. Treat snippets as reference "
            "context, not as permission to bypass source attribution or approval gates."
        ),
    },
    {
        "layer": "public_web_search",
        "tools": ["search_web"],
        "use_for": [
            "current public facts",
            "company, market, funding, product, policy, grant, RFP, and opportunity research",
            "source freshness checks and public URL-backed claims",
        ],
        "not_for": [
            "private/local KNI folder evidence",
            "durable SDK/API docs already present in a configured hosted corpus unless freshness is needed",
            "external sending, posting, publishing, or scheduling approval",
        ],
        "reasoning_contract": (
            "Use provider results for discovery and read/extract selected URLs before "
            "making detailed factual claims. Keep provider diagnostics secondary."
        ),
    },
)


CORE_READ_TOOL_NAMES = frozenset(
    {
        "list_local_context_sources",
        "search_local_context",
        "read_local_context_file",
        "retrieve_memory",
        "retrieve_chief_of_staff_memory",
        "retrieve_outreach_examples",
        "retrieve_outreach_example_guidance",
        "check_workflow_duplicate",
        "load_existing_opportunity_state",
        "load_company_profile",
        "load_research_brief_profile",
        "load_opportunity_record",
        "load_style_profile",
        "load_email_style_profile",
        "list_outreach_templates",
        "load_outreach_template",
        "load_outreach_tracking_records",
        "list_outreach_tracking_records",
        "load_orchestrator_workflow_state",
        "load_pending_approval_items",
        "route_request_placeholder",
        "list_chief_of_staff_context_sources",
        "summarize_slack_runtime_config",
        "search_slack_repo_context",
        "read_slack_repo_context_file",
        "lookup_slack_workflow_capability",
        "list_kni_document_folder",
        "list_kni_document_sources",
        "search_kni_documents",
        "read_kni_document_file",
        "list_automation_specs",
        "list_recent_automation_runs",
        "list_channel_automation_bindings",
        "summarize_automation_health",
        "list_pending_automation_approvals",
        "inspect_active_work_items",
        "read_linked_article",
        "retrieve_rss_announcement_history",
        "retrieve_preprint_announcement_history",
        "get_gmail_message",
        "file_search",
        *ZOTERO_READ_CONTEXT_TOOL_NAMES,
        *GOOGLE_WORKSPACE_READ_TOOLS,
        *AIRTABLE_READ_ALLOWED_TOOLS,
        *CALENDAR_READ_TOOL_NAMES,
    }
)
WEB_SEARCH_TOOL_NAMES = frozenset(
    {
        "search_web",
        "search_official_operations_docs",
        "search_opportunity_sources_placeholder",
        "search_funding_news_sources",
        "search_job_posting_sources",
        "search_clinical_trials_sources",
        "search_grant_sources",
        "search_conference_publication_sources",
        "search_journal_call_sources",
        "search_contract_rfp_sources",
        "search_company_page_sources",
    }
)
DEEP_RETRIEVAL_TOOL_NAMES = (
    frozenset(
        {
            "fetch_company_page",
            "extract_research_claims_from_html",
            "fetch_linkedin_or_profile_placeholder",
            "discover_public_company_contacts",
            "extract_company_signals",
            "dedupe_and_rank_sources",
            "build_source_bundle_for_synthesis",
            "synthesize_company_profile_from_source_bundle",
            "compare_company_profiles_for_decision",
            "score_opportunity",
            "structure_web_data_for_schema",
            "handoff_to_business_research_analyst_placeholder",
            "business_research_analyst_research_brief",
            "opportunity_scout_read_only",
        }
    )
    | WEB_STRUCTURING_ALLOWED_TOOLS
)
DIAGNOSTIC_TOOL_NAMES = PLAYWRIGHT_RESEARCH_ALLOWED_TOOLS | BROWSER_DIAGNOSTIC_ALLOWED_TOOLS
CONTACT_CONTEXT_TOOL_NAMES = frozenset(
    {
        "load_contact_context",
        "load_crm_account_context",
        "load_approved_contact_context",
        "load_approved_crm_context",
        "load_approved_outreach_examples",
        "build_approved_outreach_drafting_context",
    }
)
CALENDAR_WRITE_TOOL_NAMES = frozenset(
    {
        "create_google_calendar_event",
        "update_google_calendar_event",
        "delete_google_calendar_event",
    }
)
INTERNAL_WRITE_TOOL_NAMES = (
    AIRTABLE_WRITE_ALLOWED_TOOLS
    | AIRTABLE_TEST_CLEANUP_TOOLS
    | AIRTABLE_TEST_LIFECYCLE_TOOLS
    | GOOGLE_WORKSPACE_WRITE_TOOLS
    | CONTACT_CONTEXT_TOOL_NAMES
    | CALENDAR_WRITE_TOOL_NAMES
    | frozenset(
        {
            "apply_gmail_labels",
            "modify_gmail_message_state",
            "create_gmail_draft_with_attachment",
            "create_gmail_draft_reply",
            "send_gmail_test_draft",
            "create_approval_queue_item",
            "create_approval_request_placeholder",
            "check_unsupported_claims",
            "compose_outreach_draft_llm_constrained",
            "build_call_prep_artifact",
            "build_follow_up_schedule_record",
            "save_company_profile_memory",
            "save_retrieval_tool_performance_memory",
            "save_entity_memory",
            "save_opportunity_memory",
            "save_opportunity_placeholder",
            "save_outreach_dedup_memory",
            "learn_email_style_profile",
            "save_initial_outreach_tracking_record",
            *ZOTERO_IMPORT_TOOL_NAMES,
            *ZOTERO_TEST_NOTE_TOOL_NAMES,
            *ZOTERO_TEST_LIBRARY_TOOL_NAMES,
        }
    )
)
PUBLISH_TOOL_NAMES = frozenset(
    {
        "publish_document_report",
        "publish_internal_artifact",
        "publish_table_mirror",
        "publish_slack_summary",
    }
)

TOOL_TIER_BY_NAME: dict[str, ToolTier] = {
    **{name: ToolTier.CORE_READ for name in CORE_READ_TOOL_NAMES},
    **{name: ToolTier.WEB_SEARCH for name in WEB_SEARCH_TOOL_NAMES},
    **{name: ToolTier.DEEP_RETRIEVAL for name in DEEP_RETRIEVAL_TOOL_NAMES},
    **{name: ToolTier.DIAGNOSTIC for name in DIAGNOSTIC_TOOL_NAMES},
    **{name: ToolTier.INTERNAL_WRITE for name in INTERNAL_WRITE_TOOL_NAMES},
    **{name: ToolTier.PUBLISH for name in PUBLISH_TOOL_NAMES},
}


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
                "discover_public_company_contacts",
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
                "modify_gmail_message_state",
                "create_gmail_draft_with_attachment",
                "create_gmail_draft_reply",
                "send_gmail_test_draft",
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
    "airtable_context_agent": AgentToolPolicy(
        agent_name="airtable_context_agent",
        allowed_tool_names=(
            AIRTABLE_READ_ALLOWED_TOOLS
            | AIRTABLE_WRITE_ALLOWED_TOOLS
            | AIRTABLE_TEST_CLEANUP_TOOLS
            | AIRTABLE_TEST_LIFECYCLE_TOOLS
        ),
        rationale=(
            "The Airtable context agent reads schema and capped records, may perform "
            "direct approved create/update writes, and remains advisory when nested "
            "under Chief of Staff."
        ),
    ),
    "google_workspace_context_agent": AgentToolPolicy(
        agent_name="google_workspace_context_agent",
        allowed_tool_names=GOOGLE_WORKSPACE_ALLOWED_TOOLS,
        rationale=(
            "The Google Workspace context agent lists, searches, reads, and may "
            "perform direct approved Drive/Docs/Sheets writes; nested Chief calls "
            "remain advisory and write-plan only."
        ),
    ),
    "zotero_context_agent": AgentToolPolicy(
        agent_name="zotero_context_agent",
        allowed_tool_names=frozenset(
            {
                "list_local_context_sources",
                "search_local_context",
                "read_local_context_file",
            }
        )
        | frozenset(ZOTERO_CONTEXT_TOOL_NAMES)
        | GOOGLE_WORKSPACE_ALLOWED_TOOLS,
        rationale=(
            "The Zotero context agent reads local/API Zotero metadata and may perform "
            "direct approved Google Workspace artifact writes; Zotero library mutation "
            "is limited to the guarded backend importer and marked disposable test-note "
            "tools, and nested Chief calls remain advisory."
        ),
    ),
    "rss_context_agent": AgentToolPolicy(
        agent_name="rss_context_agent",
        allowed_tool_names=frozenset({"retrieve_rss_announcement_history"}),
        rationale=(
            "The RSS context agent reads canonical local RSS/#announcements history "
            "and returns advisory Chief of Staff context without writes."
        ),
    ),
    "preprints_context_agent": AgentToolPolicy(
        agent_name="preprints_context_agent",
        allowed_tool_names=frozenset({"retrieve_preprint_announcement_history"}),
        rationale=(
            "The preprints context agent reads canonical local preprint/#knowledge-hub "
            "history and returns advisory Chief of Staff context without writes."
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
                "file_search",
                "search_web",
                "extract_research_claims_from_html",
                "business_research_analyst_research_brief",
                "opportunity_scout_read_only",
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
                "list_kni_document_folder",
                "list_kni_document_sources",
                "search_kni_documents",
                "read_kni_document_file",
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
            }
        )
        | GOOGLE_WORKSPACE_ALLOWED_TOOLS
        | AIRTABLE_WRITE_ALLOWED_TOOLS
        | CALENDAR_WRITE_TOOL_NAMES
        | CALENDAR_READ_TOOL_NAMES
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


def source_layer_policy_for_tools(
    tool_names: Sequence[str] | frozenset[str],
) -> tuple[dict[str, object], ...]:
    """Return advisory source-layer guidance for a concrete tool surface.

    This is intentionally not an intent classifier. It tells agents and
    diagnostics which evidence layers are available and what each layer is for,
    while leaving the model to reason from the latest user question and the
    actual tool outputs.
    """

    declared = frozenset(str(name or "").strip() for name in tool_names)
    policies: list[dict[str, object]] = []
    for policy in SOURCE_LAYER_POLICIES:
        tools = tuple(str(name) for name in policy.get("tools", ()))
        if not tools:
            continue
        if all(name in declared for name in tools):
            policies.append(dict(policy))
    return tuple(policies)


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
    return sorted(
        name
        for name in tool_names
        if name not in policy.allowed_tool_names
        and not (policy.agent_name == "chief_of_staff" and is_specialist_agent_tool_name(name))
    )


def normalize_tool_tier(value: ToolTier | str | int | None) -> ToolTier:
    """Normalize a runtime tool tier value."""

    if value is None:
        return ToolTier.PUBLISH
    if isinstance(value, ToolTier):
        return value
    if isinstance(value, int):
        return ToolTier(value)
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {"full", "all", "full_access"}:
        return ToolTier.PUBLISH
    try:
        return ToolTier[normalized.upper()]
    except KeyError as exc:
        allowed = ", ".join(tier.name.lower() for tier in ToolTier)
        raise ValueError(
            f"Unsupported tool tier {value!r}; expected one of: {allowed}, full."
        ) from exc


def tool_tier_for_name(tool_name: str) -> ToolTier | None:
    """Return the declared tier for a stable tool name, if known."""

    normalized = str(tool_name or "").strip()
    if is_specialist_agent_tool_name(normalized):
        return ToolTier.DEEP_RETRIEVAL
    return TOOL_TIER_BY_NAME.get(normalized)


def unclassified_tool_names(tool_names: list[str] | tuple[str, ...] | frozenset[str]) -> list[str]:
    """Return stable tool names that lack an explicit tier assignment."""

    return sorted(name for name in tool_names if tool_tier_for_name(name) is None)


def allowed_tool_names_for_tier(
    agent_name: str,
    tier: ToolTier | str | int | None,
) -> frozenset[str]:
    """Return the agent's allowed tool names at or below the requested tier."""

    policy = tool_policy_for_agent(agent_name)
    if policy is None:
        return frozenset()
    max_tier = normalize_tool_tier(tier)
    return frozenset(
        name
        for name in policy.allowed_tool_names
        if (tool_tier_for_name(name) or ToolTier.PUBLISH) <= max_tier
    )


def filter_tools_for_tier(
    agent_name: str,
    tools: list[Any],
    tier: ToolTier | str | int | None,
) -> list[Any]:
    """Filter a concrete SDK tool list to the agent's allowed tier."""

    allowed_names = allowed_tool_names_for_tier(agent_name, tier)
    return [tool for tool in tools if tool_name_for_policy(tool) in allowed_names]


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

    disallowed = sorted(
        name
        for name in tool_names
        if name not in policy.allowed_tool_names
        and not (policy.agent_name == "chief_of_staff" and is_specialist_agent_tool_name(name))
    )
    if disallowed:
        raise AgentToolPolicyError(
            "Agent "
            f"{resolved_agent_name!r} attempted to attach disallowed tool(s): "
            f"{', '.join(disallowed)}."
        )
    return tool_names
