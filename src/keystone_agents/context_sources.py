"""Explicit context-source catalog for Keystone agent runs."""

from __future__ import annotations

from dataclasses import dataclass

ALL_AGENT_ROUTES = (
    "orchestrator",
    "chief_of_staff",
    "business_research_analyst",
    "opportunity_scout",
    "gmail_triage",
    "outreach_composer",
    "airtable_context_agent",
    "google_workspace_context_agent",
    "zotero_context_agent",
    "rss_context_agent",
    "preprints_context_agent",
)


@dataclass(frozen=True)
class ContextSourceSpec:
    """Static contract for a context surface that may feed agent reasoning."""

    source_id: str
    owner_modules: tuple[str, ...]
    allowed_agents: tuple[str, ...]
    contract: str
    live_flags: tuple[str, ...] = ()
    approval_notes: tuple[str, ...] = ()
    source_attribution: str = ""
    validation_paths: tuple[str, ...] = ()
    prompt_only: bool = False


CONTEXT_SOURCE_CATALOG: tuple[ContextSourceSpec, ...] = (
    ContextSourceSpec(
        source_id="operator_project_policy",
        owner_modules=("AGENTS.md", "src/keystone_agents/prompts"),
        allowed_agents=ALL_AGENT_ROUTES,
        contract="Prompt context only; establishes safety, style, and project rules.",
        approval_notes=("Policy context is not a live data source and must not imply writes.",),
        source_attribution="Not cited as factual external evidence.",
        validation_paths=("tests/test_architecture.py", "tests/test_prompt_contracts.py"),
        prompt_only=True,
    ),
    ContextSourceSpec(
        source_id="work_item_state",
        owner_modules=(
            "src/keystone_agents/schemas/work_item.py",
            "src/keystone_agents/work_items.py",
            "src/keystone_agents/workflow_runner.py",
        ),
        allowed_agents=ALL_AGENT_ROUTES,
        contract="Typed WorkItem state, context packs, artifacts, blockers, and audit notes.",
        approval_notes=("Python gates remain authoritative for approvals and side effects.",),
        source_attribution="Artifacts and WorkItemSourceRef records carry source refs.",
        validation_paths=("tests/test_context_packs.py", "tests/test_workflow_runner.py"),
    ),
    ContextSourceSpec(
        source_id="slack_thread_context",
        owner_modules=(
            "src/keystone_agents/workflow_runner.py",
            "scripts/handle_slack_agent_action.py",
        ),
        allowed_agents=("orchestrator", "chief_of_staff", "business_research_analyst"),
        contract="Bounded selected Slack thread/message context plus ordered source refs.",
        approval_notes=("Slack context is read-only unless a separate posting approval applies.",),
        source_attribution="Visible Slack links are promoted to ordered_sources/source_refs.",
        validation_paths=("tests/test_slack_agent_actions.py", "tests/test_workflow_runner.py"),
    ),
    ContextSourceSpec(
        source_id="agent_memory",
        owner_modules=("src/keystone_agents/memory.py", "src/keystone_agents/tools/memory_tool.py"),
        allowed_agents=ALL_AGENT_ROUTES,
        contract="Retrieval memory and duplicate-check context with prompt-safety flags.",
        approval_notes=(
            "Memory writes stay typed and scoped; no secrets, PHI, or raw private threads.",
        ),
        source_attribution="Memory items must preserve source refs or state that verification is needed.",
        validation_paths=("tests/test_architecture.py",),
    ),
    ContextSourceSpec(
        source_id="local_docs_zotero_cache",
        owner_modules=(
            "src/keystone_agents/tools/local_context_tool.py",
            "src/keystone_agents/zotero_research.py",
        ),
        allowed_agents=("chief_of_staff", "business_research_analyst", "zotero_context_agent"),
        contract="Read-only local docs and Zotero cache context with bounded paths.",
        approval_notes=(
            "Local context must remain read-only unless a separate artifact write is approved.",
        ),
        source_attribution="Zotero/local records become WorkItemSourceRef entries with source IDs.",
        validation_paths=("tests/test_workflow_runner.py", "tests/test_local_context_tool.py"),
    ),
    ContextSourceSpec(
        source_id="web_search_and_page_extraction",
        owner_modules=(
            "src/keystone_agents/tools/search_provider.py",
            "src/keystone_agents/retrieval_policy.py",
            "src/keystone_agents/tools/website_extraction_tool.py",
            "src/keystone_agents/tools/serper_tool.py",
        ),
        allowed_agents=("chief_of_staff", "business_research_analyst", "opportunity_scout"),
        contract=(
            "Shared SearchProvider retrieval and secondary page extraction with "
            "retrieval_diagnostics, source_refs, and source-backed claims."
        ),
        live_flags=(
            "live_search",
            "KEYSTONE_LIVE_MODE",
            "SEARXNG_BASE_URL",
            "EXA_API_KEY",
            "TAVILY_API_KEY",
            "FIRECRAWL_API_KEY",
            "KEYSTONE_SERPER_ENABLED",
        ),
        approval_notes=(
            "Search and extraction are read-only; Serper stays disabled unless credits are restored.",
        ),
        source_attribution="Provider results are candidates; extracted/read URLs become source_refs.",
        validation_paths=(
            "tests/test_search_provider.py",
            "tests/test_retrieval_policy.py",
            "tests/test_live_retrieval_integration.py",
            "tests/test_website_extraction_tool.py",
        ),
    ),
    ContextSourceSpec(
        source_id="airtable_finance_tax",
        owner_modules=(
            "src/keystone_agents/tools/internal_data_tools.py",
            "src/keystone_agents/schemas/airtable.py",
        ),
        allowed_agents=("chief_of_staff", "orchestrator", "airtable_context_agent"),
        contract=(
            "Schema-first Airtable reads; direct-agent writes require typed fields "
            "and approval references."
        ),
        live_flags=("KEYSTONE_AIRTABLE_API_KEY", "KEYSTONE_AIRTABLE_BASE_ID"),
        approval_notes=(
            "Airtable writes require explicit live flags and scoped approval references.",
        ),
        source_attribution="Airtable-derived facts should name the table/context, not external URLs.",
        validation_paths=("tests/test_chief_of_staff.py", "tests/test_architecture.py"),
    ),
    ContextSourceSpec(
        source_id="gmail",
        owner_modules=(
            "src/keystone_agents/tools/gmail_tool.py",
            "src/keystone_agents/agents/gmail_triage.py",
        ),
        allowed_agents=("gmail_triage", "chief_of_staff", "orchestrator", "outreach_composer"),
        contract="Gmail read, triage, labels, and draft-only tools; no sends.",
        live_flags=("live_gmail", "KEYSTONE_LIVE_GMAIL", "GOOGLE_CLIENT_ID"),
        approval_notes=(
            "Gmail sends are never automatic; drafts/labels require live flags and approval gates.",
        ),
        source_attribution="Email-derived claims should cite thread/message context without exposing secrets.",
        validation_paths=("tests/test_gmail_triage.py", "tests/test_architecture.py"),
    ),
    ContextSourceSpec(
        source_id="google_workspace",
        owner_modules=("src/keystone_agents/tools/internal_data_tools.py",),
        allowed_agents=(
            "chief_of_staff",
            "orchestrator",
            "outreach_composer",
            "google_workspace_context_agent",
            "zotero_context_agent",
        ),
        contract=(
            "Scoped Google Drive/Docs/Sheets context, Drive file/media metadata, "
            "and approved write tools."
        ),
        live_flags=("KEYSTONE_GOOGLE_WORKSPACE_LIVE", "GOOGLE_CLIENT_ID"),
        approval_notes=("Workspace writes require explicit live flags and approval references.",),
        source_attribution="Workspace-derived facts must name the artifact/context when visible externally.",
        validation_paths=(
            "tests/test_google_workspace_health_check.py",
            "tests/test_agent_registry.py",
        ),
    ),
    ContextSourceSpec(
        source_id="slack_posting",
        owner_modules=(
            "src/keystone_agents/tools/slack_tool.py",
            "src/keystone_agents/tools/operations_publisher_tool.py",
        ),
        allowed_agents=("chief_of_staff", "orchestrator", "outreach_composer"),
        contract="Slack post plans and approved internal posts; read-only by default.",
        live_flags=("KNI_SLACK_BOT_TOKEN", "KEYSTONE_SLACK_LIVE"),
        approval_notes=("Posting requires channel policy and approval; no silent external posts.",),
        source_attribution="Slack-facing factual posts must include visible source URLs when external claims appear.",
        validation_paths=("tests/test_slack_agent_actions.py", "tests/test_prompt_contracts.py"),
    ),
    ContextSourceSpec(
        source_id="openai_file_search",
        owner_modules=(
            "src/keystone_agents/file_search.py",
            "src/keystone_agents/file_search_corpus.py",
        ),
        allowed_agents=("chief_of_staff", "business_research_analyst", "orchestrator"),
        contract="Approved vector-store corpus for durable docs, runbooks, and prior traces.",
        live_flags=("OPENAI_API_KEY", "KEYSTONE_OPENAI_FILE_SEARCH"),
        approval_notes=(
            "Corpus changes require approval; do not upload secrets, PHI, raw private "
            "messages, or unapproved artifacts.",
        ),
        source_attribution="Retrieved file chunks must preserve document/source identifiers.",
        validation_paths=("tests/test_file_search.py", "tests/test_architecture.py"),
    ),
    ContextSourceSpec(
        source_id="announcement_feed_history",
        owner_modules=(
            "src/keystone_agents/storage/sqlite_store.py",
            "src/keystone_agents/multi_agent_automations.py",
            "docs/corpus/resources/file_search_corpus_policy.md",
        ),
        allowed_agents=(
            "chief_of_staff",
            "business_research_analyst",
            "orchestrator",
            "rss_context_agent",
            "preprints_context_agent",
        ),
        contract=(
            "Local canonical RSS/preprint announcement records with derived SQLite "
            "retrieval index; hosted vector stores are not canonical state."
        ),
        live_flags=(),
        approval_notes=(
            "Hosted vector upload requires separate approval, public/sanitized chunks, "
            "source provenance, sensitivity, and retention metadata.",
        ),
        source_attribution="Retrieved review history must cite feed item ids, source URLs, and dates.",
        validation_paths=("tests/test_storage.py", "tests/test_multi_agent_automations.py"),
    ),
    ContextSourceSpec(
        source_id="sandbox_workspace_review",
        owner_modules=("src/keystone_agents/sandboxing.py", "docs/SANDBOX_AGENTS.md"),
        allowed_agents=("orchestrator", "chief_of_staff", "business_research_analyst"),
        contract="Optional draft-artifact workspace review over scoped mounted files.",
        live_flags=("KEYSTONE_SANDBOX_LIVE",),
        approval_notes=(
            "Sandbox review is read-only for live systems and must not perform Slack, "
            "Gmail, CRM, scheduling, publication, or other live side effects without approval.",
        ),
        source_attribution="Sandbox outputs are draft artifacts and require host review before release.",
        validation_paths=("tests/test_sandboxing.py", "tests/test_architecture.py"),
    ),
)


def context_source_catalog() -> tuple[ContextSourceSpec, ...]:
    """Return the immutable context-source catalog."""

    return CONTEXT_SOURCE_CATALOG


def context_source_by_id(source_id: str) -> ContextSourceSpec:
    """Return a context source by ID or raise KeyError."""

    for source in CONTEXT_SOURCE_CATALOG:
        if source.source_id == source_id:
            return source
    raise KeyError(source_id)
