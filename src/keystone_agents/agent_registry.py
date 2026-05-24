"""Registry of Keystone SDK agents and their extension metadata."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from typing import Any

from pydantic import BaseModel

from keystone_agents.agent_tool_policy import tool_policy_for_agent
from keystone_agents.schemas.orchestrator import HandoffSpec
from keystone_agents.tools.internal_data_tools import GOOGLE_WORKSPACE_TOOL_NAMES

AIRTABLE_READ_TOOL_NAMES = ("airtable_get_base_schema", "airtable_read_records")
AIRTABLE_WRITE_TOOL_NAMES = ("airtable_write_record",)
WEB_STRUCTURING_TOOL_NAMES = ("structure_web_data_for_schema",)
PLAYWRIGHT_RESEARCH_TOOL_NAMES = ("render_page",)
BROWSER_DIAGNOSTIC_TOOL_NAMES = (
    "capture_browser_diagnostics",
    "summarize_rendered_page_diagnostics",
)


@dataclass(frozen=True)
class AgentSpec:
    """Static metadata for an agent builder.

    Builders and schemas are stored as import strings so the registry can be
    imported by orchestration code without forcing every agent module to load.
    """

    route_name: str
    agent_name: str
    builder: str
    output_schema: str
    prompt_files: tuple[str, ...]
    # Prompt files are instruction fragments only. `skills.md` is not a runtime
    # capability registry and must not imply hidden routing or dynamic tools.
    tools: tuple[str, ...] = ()
    live_flags_required: tuple[str, ...] = ()
    eval_datasets: tuple[str, ...] = ()
    validation_paths: tuple[str, ...] = ()
    handoff_description: str = ""
    safety_notes: tuple[str, ...] = ()

    @property
    def builder_name(self) -> str:
        """Return the unqualified builder function name."""

        return self.builder.rsplit(":", maxsplit=1)[-1]

    def resolve_builder(self) -> Callable[..., Any]:
        """Import and return the builder function."""

        module_name, function_name = self.builder.split(":", maxsplit=1)
        return getattr(import_module(module_name), function_name)

    def resolve_output_schema(self) -> type[BaseModel]:
        """Import and return the Pydantic output schema."""

        module_name, class_name = self.output_schema.rsplit(".", maxsplit=1)
        schema = getattr(import_module(module_name), class_name)
        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            raise TypeError(f"{self.output_schema} is not a Pydantic BaseModel schema")
        return schema

    def build_agent(self, **kwargs: Any) -> Any:
        """Build the SDK agent for this spec."""

        return self.resolve_builder()(**kwargs)

    def to_handoff_spec(self) -> HandoffSpec:
        """Return the orchestrator handoff metadata for specialist routes."""

        return HandoffSpec(
            route=self.route_name,  # type: ignore[arg-type]
            agent_name=self.agent_name,
            builder=self.builder_name,
            description=self.handoff_description,
        )

    def to_card(self) -> dict[str, Any]:
        """Return a JSON-safe agent card."""

        policy = tool_policy_for_agent(self.route_name)
        return {
            "route_name": self.route_name,
            "agent_name": self.agent_name,
            "builder": self.builder,
            "output_schema": self.output_schema,
            "prompt_files": list(self.prompt_files),
            "tools": list(self.tools),
            "live_flags_required": list(self.live_flags_required),
            "eval_datasets": list(self.eval_datasets),
            "validation_paths": list(self.validation_paths),
            "handoff_description": self.handoff_description,
            "safety_notes": list(self.safety_notes),
            "tool_policy": (
                {
                    "agent_name": policy.agent_name,
                    "allowed_tool_names": sorted(policy.allowed_tool_names),
                    "rationale": policy.rationale,
                }
                if policy is not None
                else None
            ),
        }


SPECIALIST_AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(
        route_name="gmail_triage",
        agent_name="Gmail Inbound Triage Agent",
        builder="keystone_agents.agents.gmail_triage:build_gmail_triage_agent",
        output_schema="keystone_agents.schemas.email_triage.EmailTriageResult",
        prompt_files=(
            "keystone_profile.md",
            "safety_policy.md",
            "skills.md",
            "tools.md",
            "gmail_triage.md",
        ),
        tools=(
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
            *AIRTABLE_READ_TOOL_NAMES,
            *AIRTABLE_WRITE_TOOL_NAMES,
            *WEB_STRUCTURING_TOOL_NAMES,
            *GOOGLE_WORKSPACE_TOOL_NAMES,
        ),
        live_flags_required=("--live-gmail", "--no-dry-run", "--live-sdk"),
        eval_datasets=("tests/evals/gmail_triage_cases.json", "evals/gmail_triage.jsonl"),
        validation_paths=("tests/test_gmail_triage.py", "tests/test_sdk_execution.py"),
        handoff_description=(
            "Classify inbound email, recommend labels, flag risk, and request drafts only."
        ),
        safety_notes=(
            "Draft-only replies",
            "No PHI processing",
            "Human approval required before outbound copy is used",
            "Google Workspace writes require live flags and approval references",
        ),
    ),
    AgentSpec(
        route_name="business_research_analyst",
        agent_name="Business Research Analyst",
        builder="keystone_agents.agents.business_research_analyst:build_business_research_analyst_research_brief_agent",
        output_schema="keystone_agents.schemas.research.ResearchBrief",
        prompt_files=(
            "keystone_profile.md",
            "safety_policy.md",
            "skills.md",
            "tools.md",
            "local_context.md",
            "business_research_analyst.md",
        ),
        tools=(
            "list_local_context_sources",
            "search_local_context",
            "read_local_context_file",
            "search_web",
            "fetch_company_page",
            "extract_research_claims_from_html",
            "extract_company_signals",
            "load_approved_contact_context",
            "load_approved_crm_context",
            "retrieve_memory",
            *AIRTABLE_READ_TOOL_NAMES,
            *AIRTABLE_WRITE_TOOL_NAMES,
            *WEB_STRUCTURING_TOOL_NAMES,
            *PLAYWRIGHT_RESEARCH_TOOL_NAMES,
            *BROWSER_DIAGNOSTIC_TOOL_NAMES,
            *GOOGLE_WORKSPACE_TOOL_NAMES,
        ),
        live_flags_required=("--live-search", "--no-dry-run", "--live-sdk"),
        eval_datasets=(
            "tests/evals/business_research_analyst_cases.json",
            "evals/source_attribution.jsonl",
        ),
        validation_paths=("tests/test_business_research_analyst.py",),
        handoff_description=(
            "Create source-attributed research briefs for companies, institutes, "
            "conferences, topics, Zotero collections, and article collections."
        ),
        safety_notes=(
            "Source attribution required",
            "No hallucinated research facts",
            "Hosted file search attaches only with explicit vector store configuration",
            "Local/Zotero context is private unless explicitly approved for external use",
            "CRM/contact context must be approved before use",
            "Google Workspace writes require live flags and approval references",
        ),
    ),
    AgentSpec(
        route_name="opportunity_scout",
        agent_name="Opportunity Scout Agent",
        builder="keystone_agents.agents.opportunity_scout:build_opportunity_scout_agent",
        output_schema="keystone_agents.schemas.opportunity.OpportunityScoutResult",
        prompt_files=(
            "keystone_profile.md",
            "safety_policy.md",
            "skills.md",
            "tools.md",
            "opportunity_scout.md",
        ),
        tools=(
            "retrieve_memory",
            "search_web",
            "search_opportunity_sources_placeholder",
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
            *AIRTABLE_READ_TOOL_NAMES,
            *AIRTABLE_WRITE_TOOL_NAMES,
            *WEB_STRUCTURING_TOOL_NAMES,
            *PLAYWRIGHT_RESEARCH_TOOL_NAMES,
            *BROWSER_DIAGNOSTIC_TOOL_NAMES,
            *GOOGLE_WORKSPACE_TOOL_NAMES,
        ),
        live_flags_required=("--live-search", "--no-dry-run", "--live-sdk"),
        eval_datasets=(
            "tests/evals/opportunity_scout_cases.json",
            "evals/opportunity_scoring.jsonl",
            "evals/source_attribution.jsonl",
        ),
        validation_paths=("tests/test_opportunity_scout.py",),
        handoff_description=(
            "Find and score opportunities, leads, grants, partners, and companies."
        ),
        safety_notes=(
            "No outreach generation",
            "Deduplicate before prioritizing",
            "Source-backed scoring required",
            "Google Workspace writes require live flags and approval references",
        ),
    ),
    AgentSpec(
        route_name="outreach_composer",
        agent_name="Outreach Composer Agent",
        builder="keystone_agents.agents.outreach_composer:build_outreach_composer_agent",
        output_schema="keystone_agents.schemas.outreach.OutreachDraft",
        prompt_files=(
            "keystone_profile.md",
            "safety_policy.md",
            "skills.md",
            "tools.md",
            "outreach_composer.md",
        ),
        tools=(
            "load_company_profile",
            "load_opportunity_record",
            "load_approved_contact_context",
            "load_approved_crm_context",
            "retrieve_memory",
            "search_web",
            "check_unsupported_claims",
            "save_initial_outreach_tracking_record",
            "list_outreach_tracking_records",
            "create_approval_queue_item",
            *AIRTABLE_READ_TOOL_NAMES,
            *AIRTABLE_WRITE_TOOL_NAMES,
            *WEB_STRUCTURING_TOOL_NAMES,
            *GOOGLE_WORKSPACE_TOOL_NAMES,
        ),
        live_flags_required=("--live-sdk",),
        eval_datasets=(
            "tests/evals/outreach_composer_cases.json",
            "evals/outreach_copy_constraints.jsonl",
        ),
        validation_paths=("tests/test_outreach_composer.py",),
        handoff_description=(
            "Create draft-only outreach from approved company or opportunity context."
        ),
        safety_notes=(
            "Draft-only behavior",
            "Human approval required",
            "Only approved source-backed context may be used",
            "Google Workspace writes require live flags and approval references",
        ),
    ),
)

ORCHESTRATOR_AGENT_SPEC = AgentSpec(
    route_name="orchestrator",
    agent_name="Orchestrator Agent",
    builder="keystone_agents.agents.orchestrator:build_orchestrator_agent",
    output_schema="keystone_agents.schemas.orchestrator.OrchestratorResult",
    prompt_files=(
        "keystone_profile.md",
        "safety_policy.md",
        "skills.md",
        "tools.md",
        "orchestrator.md",
    ),
    tools=(
        "route_request_placeholder",
        "load_orchestrator_workflow_state",
        "list_local_context_sources",
        "search_local_context",
        "read_local_context_file",
        "retrieve_memory",
        "load_pending_approval_items",
        "search_web",
        "extract_research_claims_from_html",
        *AIRTABLE_READ_TOOL_NAMES,
        *AIRTABLE_WRITE_TOOL_NAMES,
        *WEB_STRUCTURING_TOOL_NAMES,
        *PLAYWRIGHT_RESEARCH_TOOL_NAMES,
        *BROWSER_DIAGNOSTIC_TOOL_NAMES,
        *GOOGLE_WORKSPACE_TOOL_NAMES,
    ),
    live_flags_required=("--live-sdk",),
    eval_datasets=("evals/orchestrator_routing.jsonl", "evals/safety_refusals.jsonl"),
    validation_paths=("tests/test_orchestrator.py", "tests/test_handoff_contracts.py"),
    handoff_description=(
        "Route Keystone business-agent requests to the correct specialist while preserving "
        "approval gates and no-send policy."
    ),
    safety_notes=(
        "Python safety gates remain authoritative",
        "No specialist handoff may bypass approval policy",
        "Google Workspace writes require live flags and approval references",
    ),
)

CHIEF_OF_STAFF_AGENT_SPEC = AgentSpec(
    route_name="chief_of_staff",
    agent_name="KNI Chief of Staff Agent",
    builder="keystone_agents.agents.chief_of_staff:build_chief_of_staff_agent",
    output_schema="keystone_agents.schemas.chief_of_staff.ChiefOfStaffResult",
    prompt_files=(
        "keystone_profile.md",
        "safety_policy.md",
        "skills.md",
        "tools.md",
        "chief_of_staff.md",
    ),
    tools=(
        "list_chief_of_staff_context_sources",
        "summarize_slack_runtime_config",
        "search_slack_repo_context",
        "read_slack_repo_context_file",
        "lookup_slack_workflow_capability",
        "search_official_operations_docs",
        "retrieve_chief_of_staff_memory",
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
        "search_web",
        "airtable_get_base_schema",
        "airtable_read_records",
        "airtable_write_record",
        *WEB_STRUCTURING_TOOL_NAMES,
        *PLAYWRIGHT_RESEARCH_TOOL_NAMES,
        *BROWSER_DIAGNOSTIC_TOOL_NAMES,
        *GOOGLE_WORKSPACE_TOOL_NAMES,
    ),
    live_flags_required=("--live-sdk",),
    eval_datasets=(),
    validation_paths=("tests/test_chief_of_staff.py",),
    handoff_description=(
        "Plan read-only KNI Slack operations routing across calendar, Gmail, "
        "business-agent, and Slack runtime workflows, including scoped internal "
        "Slack communication when channel policy allows it."
    ),
    safety_notes=(
        "Internal review writes only through typed tools",
        "Hosted file search attaches only with explicit vector store configuration",
        "No unscoped public Slack posts",
        "No Gmail sends or calendar writes",
        "Human approval required before outbound Slack copy is used",
        "Keystone Slack repo access is read-only and secret-filtered",
        "Full article reading is default-off and enabled only by explicit natural-language request",
        "Airtable and Google Workspace writes require typed tools, live flags, "
        "and approval references",
    ),
)

REGISTERED_AGENT_SPECS: tuple[AgentSpec, ...] = (
    *SPECIALIST_AGENT_SPECS,
    ORCHESTRATOR_AGENT_SPEC,
    CHIEF_OF_STAFF_AGENT_SPEC,
)
AGENT_REGISTRY: Mapping[str, AgentSpec] = {spec.route_name: spec for spec in REGISTERED_AGENT_SPECS}


def list_agent_specs() -> Sequence[AgentSpec]:
    """Return the registered canonical agents in stable order."""

    return REGISTERED_AGENT_SPECS


def get_agent_spec(route_name: str) -> AgentSpec:
    """Return a registered spec by route name."""

    return AGENT_REGISTRY[route_name]


def specialist_handoff_specs() -> tuple[HandoffSpec, ...]:
    """Return orchestrator handoff specs derived from registered specialists."""

    return tuple(spec.to_handoff_spec() for spec in SPECIALIST_AGENT_SPECS)


def agent_cards() -> list[dict[str, Any]]:
    """Return JSON-safe cards for docs, dashboards, or CLIs."""

    return [spec.to_card() for spec in REGISTERED_AGENT_SPECS]
