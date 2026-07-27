"""Orchestrator agent builder and deterministic fixture-mode routing."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, Field

from keystone_agents.agent_registry import SPECIALIST_AGENT_SPECS, specialist_handoff_specs
from keystone_agents.agent_tool_policy import filter_tools_for_tier
from keystone_agents.authority.semantic import ExecutionIntentAuthority
from keystone_agents.feedback import build_operator_feedback_request
from keystone_agents.file_search import append_configured_file_search_tools
from keystone_agents.guardrails import (
    assess_text_guardrails,
    keystone_guardrails,
    keystone_tool_guardrail_kwargs,
)
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.orchestrator.routing import (
    OPPORTUNITY_RE as _OPPORTUNITY_RE,
)
from keystone_agents.orchestrator.routing import (
    OUTREACH_RE as _OUTREACH_RE,
)
from keystone_agents.orchestrator.routing import (
    looks_like_company as _looks_like_company,
)
from keystone_agents.orchestrator.routing import (
    looks_like_email as _looks_like_email,
)
from keystone_agents.orchestrator.routing import (
    looks_like_resume_request as _looks_like_resume_request,
)
from keystone_agents.orchestrator.routing import (
    looks_like_send_side_effect as _looks_like_send_side_effect,
)
from keystone_agents.orchestrator.routing import (
    looks_like_thread_local_draft_request as _looks_like_thread_local_draft_request,
)
from keystone_agents.orchestrator.routing import (
    mapping_value as _mapping_value,
)
from keystone_agents.orchestrator.routing import (
    payload_text as _payload_text,
)
from keystone_agents.planning.compatibility import (
    infer_manual_request_plan,
    positive_capability_text,
)
from keystone_agents.quality_budget import business_research_quality_budget
from keystone_agents.retrieval_policy import derive_request_autonomy_hint
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.approval import (
    ApprovalScope,
    ApprovalState,
    state_allows_drafting,
)
from keystone_agents.schemas.company_profile import CompanyProfile
from keystone_agents.schemas.decision_trace import DecisionTrace
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.orchestrator import (
    HandoffSpec,
    OrchestratorOutputReview,
    OrchestratorOutputReviewScore,
    OrchestratorResult,
    OrchestratorReviewBaseline,
    OrchestratorReviewChecks,
    OrchestratorReviewCostGuard,
    OutputReviewStatus,
    RouteName,
)
from keystone_agents.schemas.retrieval import RetrievalHint
from keystone_agents.sdk import (
    Agent,
    build_model_settings,
    build_sdk_agent,
    compose_instructions,
    function_tool,
)
from keystone_agents.skill_sets import select_agent_skill_names, skill_request_text
from keystone_agents.source_layer_context import runtime_source_layer_policy_context
from keystone_agents.specialist_agent_tools import build_specialist_agent_tools
from keystone_agents.specialist_tool_names import (
    ORCHESTRATOR_BUSINESS_RESEARCH_TOOL_NAME,
    ORCHESTRATOR_OPPORTUNITY_SCOUT_TOOL_NAME,
)
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env, redact_secrets
from keystone_agents.tools.browser_diagnostics_tool import (
    capture_browser_diagnostics,
    summarize_rendered_page_diagnostics,
)
from keystone_agents.tools.html_review_tool import extract_research_claims_from_html
from keystone_agents.tools.internal_data_tools import (
    airtable_create_expense_from_receipt,
    airtable_get_base_schema,
    airtable_read_records,
    airtable_upload_attachment,
    airtable_write_record,
    google_workspace_tools,
)
from keystone_agents.tools.local_context_tool import (
    list_local_context_sources,
    read_local_context_file,
    search_local_context,
)
from keystone_agents.tools.memory_tool import retrieve_memory
from keystone_agents.tools.playwright_tool import render_page
from keystone_agents.tools.serper_tool import search_web
from keystone_agents.tools.storage_tool import load_pending_approval_items
from keystone_agents.tools.web_structuring_tool import structure_web_data_for_schema

INTENDED_HANDOFFS: tuple[HandoffSpec, ...] = specialist_handoff_specs()
ORCHESTRATOR_REASONING_EFFORT = "low"
ORCHESTRATOR_REVIEW_VERBOSITY = "low"
ORCHESTRATOR_REVIEW_MAX_TOKENS = 1800
ORCHESTRATOR_SPECIALIST_TOOLS_ENV = "KEYSTONE_ORCHESTRATOR_SPECIALIST_TOOLS"
BUSINESS_RESEARCH_TOOL_NAME = ORCHESTRATOR_BUSINESS_RESEARCH_TOOL_NAME
OPPORTUNITY_SCOUT_TOOL_NAME = ORCHESTRATOR_OPPORTUNITY_SCOUT_TOOL_NAME


_HANDOFF_BY_ROUTE = {handoff.route: handoff for handoff in INTENDED_HANDOFFS}


def _orchestrator_sdk_input(
    typed_input: str | Mapping[str, Any], *, live: bool
) -> str | Mapping[str, Any]:
    """Add live-safe operating context for SDK Orchestrator string prompts."""

    if not live:
        return typed_input
    source_layer_policy = runtime_source_layer_policy_context("orchestrator")
    if isinstance(typed_input, Mapping):
        data = dict(typed_input)
        data.setdefault("runtime_source_layer_policy", source_layer_policy)
        return data
    if not isinstance(typed_input, str):
        return typed_input
    return {
        "request": typed_input,
        "runtime_source_layer_policy": source_layer_policy,
        "live_integration_context": {
            "live_sdk": True,
            "backend_browser_diagnostics_allowed": _env_flag_enabled("KEYSTONE_PLAYWRIGHT_ENABLED"),
            "backend_browser_tools": (
                "render_page",
                "capture_browser_diagnostics",
                "summarize_rendered_page_diagnostics",
            ),
            "browser_tool_live_argument": (
                "Use live=true for backend browser diagnostics only when the user asks "
                "for rendered-page, console, network, layout, or browser diagnostic evidence."
            ),
            "side_effect_policy": (
                "Read-only browser diagnostics, retrieval, routing, and analysis are allowed. "
                "No writes, posts, sends, scheduling, payments, downloads, authenticated "
                "browser sessions, local files, clicks, or form submissions are allowed."
            ),
        },
    }


_FEEDBACK_OBJECT_TYPE_BY_ROUTE: dict[RouteName, str] = {
    "gmail_triage": "email_triage",
    "business_research_analyst": "company_profile",
    "opportunity_scout": "opportunity",
    "outreach_composer": "outreach_draft",
    "clarification": "other",
}
_HARD_REFUSAL_FLAGS = frozenset({"possible_phi", "security", "professional_advice"})
_APPROVED_CONTEXT_OBJECT_TYPES = frozenset({"company_profile", "opportunity"})
_SAFE_STATE_TEXT_CHARS = 140
_REVIEW_TEXT_CHARS = 5000
_REVIEW_FIELD_CHARS = 700
_REVIEW_LIST_ITEMS = 8
_REVIEW_MAPPING_KEYS = 60
_FOLLOWUP_MARKERS = (
    "user follow-up:",
    "follow-up:",
    "follow up:",
    "latest request:",
    "current request:",
    "new request:",
)
_PURE_RESUME_REQUEST_RE = re.compile(
    r"^\s*(?:@\S+\s+)?(?:(?:chief of staff|business research analyst|"
    r"opportunity scout|outreach composer|gmail triage|orchestrator)\s+)?"
    r"(?:workitem\s+|work item\s+)?(?:continue|resume|pick up|what'?s next)"
    r"(?:\s+(?:this|the|prior|current|existing|saved|latest|previous|same|"
    r"last|from|step|workflow|workitem|work item|run|case|thread|slack thread|"
    r"prior slack thread))*"
    r"[\s.?!]*$",
    re.I,
)
_SEND_SIDE_EFFECT_KEYS = frozenset(
    {
        "send_enabled",
        "sent",
        "can_send_email",
        "email_sent",
        "outreach_sent",
        "live_side_effects_enabled",
        "gmail_scheduled",
        "background_job_created",
        "published",
        "scheduled",
        "crm_updated",
    }
)
_HYPE_OR_UNSUPPORTED_TONE_RE = re.compile(
    r"\b(guaranteed|proven results|act now|best-in-class|revolutionary|"
    r"we have helped|worked with companies like yours)\b",
    re.I,
)
_NON_HUMAN_METADATA_KEYS = frozenset(
    {
        "raw_context",
        "raw_result",
        "typed_input",
        "input_payload",
        "prompt",
        "prompt_body",
        "trace",
        "trace_metadata",
        "tool_calls",
        "output_json",
        "profile_json",
        "draft_json",
        "opportunity_json",
    }
)
_CRM_WRITE_RE = re.compile(
    r"\b(?:save|write|add|sync|push|update|create|log)\b.*\b(?:crm|airtable|salesforce|hubspot)\b"
    r"|\b(?:crm|airtable|salesforce|hubspot)\b.*"
    r"\b(?:save|write|add|sync|push|update|create|log)\b",
    re.I,
)
_NEGATED_CRM_WRITE_CLAUSE_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never|avoid|skip)\b"
    r"[^.;\n]{0,260}\b(?:crm|airtable|salesforce|hubspot|external\s+systems?)\b"
    r"[^.;\n]*[.;]?"
    r"|\bwithout\s+(?:saving|writing|adding|syncing|pushing|updating|creating|logging)\b"
    r"[^.;\n]{0,160}\b(?:crm|airtable|salesforce|hubspot|external\s+systems?)\b"
    r"[^.;\n]*[.;]?"
    r"|\b(?:save|write|add|sync|push|update|create|log)\s+(?:no|zero|0)\s+"
    r"(?:crm|airtable|salesforce|hubspot)\b[^.;\n]*[.;]?",
    re.I,
)
_NEGATED_OUTREACH_DRAFT_CLAUSE_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never|avoid|skip|no)\b"
    r"[^.;\n]{0,220}\b(?:draft|write|compose|prepare|outline)\b"
    r"[^.;\n]{0,160}\b(?:outreach|email|reply|response|follow-up|followup|message|note)\b"
    r"[^.;\n]*[.;]?"
    r"|\b(?:do\s+not|don't|dont|never|avoid|skip|no)\b"
    r"[^.;\n]{0,220}\b(?:outreach|email|reply|response|follow-up|followup|message|note)\b"
    r"[^.;\n]{0,160}\b(?:draft|write|compose|prepare|outline)\b"
    r"[^.;\n]*[.;]?",
    re.I,
)
_NEGATED_OPPORTUNITY_DISCOVERY_CLAUSE_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never|avoid|skip|no|without)\b"
    r"[^.;\n]{0,220}\b(?:scout|find|identify|search|source|discover|list|"
    r"assess|evaluate|review|qualify)\b"
    r"[^.;\n]{0,160}\b(?:opportunities?|leads?|grants?|partners?|"
    r"partnerships?|pilots?|companies?|targets?|roles?|jobs?|positions?|"
    r"postings?|openings?)\b"
    r"[^.;\n]*[.;]?",
    re.I,
)
_NEGATED_CONTEXT_SEND_CLAUSE_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never|avoid|skip|no)\b"
    r"[^.;\n]{0,260}\b(?:send|post|publish|share|schedule|email|slack\s+post)\b"
    r"[^.;\n]*[.;]?"
    r"|\bwithout\s+(?:sending|posting|publishing|sharing|scheduling)\b"
    r"[^.;\n]*[.;]?",
    re.I,
)
_CONTEXT_AGENT_ROUTES = frozenset(
    {
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
        "rss_context_agent",
        "preprints_context_agent",
    }
)
_DISCOVERY_OUTREACH_WORKFLOW_RE = re.compile(
    r"\b(?:find|identify|search|scout|source|discover|list)\b[\s\S]*?"
    r"\b(?:outreach|emails?|messages?|companies|targets?|leads?|opportunities?)\b[\s\S]*?"
    r"\b(?:draft|write|compose|prepare|send|outreach|emails?|messages?|companies|targets?|leads?)\b",
    re.I,
)
_EXPLICIT_RESEARCH_BEFORE_OUTREACH_RE = re.compile(
    r"\b(?:research|profile|assess|evaluate|look\s+into|check\s+out|"
    r"source[- ]backed\s+research|prepare\s+(?:a\s+)?research|"
    r"create\s+(?:a\s+)?research|build\s+(?:a\s+)?research)\b",
    re.I,
)
_FULL_BODY_KEYS = frozenset(
    {
        "body",
        "raw_body",
        "normalized_body",
        "email_body",
        "draft_reply",
        "draft_text",
        "thread_context",
        "raw_source_content",
    }
)
_READ_ONLY_BUSINESS_RESEARCH_TOOL_NAMES = frozenset(
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
        "file_search",
        "search_web",
        "fetch_company_page",
        "fetch_linkedin_or_profile_placeholder",
        "extract_company_signals",
        "dedupe_and_rank_sources",
        "build_source_bundle_for_synthesis",
        "synthesize_company_profile_from_source_bundle",
        "compare_company_profiles_for_decision",
    }
)
_READ_ONLY_OPPORTUNITY_SCOUT_TOOL_NAMES = frozenset(
    {
        "list_local_context_sources",
        "search_local_context",
        "read_local_context_file",
        "retrieve_memory",
        "check_workflow_duplicate",
        "search_web",
        "structure_web_data_for_schema",
        "render_page",
        "capture_browser_diagnostics",
        "summarize_rendered_page_diagnostics",
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
    }
)

LLMRouter = Callable[[str, Mapping[str, Any]], OrchestratorResult | Mapping[str, Any]]


class OrchestratorPreflight(BaseModel):
    """Orchestrator-owned front-door interpretation for manual agent execution."""

    request_text: str = ""
    requested_agent: str | None = None
    advisory_only: bool = False
    selected_agent: str = ""
    blocked_by_orchestrator: bool = False
    execution_allowed: bool = True
    block_kind: str = ""
    block_reason: str = ""
    manual_request_plan: ManualRequestPlan
    route_result: OrchestratorResult
    sdk_usage_events: list[dict[str, Any]] = Field(default_factory=list)


def run_orchestrator_preflight(
    request_text: str | Mapping[str, Any] | None,
    *,
    requested_agent: str | None = None,
    live_manual_plan: bool = False,
    run_config: Any | None = None,
    model: str | None = None,
    session: Any | None = None,
    database_url: str | None = None,
    workflow_state: Mapping[str, Any] | None = None,
) -> OrchestratorPreflight:
    """Resolve the manual plan as an Orchestrator-owned preflight step.

    Explicit specialist mentions are advisory-only: this records the
    Orchestrator interpretation and safety posture, while callers keep the named
    specialist unless the Orchestrator returns a hard refusal/blocker.
    """

    from keystone_agents.agents.manual_request_planner import resolve_manual_request_plan

    text = _payload_text(request_text)
    sdk_usage_events: list[dict[str, Any]] = []

    def record_planner_cost(sdk_result: Any) -> None:
        sdk_usage_events.append(
            _preflight_sdk_usage_event_payload(
                sdk_result,
                agent_name="manual_request_planner",
                run_stage="orchestrator_preflight.manual_request_planner",
            )
        )

    manual_plan = resolve_manual_request_plan(
        text,
        requested_agent=requested_agent,
        live=live_manual_plan,
        run_config=run_config,
        model=model,
        session=session,
        workflow_state=workflow_state,
        cost_callback=record_planner_cost,
    )
    return _build_orchestrator_preflight(
        text,
        manual_plan=manual_plan,
        database_url=database_url,
        workflow_state=workflow_state,
        sdk_usage_events=sdk_usage_events,
    )


def run_orchestrator_preflight_from_plan(
    request_text: str | Mapping[str, Any] | None,
    *,
    manual_request_plan: ManualRequestPlan | Mapping[str, Any],
    database_url: str | None = None,
    workflow_state: Mapping[str, Any] | None = None,
) -> OrchestratorPreflight:
    """Build preflight context from validated persisted semantic authority.

    Literal WorkItem continuation must not spend another planner/model request
    merely to reinterpret ``continue``. Callers use this only after selecting a
    concrete WorkItem; invalid persisted state is rejected so they can invoke
    the ordinary planner fallback explicitly.
    """

    authority = ExecutionIntentAuthority.from_value(manual_request_plan)
    if not authority.canonical or authority.plan is None:
        raise ValueError("Persisted WorkItem plan is not canonical.")
    return _build_orchestrator_preflight(
        _payload_text(request_text),
        manual_plan=authority.plan,
        database_url=database_url,
        workflow_state=workflow_state,
        sdk_usage_events=[],
    )


def _build_orchestrator_preflight(
    text: str,
    *,
    manual_plan: ManualRequestPlan,
    database_url: str | None,
    workflow_state: Mapping[str, Any] | None,
    sdk_usage_events: list[dict[str, Any]],
) -> OrchestratorPreflight:
    """Assemble one bounded preflight from the selected semantic plan."""

    route_result = route_request(
        text,
        manual_plan=manual_plan,
        database_url=database_url,
        workflow_state=workflow_state,
    )
    explicit_agent = manual_plan.requested_agent
    advisory_only = explicit_agent not in {None, "orchestrator"}
    selected_agent = str(route_result.route or manual_plan.target_agent)
    block_kind, block_reason = _preflight_block(route_result)
    execution_allowed = not bool(block_kind)
    return OrchestratorPreflight(
        request_text=text,
        requested_agent=explicit_agent,
        advisory_only=advisory_only,
        selected_agent=selected_agent,
        blocked_by_orchestrator=not execution_allowed,
        execution_allowed=execution_allowed,
        block_kind=block_kind,
        block_reason=block_reason,
        manual_request_plan=manual_plan,
        route_result=route_result,
        sdk_usage_events=sdk_usage_events,
    )


def _preflight_sdk_usage_event_payload(
    sdk_result: Any,
    *,
    agent_name: str,
    run_stage: str,
) -> dict[str, Any]:
    """Return audit-safe SDK usage metadata for WorkItem cost accounting."""

    return {
        "agent_name": agent_name,
        "run_stage": run_stage,
        "usage": dict(getattr(sdk_result, "usage", None) or {}),
        "cost": dict(getattr(sdk_result, "cost", None) or {}),
        "request_cache": dict(getattr(sdk_result, "request_cache", None) or {}),
    }


def _preflight_block(result: OrchestratorResult) -> tuple[str, str]:
    """Return an explicit execution block for front-door Orchestrator refusals."""

    if not result.refused:
        return "", ""
    combined = " ".join(
        str(part or "")
        for part in (
            result.rationale,
            result.stop_reason,
            result.clarification_request,
            " ".join(result.audit_notes),
        )
    ).lower()
    if "send gate" in combined or "email sending is not allowed" in combined:
        kind = "send"
    elif "safety gate" in combined or "phi" in combined or "security" in combined:
        kind = "safety"
    elif "approval" in combined:
        kind = "approval"
    else:
        kind = "refusal"
    reason = result.stop_reason or result.clarification_request or result.rationale
    return kind, str(reason or "Blocked by Orchestrator preflight.")


def _handoff_specs() -> list[HandoffSpec]:
    return list(INTENDED_HANDOFFS)


def _env_flag_enabled(name: str) -> bool:
    value = os.environ.get(name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", getattr(tool, "__name__", "")) or "")


def _safe_text(value: Any, *, max_chars: int = _SAFE_STATE_TEXT_CHARS) -> str:
    text = str(redact_secrets(value) if value is not None else "").replace("\u2014", "-").strip()
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        loaded = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _latest(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return list(reversed(rows))[: max(0, limit)]


def _approval_item_summary(item: Any) -> dict[str, Any]:
    mapping = _to_mapping(item) or {}
    return {
        "id": _safe_text(mapping.get("id"), max_chars=80),
        "object_type": _safe_text(mapping.get("object_type"), max_chars=80),
        "object_id": _safe_text(mapping.get("object_id"), max_chars=80),
        "status": _safe_text(
            mapping.get("approval_status") or mapping.get("status"),
            max_chars=80,
        ),
        "source_agent": _safe_text(mapping.get("source_agent"), max_chars=80),
        "title": _safe_text(mapping.get("title")),
        "summary": _safe_text(mapping.get("summary")),
    }


def _company_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = _json_dict(row.get("profile_json"))
    return {
        "id": _safe_text(row.get("id"), max_chars=40),
        "company": _safe_text(row.get("company_name") or payload.get("name")),
        "url": _safe_text(row.get("company_url") or payload.get("website")),
        "fit_score": row.get("consulting_fit_score") or payload.get("consulting_fit_score"),
        "confidence_score": row.get("confidence_score") or payload.get("confidence_score"),
        "fit_summary": _safe_text(payload.get("fit_summary")),
    }


def _opportunity_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = _json_dict(row.get("opportunity_json"))
    return {
        "id": _safe_text(row.get("id"), max_chars=40),
        "company": _safe_text(row.get("target_company") or payload.get("company_name")),
        "type": _safe_text(row.get("opportunity_type") or payload.get("opportunity_type")),
        "priority_score": row.get("priority_score") or payload.get("priority_score"),
        "status": _safe_text(row.get("status"), max_chars=80),
        "next_step": _safe_text(payload.get("recommended_next_step")),
    }


def _draft_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = _json_dict(row.get("draft_json"))
    return {
        "id": _safe_text(row.get("id"), max_chars=40),
        "company": _safe_text(row.get("company_name") or payload.get("company_name")),
        "contact": _safe_text(row.get("contact_name") or payload.get("contact_name")),
        "subject": _safe_text(row.get("email_subject") or payload.get("email_subject")),
        "approval_state": _safe_text(
            row.get("approval_state") or payload.get("approval_state"),
            max_chars=80,
        ),
    }


def _prior_route_summary(row: Mapping[str, Any]) -> dict[str, Any] | None:
    output = _json_dict(row.get("output_json"))
    if not output:
        return None
    route = output.get("route")
    if route not in _HANDOFF_BY_ROUTE and route != "clarification":
        return None
    return {
        "id": _safe_text(row.get("id"), max_chars=40),
        "route": route,
        "target_agent": _safe_text(output.get("target_agent"), max_chars=120),
        "stop_reason": _safe_text(output.get("stop_reason")),
        "created_at": _safe_text(row.get("created_at_et") or row.get("created_at_utc")),
    }


def _state_counts(store: SQLiteStore) -> dict[str, int]:
    return {
        "approval_queue": store.count("approval_queue"),
        "companies": store.count("companies"),
        "opportunities": store.count("opportunities"),
        "outreach_drafts": store.count("outreach_drafts"),
        "agent_runs": store.count("agent_runs"),
    }


def load_workflow_state_context(
    *,
    database_url: str | None = None,
    max_results: int = 5,
) -> dict[str, Any]:
    """Load a redacted workflow-state summary for routing and resume decisions."""

    store = SQLiteStore(database_url or database_url_from_env())
    limit = max(0, min(int(max_results), 20))
    approval_items = [
        _approval_item_summary(item) for item in store.list_approval_items(status=None)
    ]
    pending_approvals = [
        item for item in approval_items if str(item.get("status") or "").lower() == "pending"
    ][:limit]
    approved_approval_items = [
        item for item in approval_items if str(item.get("status") or "").lower() == "approved"
    ][:limit]
    prior_routes = [
        route
        for route in (
            _prior_route_summary(row)
            for row in _latest(store.fetch_all("agent_runs"), limit)
            if str(row.get("agent_name") or "") == "orchestrator"
        )
        if route is not None
    ]
    context = {
        "counts": _state_counts(store),
        "pending_approvals": pending_approvals,
        "approved_approval_items": approved_approval_items,
        "companies": [
            _company_summary(row) for row in _latest(store.fetch_all("companies"), limit)
        ],
        "opportunities": [
            _opportunity_summary(row) for row in _latest(store.fetch_all("opportunities"), limit)
        ],
        "outreach_drafts": [
            _draft_summary(row) for row in _latest(store.fetch_all("outreach_drafts"), limit)
        ],
        "prior_route_decisions": prior_routes,
        "send_enabled": False,
    }
    return _normalize_workflow_state(context)


def _normalize_workflow_state(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {
            "counts": {},
            "pending_approvals": [],
            "approved_approval_items": [],
            "companies": [],
            "opportunities": [],
            "outreach_drafts": [],
            "prior_route_decisions": [],
            "recent_slack_thread": [],
            "prior_agent_runs": [],
            "channel_automations": [],
            "slack_context": {},
            "approved_context_available": False,
            "send_enabled": False,
        }
    context = dict(value)
    approval_items = list(context.get("approval_items") or [])
    approved_items = list(context.get("approved_approval_items") or [])
    pending_items = list(context.get("pending_approvals") or [])
    for item in approval_items:
        mapping = _to_mapping(item) or {}
        status = str(mapping.get("approval_status") or mapping.get("status") or "").lower()
        if status == "pending":
            pending_items.append(item)
        elif status == "approved":
            approved_items.append(item)
    normalized = {
        "counts": dict(context.get("counts") or {}),
        "pending_approvals": [_approval_item_summary(item) for item in pending_items],
        "approved_approval_items": [_approval_item_summary(item) for item in approved_items],
        "companies": list(context.get("companies") or []),
        "opportunities": list(context.get("opportunities") or []),
        "outreach_drafts": list(context.get("outreach_drafts") or []),
        "prior_route_decisions": list(context.get("prior_route_decisions") or []),
        "recent_slack_thread": list(context.get("recent_slack_thread") or []),
        "prior_agent_runs": list(context.get("prior_agent_runs") or []),
        "channel_automations": list(context.get("channel_automations") or []),
        "slack_context": dict(context.get("slack_context") or {}),
        "send_enabled": False,
    }
    normalized["approved_context_available"] = bool(
        context.get("approved_context_available") or _state_has_approved_context(normalized)
    )
    return normalized


def _merge_workflow_state_context(
    base: Mapping[str, Any] | None,
    overlay: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge persisted workflow state with caller-supplied context."""

    normalized_base = _normalize_workflow_state(base)
    if not overlay:
        return normalized_base
    normalized_overlay = _normalize_workflow_state(overlay)
    merged = dict(normalized_base)
    merged["counts"] = {
        **dict(normalized_base.get("counts") or {}),
        **dict(normalized_overlay.get("counts") or {}),
    }
    for key in (
        "pending_approvals",
        "approved_approval_items",
        "companies",
        "opportunities",
        "outreach_drafts",
        "prior_route_decisions",
        "recent_slack_thread",
        "prior_agent_runs",
        "channel_automations",
    ):
        merged[key] = [
            *list(normalized_overlay.get(key) or []),
            *list(normalized_base.get(key) or []),
        ]
    if normalized_overlay.get("slack_context"):
        merged["slack_context"] = normalized_overlay["slack_context"]
    merged["approved_context_available"] = bool(
        normalized_base.get("approved_context_available")
        or normalized_overlay.get("approved_context_available")
        or _state_has_approved_context(merged)
    )
    merged["send_enabled"] = False
    return _normalize_workflow_state(merged)


def _workflow_state_is_empty(state: Mapping[str, Any] | None) -> bool:
    if not state:
        return True
    return (
        not any(
            state.get(key)
            for key in (
                "pending_approvals",
                "approved_approval_items",
                "companies",
                "opportunities",
                "outreach_drafts",
                "prior_route_decisions",
                "recent_slack_thread",
                "prior_agent_runs",
                "channel_automations",
            )
        )
        and not bool(state.get("counts"))
        and not bool(state.get("slack_context"))
    )


def _state_has_pending_approval(state: Mapping[str, Any]) -> bool:
    return bool(state.get("pending_approvals"))


def _state_has_approved_context(state: Mapping[str, Any]) -> bool:
    for item in state.get("approved_approval_items") or []:
        mapping = _to_mapping(item) or {}
        object_type = str(mapping.get("object_type") or "").lower()
        if object_type in _APPROVED_CONTEXT_OBJECT_TYPES:
            return True
    return False


def _request_has_inline_approved_outreach_context(text: str) -> bool:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return False
    if _looks_like_send_side_effect(cleaned):
        return False
    if not _OUTREACH_RE.search(cleaned) and not _looks_like_email(None, cleaned):
        return False
    if not re.search(r"\b(?:draft|write|compose|prepare)\b", cleaned, flags=re.I):
        return False
    approved_context_label = re.search(
        r"\b(?:"
        r"(?:these\s+|the\s+following\s+)?approved"
        r"(?:\s+(?:inline|source|source-backed|source backed))?\s+"
        r"(?:context|facts|evidence|background|grounding|rationale)"
        r"|source[-\s]+backed\s+(?:context|facts|evidence|background|grounding)"
        r"|context\s+approved\s+for\s+(?:drafting|draft-only\s+use|draft\s+only\s+use)"
        r")\s*:",
        cleaned,
        flags=re.I,
    )
    if not approved_context_label:
        return False
    return bool(
        re.search(r"\b(?:no send|draft-only|draft only)\b", cleaned, flags=re.I)
        or re.search(
            r"\b(?:do\s+not|don't|dont|never|without)\b"
            r"[^.;\n]{0,200}\b(?:send|post|publish|share)\b",
            cleaned,
            flags=re.I,
        )
    )


def _to_mapping(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump()
    return None


def _is_approved_context(value: Any, *, explicit_approved: bool = False) -> bool:
    if value is None:
        return False
    if explicit_approved:
        return True
    mapping = _to_mapping(value)
    if mapping is None:
        return isinstance(value, CompanyProfile)

    status = (
        str(
            mapping.get("approval_state")
            or mapping.get("approval_status")
            or mapping.get("decision")
            or mapping.get("status")
            or mapping.get("approval")
            or ""
        )
        .strip()
        .lower()
    )
    try:
        if state_allows_drafting(status):
            return True
    except ValueError:
        pass
    return mapping.get("approved") is True or mapping.get("human_approved") is True


def _result(
    *,
    route: RouteName,
    rationale: str,
    approved_context_present: bool = False,
    refused: bool = False,
    stop_reason: str | None = None,
    clarification_request: str | None = None,
    artifact: tuple[str, str] | None = None,
    approval_scope: ApprovalScope = ApprovalScope.DRAFTING,
    approval_state: ApprovalState = ApprovalState.PENDING,
    approval_required: bool = True,
    requires_human_review: bool = True,
    approval_rationale: str = "",
    routing_mode: str = "deterministic",
    workflow_state: Mapping[str, Any] | None = None,
    audit_notes: list[str] | None = None,
    retrieval_hint: RetrievalHint | None = None,
    workflow: list[str] | None = None,
) -> OrchestratorResult:
    handoff = _HANDOFF_BY_ROUTE.get(route)
    artifacts = {artifact[0]: artifact[1]} if artifact else {}
    workflow_steps = (
        workflow if workflow is not None else ([route] if route != "clarification" else [])
    )
    state_summary = dict(workflow_state or {})
    notes = [
        "Fixture-mode Python routing fallback is available.",
        "Email sending is disabled for every route.",
        "SDK handoffs are explicit intended transfers; no specialist handoff runs here.",
    ]
    notes.extend(audit_notes or [])
    rejected_routes = [handoff.route for handoff in INTENDED_HANDOFFS if handoff.route != route]
    blockers = [clarification_request] if clarification_request else []
    if stop_reason:
        blockers.append(stop_reason)
    return OrchestratorResult(
        route=route,
        target_agent=handoff.agent_name if handoff else None,
        workflow=workflow_steps,
        routing_mode=routing_mode,
        rationale=rationale,
        clarification_request=clarification_request,
        stop_reason=stop_reason,
        requires_human_review=requires_human_review,
        approval_required=approval_required,
        approval_state=approval_state,
        approval_scope=approval_scope,
        approval_rationale=approval_rationale,
        external_use_approval_required=approval_scope == ApprovalScope.EXTERNAL_USE,
        approved_context_present=approved_context_present,
        refused=refused,
        send_enabled=False,
        can_send_email=False,
        intended_handoffs=_handoff_specs(),
        retrieval_hint=retrieval_hint,
        artifacts=artifacts,
        state_context_used=not _workflow_state_is_empty(state_summary),
        workflow_state_summary=state_summary,
        decision_trace=DecisionTrace(
            selected_route=route,
            rejected_routes=rejected_routes,
            safety_gates_applied=[
                "no_send_enforced",
                *(
                    ["approval_required"]
                    if approval_required
                    else ["read_only_no_approval_required"]
                ),
                "guardrail_assessment",
            ],
            missing_information_blockers=blockers,
            handoff_readiness=(
                "ready_for_sdk_handoff"
                if handoff and not refused
                else "requires_human_clarification"
            ),
            notes=notes,
        ),
        audit_notes=notes,
    )


def _looks_like_crm_write_request(text: str) -> bool:
    cleaned = _NEGATED_CRM_WRITE_CLAUSE_RE.sub(" ", str(text or ""))
    lowered = " ".join(cleaned.lower().split())
    if "airtable" in lowered and any(
        marker in lowered
        for marker in (
            "finance_tax_tracker",
            "finance tax tracker",
            "tax tracker",
            "business expense",
            "business expenses",
            "personal expense",
            "personal expenses",
        )
    ):
        return False
    return bool(_CRM_WRITE_RE.search(cleaned))


def _without_negated_outreach_draft_clauses(text: str) -> str:
    return positive_capability_text(text)


def _without_negated_route_action_clauses(text: str) -> str:
    return positive_capability_text(text)


def _without_negated_context_send_clauses(text: str) -> str:
    return _NEGATED_CONTEXT_SEND_CLAUSE_RE.sub(" ", str(text or ""))


def _with_crm_write_boundary(
    result: OrchestratorResult,
    *,
    request_text: str,
) -> OrchestratorResult:
    if not _looks_like_crm_write_request(request_text):
        return result
    payload = result.model_dump(mode="json")
    payload["workflow"] = list(dict.fromkeys([*(payload.get("workflow") or []), "crm_preflight"]))
    payload["forbidden_actions"] = list(
        dict.fromkeys([*(payload.get("forbidden_actions") or []), "save_to_crm", "crm_write"])
    )
    payload["approval_required"] = True
    payload["approval_scope"] = ApprovalScope.EXTERNAL_USE.value
    payload["external_use_approval_required"] = True
    payload["send_enabled"] = False
    payload["can_send_email"] = False
    payload["approval_rationale"] = (
        "The request asks to save or write to CRM; Orchestrator converted that into "
        "draft-only CRM-ready fields pending explicit external-use approval."
    )
    note = (
        "CRM save/write request treated as draft-only approval-gated boundary; "
        "no CRM write was performed."
    )
    payload["audit_notes"] = list(dict.fromkeys([*(payload.get("audit_notes") or []), note]))
    decision_trace = payload.get("decision_trace") or {}
    if isinstance(decision_trace, dict):
        decision_trace["safety_gates_applied"] = list(
            dict.fromkeys(
                [
                    *(decision_trace.get("safety_gates_applied") or []),
                    "crm_write_blocked_pending_approval",
                ]
            )
        )
        decision_trace["notes"] = list(dict.fromkeys([*(decision_trace.get("notes") or []), note]))
        payload["decision_trace"] = decision_trace
    return OrchestratorResult.model_validate(payload)


def _operator_feedback_object_id(result: OrchestratorResult) -> str:
    artifacts = (
        result.artifacts.model_dump(mode="json")
        if isinstance(result.artifacts, BaseModel)
        else result.artifacts
        if isinstance(result.artifacts, Mapping)
        else {}
    )
    state = (
        result.workflow_state_summary.model_dump(mode="json")
        if isinstance(result.workflow_state_summary, BaseModel)
        else result.workflow_state_summary
        if isinstance(result.workflow_state_summary, Mapping)
        else {}
    )
    for key in (
        "object_id",
        "draft_id",
        "opportunity_id",
        "company_id",
        "message_id",
        "latest_artifact_id",
    ):
        value = artifacts.get(key) or state.get(key)
        if value not in {None, ""}:
            return str(value)
    return result.route


def _with_operator_feedback_request(
    result: OrchestratorResult,
    *,
    include: bool,
) -> OrchestratorResult:
    if not include or result.operator_feedback_request is not None:
        return result
    if result.route == "clarification":
        return result
    object_type = _FEEDBACK_OBJECT_TYPE_BY_ROUTE.get(result.route, "other")
    feedback_request = build_operator_feedback_request(
        object_type=object_type,
        object_id=_operator_feedback_object_id(result),
        source_agent=result.target_agent or "orchestrator",
        review_stage="approval_review",
        approval_question=(
            "What should the operator approve, revise, or reject for this routed step?"
        ),
    )
    return result.model_copy(
        update={
            "operator_feedback_request": feedback_request,
            "audit_notes": [
                *result.audit_notes,
                "Optional operator feedback request attached for review-quality learning.",
            ],
        }
    )


def _route_retrieval_hint(
    route: RouteName,
    *,
    request_text: str,
    current_hint: RetrievalHint | None = None,
) -> RetrievalHint | None:
    if current_hint is not None:
        return current_hint
    if route not in {"business_research_analyst", "opportunity_scout"}:
        return None
    agent_name = (
        "business_research_analyst" if route == "business_research_analyst" else "opportunity_scout"
    )
    hint = derive_request_autonomy_hint(agent_name=agent_name, request_text=request_text)
    return RetrievalHint.model_validate(hint.to_dict())


def _hard_safety_refusal(
    text: str,
    *,
    approved_context_present: bool,
    workflow_state: Mapping[str, Any],
    owning_route: RouteName = "clarification",
) -> OrchestratorResult | None:
    assessment = assess_text_guardrails(text, check_outreach_claims=False)
    blocking_flags = sorted(_HARD_REFUSAL_FLAGS.intersection(assessment.risk_flags))
    if not blocking_flags:
        return None
    reasons = ", ".join(assessment.reasons) or "safety policy"
    return _result(
        route=owning_route,
        rationale="The request is blocked by deterministic Keystone safety gates.",
        refused=True,
        stop_reason=f"Blocked by Python safety gate: {reasons}.",
        clarification_request=(
            "Outreach blocked by safety gate\n\n"
            "*Answer:*\n"
            "I cannot route this to a specialist while the request includes possible PHI, "
            "patient-specific content, or other protected material.\n\n"
            "*Detailed Summary:*\n"
            f"* Blocked reason: {reasons}.\n"
            "* Remove PHI, named patient details, security-sensitive content, legal claims, "
            "and professional-advice content before rerunning.\n"
            "* No draft, send, post, CRM write, file write, schedule, publish, or external "
            "action was taken.\n\n"
            "*Next step:*\n"
            "Resubmit with sanitized, non-patient-specific context and explicit approval "
            "scope for any draft-only outreach."
        ),
        approved_context_present=approved_context_present,
        approval_scope=ApprovalScope.DRAFTING,
        approval_rationale="Safety review is required before any specialist workflow can continue.",
        artifact=("risk_flags", ",".join(blocking_flags)),
        workflow_state=workflow_state,
        audit_notes=["Hard Python safety gate ran before any LLM routing."],
    )


def _send_refusal(
    *,
    approved_context_present: bool,
    workflow_state: Mapping[str, Any],
) -> OrchestratorResult:
    return _result(
        route="clarification",
        rationale=(
            "The request asks for an external send, post, or live action, which is outside "
            "the v1 safety boundary without explicit approval."
        ),
        refused=True,
        stop_reason=(
            "Email sending is not allowed from this dry-run eval. External sending, posting, "
            "or live API use is not allowed; the orchestrator can only route to draft-only "
            "workflows after human approval."
        ),
        clarification_request=(
            "Confirm whether you want a draft-only workflow for human approval; no live APIs, "
            "Slack post, Gmail draft, send, or external write was performed."
        ),
        approved_context_present=approved_context_present,
        approval_scope=ApprovalScope.EXTERNAL_USE,
        approval_rationale=(
            "External-use approval is required and does not enable automatic email sending "
            "or posting from this dry-run eval."
        ),
        workflow_state=workflow_state,
        audit_notes=["Hard Python send gate ran before any LLM routing; no live APIs were called."],
    )


def _looks_like_discovery_outreach_workflow(text: str) -> bool:
    return bool(_DISCOVERY_OUTREACH_WORKFLOW_RE.search(_without_negated_route_action_clauses(text)))


def _gmail_cross_agent_workflow(text: str) -> list[str]:
    lower = positive_capability_text(text).lower()
    if not re.search(r"\b(?:gmail|email|inquiry|thread|reply|response)\b", lower):
        return []
    workflow = ["gmail_triage"]
    has_research = bool(
        re.search(
            r"\b(?:research\s+(?:the\s+)?company|research\s+it|company\s+research|"
            r"company\s+profile|source-backed|source\s+backed|evidence-backed)\b",
            lower,
        )
    )
    has_opportunity = bool(
        re.search(
            r"\b(?:create|save|add|prepare|record)\b[^.\n]{0,160}"
            r"\b(?:opportunit|crm|record|pipeline)\b",
            lower,
        )
        or re.search(r"\bopportunity record\b", lower)
    )
    has_draft_after_context = bool(
        re.search(r"\b(?:draft|write|compose|prepare)\b[^.\n]{0,160}\boutreach\b", lower)
        or (
            (has_research or has_opportunity)
            and re.search(
                r"\b(?:draft|write|compose|prepare)\b[^.\n]{0,160}\b(?:response|reply)\b",
                lower,
            )
        )
    )
    if has_research:
        workflow.append("business_research_analyst")
    if has_opportunity:
        workflow.append("opportunity_scout")
    if has_draft_after_context:
        workflow.append("outreach_composer")
    return list(dict.fromkeys(workflow)) if len(workflow) > 1 else []


def _requested_cross_agent_workflow(text: str, *, start_route: str) -> list[str]:
    lower = _without_negated_outreach_draft_clauses(text).lower()
    workflow: list[str] = []
    if re.search(r"\b(?:gmail|email)\s+context\b|\bcheck\s+recent\s+gmail\b", lower):
        workflow.append("gmail_triage")
    if re.search(
        r"\b(?:company\s+research|research\s+(?:it|the\s+company|company)|"
        r"research\s+summary|research\s+brief|source[- ]backed|profile\s+company)\b",
        lower,
    ) or re.search(
        r"\bbusiness\s+research(?:\s+analyst|\s+agent)?\b"
        r"[^.;\n]{0,120}\b(?:summari[sz]e|review|analy[sz]e|assess|brief)\b",
        lower,
    ):
        workflow.append("business_research_analyst")
    if re.search(
        r"\b(?:opportunit|active\s+roles?|advisory\s+openings?|"
        r"partnership\s+angles?|opportunity\s+scan|similar\s+companies)\b",
        lower,
    ):
        workflow.append("opportunity_scout")
    if re.search(
        r"\b(?:draft|write|compose|prepare)\b[\s\S]{0,160}\b"
        r"(?:outreach|email|reply|response|follow-up|followup)\b"
        r"|\boutreach\s+(?:recommendation|draft)\b",
        lower,
    ) or re.search(
        r"\boutreach\s+composer\b[^.;\n]{0,120}"
        r"\b(?:draft|write|compose|prepare)\b",
        lower,
    ):
        workflow.append("outreach_composer")
    if re.search(r"\b(?:scorecard|score\s+the\s+workflow|approval\s+checklist)\b", lower):
        workflow.append("chief_of_staff")
    if not workflow:
        return []
    if start_route not in workflow and start_route != "clarification":
        workflow.insert(0, start_route)
    return list(dict.fromkeys(workflow))


def _mixed_outreach_request_requires_context_gate(
    text: str,
    *,
    approved_context_present: bool,
) -> bool:
    if approved_context_present:
        return False
    scrubbed = _without_negated_outreach_draft_clauses(text)
    lower = scrubbed.lower()
    if re.search(r"\b(?:score\s+the\s+workflow|coordinate\s+the\s+agents)\b", lower):
        return False
    if re.search(r"\b(?:not|no)\s+outreach\b", lower):
        return False
    if not re.search(
        r"\b(?:draft|write|compose|prepare|outline)\b[\s\S]{0,160}\b"
        r"(?:outreach|email|reply|response|follow-up|followup|next\s+steps?)\b",
        lower,
    ):
        return False
    return _requires_research_before_outreach(scrubbed)


def _requires_research_before_outreach(text: str) -> bool:
    cleaned = str(text or "")
    lower = cleaned.lower()
    if "attached research brief" in lower or "provided research brief" in lower:
        return False
    normalized = re.sub(
        r"\b(?:same\s+company\s+research\s+brief|company\s+research\s+brief|"
        r"research\s+brief|clinical\s+research|outcomes\s+research)\b",
        " ",
        cleaned,
        flags=re.I,
    )
    return bool(_EXPLICIT_RESEARCH_BEFORE_OUTREACH_RE.search(normalized))


def _looks_like_explicit_business_research_request(text: str) -> bool:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return False
    return bool(
        re.search(
            r"\b(?:run|use|ask|call|have|route\s+to|send\s+to)?\s*"
            r"business\s+research(?:\s+analyst|\s+agent)?\b",
            cleaned,
            flags=re.I,
        )
        and re.search(
            r"\b(?:research|source-backed|source\s+backed|company|profile|"
            r"evidence[- ]?packet|evidence\s+planning|planning\s+note|brief)\b",
            cleaned,
            flags=re.I,
        )
    )


def _safe_discovery_outreach_workflow_result(
    *,
    request_text: str,
    approved_context_present: bool,
    workflow_state: Mapping[str, Any],
) -> OrchestratorResult:
    send_blocked = _looks_like_send_side_effect(request_text)
    result = _result(
        route="opportunity_scout",
        rationale=(
            "The request combines opportunity discovery with outreach. Orchestrator "
            "will start with Opportunity Scout and preserve the downstream research and "
            "draft-only outreach steps while blocking any send action."
        ),
        approved_context_present=approved_context_present,
        approval_scope=ApprovalScope.EXTERNAL_USE,
        approval_rationale=(
            "Discovery and draft preparation may proceed as a draft-only workflow; "
            "external sending remains forbidden and requires separate human action."
        ),
        workflow_state=workflow_state,
        audit_notes=[
            "Compound discovery/outreach request preserved as a draft-only workflow.",
            "Automatic sending remains blocked.",
        ],
        retrieval_hint=_route_retrieval_hint(
            "opportunity_scout",
            request_text=request_text,
        ),
    )
    payload = result.model_dump(mode="json")
    workflow = ["opportunity_scout", "business_research_analyst", "outreach_composer"]
    if send_blocked:
        workflow.append("send_blocked")
    payload["workflow"] = workflow
    payload["forbidden_actions"] = list(
        dict.fromkeys([*(payload.get("forbidden_actions") or []), "send_email"])
    )
    payload["send_enabled"] = False
    payload["can_send_email"] = False
    decision_trace = payload.get("decision_trace") or {}
    if isinstance(decision_trace, dict):
        decision_trace["safety_gates_applied"] = list(
            dict.fromkeys(
                [
                    *(decision_trace.get("safety_gates_applied") or []),
                    "send_blocked_draft_only_workflow_preserved",
                ]
            )
        )
        decision_trace["notes"] = list(
            dict.fromkeys(
                [
                    *(decision_trace.get("notes") or []),
                    "Safe workflow sequence preserved: scout, research, draft-only outreach.",
                ]
            )
        )
        payload["decision_trace"] = decision_trace
    return OrchestratorResult.model_validate(payload)


def _research_first_outreach_workflow_result(
    *,
    request_text: str,
    workflow_state: Mapping[str, Any],
    routing_mode: str = "deterministic",
    audit_notes: list[str] | None = None,
) -> OrchestratorResult:
    return _result(
        route="business_research_analyst",
        rationale=(
            "The request asks for outreach after research; research must run first so "
            "drafting can use approved, source-backed context."
        ),
        approval_scope=ApprovalScope.DRAFTING,
        approval_rationale=(
            "Draft-only outreach is downstream of source-backed research and still requires "
            "human approval before any external use."
        ),
        routing_mode=routing_mode,
        workflow_state=workflow_state,
        audit_notes=[
            *(audit_notes or []),
            "Safe workflow sequence preserved: research, draft-only outreach.",
        ],
        retrieval_hint=_route_retrieval_hint(
            "business_research_analyst",
            request_text=request_text,
        ),
        workflow=["business_research_analyst", "outreach_composer"],
    )


def _missing_outreach_context_refusal(
    *,
    workflow_state: Mapping[str, Any],
    owning_route: RouteName = "outreach_composer",
) -> OrchestratorResult:
    if owning_route == "business_research_analyst":
        return _result(
            route=owning_route,
            rationale=(
                "The research-first workflow is retained, but the target must be "
                "identified before research can produce approved outreach context."
            ),
            refused=True,
            stop_reason=(
                "An exact company or vendor target is required before research; "
                "approved source-backed context is required before outreach drafting."
            ),
            clarification_request=(
                "Which company or vendor should I review?\n\n"
                "*Answer:*\n"
                "I can keep the full research, fit assessment, and draft-only next-step "
                "workflow, but I need the exact target before searching or drafting.\n\n"
                "*What I need:*\n"
                "* Target: company name, URL, or the selected thread item.\n"
                "* Boundary: research comes first; any outreach uses only approved "
                "source-backed findings and remains draft-only.\n"
                "* No search, draft, send, post, CRM write, file write, schedule, "
                "publish, or external action has been taken.\n\n"
                "*Reply with:*\n"
                "\"Target: ...\""
            ),
            approval_scope=ApprovalScope.DRAFTING,
            approval_rationale=(
                "Research may start after target resolution; outreach drafting remains "
                "blocked until approved source-backed context exists."
            ),
            workflow_state=workflow_state,
            workflow=["business_research_analyst", "outreach_composer"],
            audit_notes=[
                "Target resolution stopped the retained research-first outreach workflow."
            ],
        )
    return _result(
        route=owning_route,
        rationale="Outreach drafting requires approved company or opportunity context.",
        refused=True,
        stop_reason=(
            "Approved CompanyProfile or approved OpportunityRecord, plus recipient/contact "
            "context, is required before outreach."
        ),
        clarification_request=(
            "What should this outreach focus on?\n\n"
            "*Answer:*\n"
            "I can keep this as draft-only thread help, but I need the intended focus and "
            "either the recipient or target organization before writing copy that could be "
            "used externally.\n\n"
            "*What I need:*\n"
            "* Focus: what the email or message should accomplish.\n"
            "* Target: recipient, target contact, or target organization.\n"
            "* Evidence: approved source-backed company or opportunity context, or permission "
            "to use the context already in this thread.\n"
            "* Boundary: no email, Gmail draft, Slack post outside this thread, CRM record, "
            "file write, send, schedule, publish, or external action has been taken.\n\n"
            "*Reply with:*\n"
            "\"Focus: ...; target: ...; use these source-backed facts: ...\""
        ),
        approval_scope=ApprovalScope.DRAFTING,
        approval_rationale=(
            "Draft-only outreach is blocked until a human-approved drafting context is present."
        ),
        workflow_state=workflow_state,
        audit_notes=["Hard Python approval-context gate blocked outreach routing."],
    )


def _resume_from_state_result(
    text: str,
    *,
    workflow_state: Mapping[str, Any],
) -> OrchestratorResult | None:
    if not _looks_like_resume_request(text):
        return None
    if _has_non_resume_followup_request(text) or not _looks_like_pure_resume_request(text):
        return None
    if _state_has_pending_approval(workflow_state):
        return _result(
            route="clarification",
            rationale="The workflow has a pending approval gate.",
            refused=True,
            stop_reason="Pending approval gate must be resolved before continuing.",
            clarification_request="Review the pending approval item, then resume the workflow.",
            approval_scope=ApprovalScope.DRAFTING,
            approval_rationale="The next workflow step is blocked by missing approval.",
            artifact=("state_gate", "pending_approval"),
            workflow_state=workflow_state,
            audit_notes=["Resume-from-state stopped at a persisted approval gate."],
        )
    drafts = workflow_state.get("outreach_drafts") or []
    if drafts:
        return _result(
            route="clarification",
            rationale="A prior outreach draft exists and remains review-only.",
            stop_reason="Existing outreach draft remains pending external-use approval.",
            clarification_request="Review or revise the existing draft before any external use.",
            approval_scope=ApprovalScope.EXTERNAL_USE,
            approval_rationale=(
                "Existing drafts stay internal until external-use approval is recorded."
            ),
            artifact=("resume_artifact", "outreach_draft"),
            workflow_state=workflow_state,
            audit_notes=["Resume-from-state found a draft artifact and held the approval gate."],
        )
    for prior in workflow_state.get("prior_route_decisions") or []:
        mapping = _to_mapping(prior) or {}
        route = mapping.get("route")
        if route in _HANDOFF_BY_ROUTE:
            return _result(
                route=route,
                rationale=f"Resuming prior orchestrator route `{route}` from workflow state.",
                artifact=("resumed_from_route", str(route)),
                workflow_state=workflow_state,
                audit_notes=["Resume-from-state reused an explicit prior route decision."],
            )
    return None


def _latest_followup_request_text(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    lower = value.lower()
    best_index = -1
    best_marker = ""
    for marker in _FOLLOWUP_MARKERS:
        index = lower.rfind(marker)
        if index > best_index:
            best_index = index
            best_marker = marker
    if best_index < 0:
        return value
    latest = value[best_index + len(best_marker) :].strip()
    return latest or value


def _has_non_resume_followup_request(text: str) -> bool:
    latest = _latest_followup_request_text(text)
    value = str(text or "").strip()
    if not latest or latest == value:
        return False
    return not _looks_like_resume_request(latest)


def _looks_like_pure_resume_request(text: str) -> bool:
    return bool(_PURE_RESUME_REQUEST_RE.match(str(text or "").strip()))


def _llm_route_prompt(
    request: str | Mapping[str, Any] | None,
    workflow_state: Mapping[str, Any],
) -> str:
    payload = {
        "request": redact_secrets(request),
        "workflow_state": workflow_state,
        "allowed_routes": [handoff.route for handoff in INTENDED_HANDOFFS] + ["clarification"],
        "hard_rules": [
            "Never send email or mark send_enabled true.",
            "Outreach Composer requires approved company or opportunity context.",
            "Use clarification for missing approvals, unsafe requests, or insufficient context.",
            "For business_research_analyst or opportunity_scout, you may optionally include "
            "retrieval_hint with precision, structured-enrichment, and search-review "
            "recommendations.",
            "Do not choose search providers; shared Python retrieval applies SearXNG plus "
            "capped Agents hosted web search unless the operator explicitly selected one.",
            "Return only an OrchestratorResult-compatible structured decision.",
        ],
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def _coerce_route_candidate(value: OrchestratorResult | Mapping[str, Any]) -> OrchestratorResult:
    if isinstance(value, OrchestratorResult):
        return value
    return OrchestratorResult.model_validate(dict(value))


def _review_status(score: int) -> OutputReviewStatus:
    if score >= 85:
        return "pass"
    if score >= 60:
        return "partial"
    return "fail"


def _review_score(
    dimension: str,
    score: int,
    rationale: str,
    suggestions: list[str] | None = None,
) -> OrchestratorOutputReviewScore:
    bounded = max(0, min(int(score), 100))
    return OrchestratorOutputReviewScore(
        dimension=dimension,  # type: ignore[arg-type]
        score=bounded,
        status=_review_status(bounded),
        rationale=rationale,
        suggestions=suggestions or [],
    )


def _to_plain_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _truthy_send_side_effects(value: Any, *, parent_key: str = "") -> list[str]:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        hits: list[str] = []
        for key, item in value.items():
            key_text = str(key)
            path = f"{parent_key}.{key_text}" if parent_key else key_text
            if key_text in _SEND_SIDE_EFFECT_KEYS and bool(item):
                hits.append(path)
            hits.extend(_truthy_send_side_effects(item, parent_key=path))
        return hits
    if isinstance(value, list | tuple):
        hits: list[str] = []
        for index, item in enumerate(value):
            path = f"{parent_key}[{index}]" if parent_key else f"[{index}]"
            hits.extend(_truthy_send_side_effects(item, parent_key=path))
        return hits
    return []


def _safe_review_text(value: Any) -> str:
    text = json.dumps(redact_secrets(value), ensure_ascii=False, sort_keys=True, default=str)
    if len(text) <= _REVIEW_TEXT_CHARS:
        return text
    return text[:_REVIEW_TEXT_CHARS]


def _compact_review_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if key in _FULL_BODY_KEYS and value:
        return "[omitted: full body is not included in orchestrator review]"
    if depth > 5:
        return "[omitted: nested content depth limit]"
    if isinstance(value, Mapping):
        compact: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= _REVIEW_MAPPING_KEYS:
                compact["_omitted_keys"] = "additional keys omitted for review cost guard"
                break
            key_text = str(raw_key)
            compact[key_text] = _compact_review_value(item, key=key_text, depth=depth + 1)
        return compact
    if isinstance(value, list | tuple):
        items = [
            _compact_review_value(item, key=key, depth=depth + 1)
            for item in list(value)[:_REVIEW_LIST_ITEMS]
        ]
        if len(value) > _REVIEW_LIST_ITEMS:
            items.append("[omitted: additional list items omitted for review cost guard]")
        return items
    if isinstance(value, str):
        text = _safe_text(redact_secrets(value), max_chars=_REVIEW_FIELD_CHARS)
        if len(text) > _REVIEW_FIELD_CHARS:
            return f"{text[: _REVIEW_FIELD_CHARS - 3].rstrip()}..."
        return text
    return redact_secrets(value)


def _safe_review_payload(
    *,
    agent_name: str,
    output: Any,
    request_summary: str,
    run_type: str,
    deterministic_review: OrchestratorOutputReview,
) -> dict[str, Any]:
    data = _to_plain_mapping(output)
    compact_output = _compact_review_value(data)
    return {
        "review_task": (
            "Evaluate whether this specialist output is professional, structured for "
            "human Keystone Neuroinformatics review, readable, relevant, and free of "
            "unnecessary non-human metadata."
        ),
        "agent_name": _review_agent_key(agent_name),
        "request_summary": _safe_text(request_summary, max_chars=400),
        "run_type": _safe_text(run_type, max_chars=120),
        "output": compact_output,
        "deterministic_review": deterministic_review.model_dump(mode="json"),
        "cost_guard": {
            "max_serialized_chars": _REVIEW_TEXT_CHARS,
            "max_string_chars": _REVIEW_FIELD_CHARS,
            "max_list_items": _REVIEW_LIST_ITEMS,
            "max_mapping_keys": _REVIEW_MAPPING_KEYS,
            "full_body_fields_omitted": sorted(_FULL_BODY_KEYS),
        },
        "hard_rules": [
            "Never enable sending or external side effects.",
            "Only evaluate structure, relevance, human readability, professional tone, "
            "and whether metadata is useful to a Keystone operator.",
            "Do not rewrite or expand the specialist output.",
            "Penalize raw prompts, raw model payloads, traces, tool call dumps, and "
            "debug metadata unless directly useful for a human audit.",
            "Return only an OrchestratorOutputReview-compatible JSON object.",
        ],
    }


def _field_present(data: Mapping[str, Any], *keys: str) -> bool:
    return any(data.get(key) not in (None, "", [], {}) for key in keys)


def _human_metadata_score(data: Mapping[str, Any]) -> tuple[int, list[str]]:
    if not data:
        return 50, ["Return a human-readable structured artifact, not an empty payload."]
    keys = {str(key) for key in data}
    non_human_keys = sorted(keys.intersection(_NON_HUMAN_METADATA_KEYS))
    if not non_human_keys:
        return 100, []
    ratio = len(non_human_keys) / max(len(keys), 1)
    score = 85 if ratio <= 0.15 else 65 if ratio <= 0.3 else 45
    return score, [
        "Hide or summarize non-human metadata unless it helps a Keystone operator: "
        + ", ".join(non_human_keys[:5])
        + ("..." if len(non_human_keys) > 5 else "")
    ]


def _record_field_coverage(records: list[Any], *keys: str) -> float:
    if not records:
        return 0.0
    covered = 0
    total = 0
    for record in records:
        mapping = _to_plain_mapping(record)
        if not mapping:
            continue
        for key in keys:
            total += 1
            if mapping.get(key) not in (None, "", [], {}):
                covered += 1
    return covered / total if total else 0.0


def _string_items(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list | tuple):
        return [text for item in value if (text := str(item).strip())]
    return []


def _scout_no_result_explained(data: Mapping[str, Any]) -> bool:
    records = list(data.get("records") or [])
    if records or data.get("outreach_generated") not in {None, False}:
        return False
    notes = [
        *_string_items(data.get("audit_notes")),
        *_string_items(data.get("source_bundle_quality_notes")),
    ]
    note_text = " ".join(notes).lower()
    has_explanation = any(
        phrase in note_text
        for phrase in (
            "strict hard filters",
            "hard filters removed",
            "filtered out",
            "no candidates satisfied",
            "no verified candidates",
            "insufficient evidence",
        )
    )
    raw_count = data.get("raw_search_result_count")
    deduped_count = data.get("deduped_candidate_count")
    search_attempted = (
        isinstance(raw_count, int)
        and raw_count > 0
        and isinstance(deduped_count, int)
        and deduped_count == 0
    ) or any(
        signal in note_text
        for signal in ("live search provider was used", "ran ", "source bundles were used")
    )
    clarification_block = any(
        phrase in note_text
        for phrase in (
            "blocked for clarification",
            "missing buyer",
            "missing buyer, geography",
            "live-search approval",
        )
    )
    return (
        _field_present(data, "topic", "audit_notes")
        and has_explanation
        and (search_attempted or clarification_block)
    )


def _outreach_variant_drafts(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    drafts: list[dict[str, Any]] = []
    for item in data.get("variants") or []:
        mapping = _to_plain_mapping(item)
        if not mapping:
            continue
        draft = _to_plain_mapping(mapping.get("draft"))
        if draft:
            drafts.append(draft)
    return drafts


def _outreach_variant_labels(data: Mapping[str, Any]) -> list[str]:
    requested = _string_items(data.get("requested_variant_labels"))
    if requested:
        return requested
    labels: list[str] = []
    for item in data.get("variants") or []:
        mapping = _to_plain_mapping(item)
        label = str(mapping.get("variant_label") or "").strip()
        if label:
            labels.append(label)
    return labels


def _is_outreach_variant_set(data: Mapping[str, Any]) -> bool:
    drafts = _outreach_variant_drafts(data)
    labels = _outreach_variant_labels(data)
    return bool(drafts) and bool(labels) and len(drafts) == len(labels)


def _structure_review(agent_key: str, data: Mapping[str, Any]) -> OrchestratorOutputReviewScore:
    if agent_key == "gmail_triage":
        if any(bucket in data for bucket in ("urgent", "important", "can_wait", "ignore")):
            buckets = ["urgent", "important", "can_wait", "ignore"]
            present = sum(1 for bucket in buckets if bucket in data)
            has_summary = _field_present(data, "request_summary", "audit_notes")
            score = int((present / len(buckets)) * 75) + (25 if has_summary else 0)
            return _review_score(
                "structure",
                score,
                "Priority grouping output exposes the expected bucket structure.",
                [] if score >= 85 else ["Include all priority buckets and a request summary."],
            )
        checks = [
            _field_present(data, "summary"),
            _field_present(data, "category"),
            _field_present(data, "priority"),
            _field_present(data, "recommended_action"),
            _field_present(data, "risk_flags"),
        ]
        score = int(sum(checks) / len(checks) * 100)
        return _review_score(
            "structure",
            score,
            "Gmail triage output is checked for category, priority, summary, action, and risks.",
            [] if score >= 85 else ["Include category, priority, summary, action, and risk flags."],
        )
    if agent_key == "business_research_analyst":
        focused_brief = _field_present(data, "company_name", "product", "customers", "facts")
        if focused_brief:
            checks = [
                _field_present(data, "company_name"),
                _field_present(data, "product"),
                _field_present(data, "customers"),
                _field_present(data, "facts"),
                _field_present(data, "unknowns"),
                _field_present(data, "source_ids_used", "sources"),
            ]
        else:
            checks = [
                _field_present(data, "name"),
                _field_present(data, "description"),
                _field_present(data, "fit_summary"),
                _field_present(data, "sources"),
                _field_present(data, "evidence", "claims"),
                _field_present(data, "missing_information", "risks"),
            ]
        score = int(sum(checks) / len(checks) * 100)
        return _review_score(
            "structure",
            score,
            "Business research output is checked for source-backed sections and unknowns.",
            [] if score >= 85 else ["Include facts, sources, unknowns, and decision context."],
        )
    if agent_key == "opportunity_scout":
        records = list(data.get("records") or [])
        if _scout_no_result_explained(data):
            checks = [
                _field_present(data, "topic"),
                "records" in data,
                _field_present(data, "audit_notes"),
                isinstance(data.get("raw_search_result_count"), int),
                data.get("outreach_generated") is False,
            ]
            score = int(sum(checks) / len(checks) * 100)
            return _review_score(
                "structure",
                score,
                (
                    "Opportunity Scout may return zero records when strict filters remove "
                    "every candidate, provided the run explains the search work and why "
                    "nothing survived."
                ),
                (
                    []
                    if score >= 85
                    else ["Explain the zero-result outcome with search counts and filter notes."]
                ),
            )
        base_checks = [
            _field_present(data, "topic"),
            "records" in data,
            _field_present(data, "audit_notes"),
        ]
        coverage = _record_field_coverage(
            records,
            "company_name",
            "opportunity_type",
            "priority_score",
            "recommended_next_step",
            "sources",
        )
        score = int((sum(base_checks) / len(base_checks) * 35) + (coverage * 65))
        if not records and _field_present(data, "audit_notes", "source_bundle_quality_notes"):
            score = max(score, 70)
        return _review_score(
            "structure",
            score,
            (
                "Opportunity Scout output is checked for ranked records, scores, next "
                "steps, and sources."
            ),
            (
                []
                if score >= 85
                else ["Include scored records with sources and recommended next steps."]
            ),
        )
    if agent_key == "outreach_composer":
        if _is_outreach_variant_set(data):
            variants = _outreach_variant_drafts(data)
            labels = _outreach_variant_labels(data)
            checks = [
                _field_present(data, "company_name"),
                _field_present(data, "outreach_goal"),
                bool(labels),
                len(variants) == len(labels),
                _record_field_coverage(
                    variants,
                    "email_subject",
                    "email_body",
                    "linkedin_note",
                    "personalization_rationale",
                    "facts_used",
                    "source_ids_used",
                )
                >= 0.9,
                all(
                    draft.get("approval_required") is True and draft.get("send_enabled") is False
                    for draft in variants
                ),
                data.get("approval_required") is True,
                data.get("send_enabled") is False,
            ]
            score = int(sum(checks) / len(checks) * 100)
            return _review_score(
                "structure",
                score,
                (
                    "Outreach variant sets are checked for aligned tone labels, grounded "
                    "draft content, and preserved approval and no-send boundaries."
                ),
                (
                    []
                    if score >= 85
                    else [
                        "Return aligned variant labels and keep each draft grounded, "
                        "approval-required, and send-disabled."
                    ]
                ),
            )
        checks = [
            _field_present(data, "email_subject", "subject"),
            _field_present(data, "email_body", "body"),
            _field_present(data, "linkedin_note"),
            _field_present(data, "personalization_rationale"),
            _field_present(data, "facts_used"),
            _field_present(data, "source_ids_used"),
            data.get("approval_required") is True,
        ]
        score = int(sum(checks) / len(checks) * 100)
        return _review_score(
            "structure",
            score,
            (
                "Outreach output is checked for draft copy, rationale, source "
                "attribution, and approval fields."
            ),
            (
                []
                if score >= 85
                else ["Include copy, rationale, facts used, source ids, and approval fields."]
            ),
        )
    return _review_score(
        "structure",
        60,
        "Unknown agent output received a generic structure review.",
    )


def _tone_review(
    agent_key: str,
    data: Mapping[str, Any],
    text: str,
) -> OrchestratorOutputReviewScore:
    gaps: list[str] = []
    score = 100
    if "\u2014" in text:
        score -= 35
        gaps.append("Remove em dashes from operator-facing or outbound copy.")
    if _HYPE_OR_UNSUPPORTED_TONE_RE.search(text):
        score -= 35
        gaps.append("Remove hype, urgency, or unsupported prior-outcome phrasing.")
    if agent_key == "outreach_composer" and data.get("unsupported_claims_flagged"):
        score -= 25
        gaps.append("Resolve unsupported outreach claims before review.")
    return _review_score(
        "tone",
        score,
        "Tone is checked for restraint, professionalism, no em dashes, and no hype.",
        gaps,
    )


def _readability_review(data: Mapping[str, Any], text: str) -> OrchestratorOutputReviewScore:
    word_count = len(re.findall(r"\b\w+\b", text))
    metadata_score, metadata_gaps = _human_metadata_score(data)
    if word_count == 0:
        return _review_score(
            "readability",
            20,
            "No readable output text was available for review.",
            ["Return enough structured text for a reviewer to inspect."],
        )
    if word_count > 1800:
        return _review_score(
            "readability",
            min(65, metadata_score),
            "Output is readable but long for manual review.",
            [
                "Tighten summaries and move raw detail behind source links or artifacts.",
                *metadata_gaps,
            ],
        )
    summary_present = _field_present(
        data,
        "summary",
        "fit_summary",
        "request_summary",
        "personalization_rationale",
        "recommended_next_step",
        "why_it_matters",
    )
    score = min(90 if summary_present else 75, metadata_score)
    return _review_score(
        "readability",
        score,
        (
            "Readability is checked for concise human-facing structure, useful summary, "
            "and minimal non-human metadata."
        ),
        [
            *(metadata_gaps or []),
            *([] if summary_present else ["Add a concise summary or rationale field."]),
        ],
    )


_REQUEST_ALIGNMENT_STOPWORDS = frozenset(
    {
        "about",
        "above",
        "after",
        "agent",
        "agents",
        "also",
        "before",
        "brief",
        "call",
        "can",
        "channel",
        "chief",
        "create",
        "current",
        "did",
        "draft",
        "drafts",
        "external",
        "externally",
        "for",
        "from",
        "have",
        "identify",
        "into",
        "kni",
        "keep",
        "next",
        "not",
        "outside",
        "post",
        "practical",
        "produce",
        "reply",
        "request",
        "requests",
        "review",
        "schedule",
        "send",
        "slack",
        "staff",
        "summarize",
        "the",
        "this",
        "tool",
        "use",
        "what",
        "which",
        "with",
        "work",
        "write",
    }
)


def _request_alignment_terms(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_]{3,}", str(text or "").lower())
        if token not in _REQUEST_ALIGNMENT_STOPWORDS
    }


_ALIGNMENT_CONCEPT_TERMS: dict[str, frozenset[str]] = {
    "planning": frozenset({"planner", "planning", "orchestrator", "executor", "control"}),
    "routing": frozenset(
        {"route", "routes", "routing", "handoff", "handoffs", "capability", "specialist"}
    ),
    "context": frozenset({"context", "memory", "workitem", "workitems", "slack", "thread"}),
    "quality": frozenset(
        {"eval", "evals", "evaluate", "evaluation", "feedback", "review", "validation", "verify"}
    ),
    "safety": frozenset({"approval", "approvals", "gate", "gates", "safety", "deterministic"}),
}


def _alignment_concepts(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-zA-Z][a-zA-Z0-9_]{2,}", str(text or "").lower()))
    return {concept for concept, terms in _ALIGNMENT_CONCEPT_TERMS.items() if tokens & set(terms)}


def _request_alignment_gap(
    *,
    request_summary: str,
    output_text: str,
) -> tuple[int | None, list[str]]:
    diagnostic_gap = _diagnostic_alignment_gap(
        request_summary=request_summary,
        output_text=output_text,
    )
    if diagnostic_gap[0] is not None:
        return diagnostic_gap
    request_terms = _request_alignment_terms(request_summary)
    if len(request_terms) < 4:
        return None, []
    output_terms = _request_alignment_terms(output_text)
    if not output_terms:
        return 35, ["Request/output term overlap is low; output may be unrelated."]
    overlap = request_terms & output_terms
    ratio = len(overlap) / max(min(len(request_terms), 12), 1)
    if ratio >= 0.25:
        return None, []
    concept_overlap = _alignment_concepts(request_summary) & _alignment_concepts(output_text)
    if len(concept_overlap) >= 2:
        return None, []
    missing = sorted(request_terms - output_terms)[:6]
    return (
        45 if ratio < 0.1 else 65,
        [
            (
                "Request/output term overlap is low; output may be unrelated. "
                f"Missing request terms include: {', '.join(missing)}."
            )
        ],
    )


def _diagnostic_alignment_gap(
    *,
    request_summary: str,
    output_text: str,
) -> tuple[int | None, list[str]]:
    request_text = " ".join(str(request_summary or "").lower().split())
    if not re.search(
        r"\b(?:same\s+response|wrong\s+(?:response|answer|lane)|"
        r"unrelated\s+(?:response|answer|output)|request\s+and\s+response|"
        r"keeps?\s+posting.*(?:answer|response)|"
        r"why\s+(?:is|did)\s+(?:this|that|it).*(?:post|posting|respond|answer))\b",
        request_text,
    ):
        return None, []
    if not re.search(
        r"\b(?:why|debug|diagnose|cause|reason|because|fix|implemented|changes|"
        r"rerun|run\s+(?:it\s+)?again|keeps?\s+posting|keeps?\s+happening)\b",
        request_text,
    ):
        return None, []
    output = " ".join(str(output_text or "").lower().split())
    if re.search(
        r"\b(?:unrelated|mismatch|wrong|stale|prior|previous|context|route|routing|"
        r"orchestrator|planner|request|response|diagnos|cause|reason|fix|"
        r"implemented|changed|rerun)\b",
        output,
    ):
        return None, []
    return (
        35,
        [
            (
                "The request asks to diagnose a wrong or unrelated prior response, "
                "but the output does not address response mismatch, routing, context, "
                "or corrective action."
            )
        ],
    )


_ALIGNMENT_ECHO_KEYS = frozenset(
    {
        "input",
        "input_summary",
        "intent",
        "original_request",
        "prompt",
        "query",
        "request",
        "request_summary",
        "request_text",
        "task",
        "topic",
        "user_request",
    }
)


def _alignment_review_text(value: Any, *, parent_key: str = "") -> str:
    if value is None:
        return ""
    if isinstance(value, BaseModel):
        return _alignment_review_text(value.model_dump(mode="json"), parent_key=parent_key)
    if isinstance(value, Mapping):
        parts: list[str] = []
        for key, child in value.items():
            key_text = str(key or "").strip()
            normalized_key = key_text.lower()
            if normalized_key in _ALIGNMENT_ECHO_KEYS:
                continue
            parts.append(_alignment_review_text(child, parent_key=normalized_key))
        return "\n".join(part for part in parts if part)
    if isinstance(value, list | tuple | set):
        return "\n".join(_alignment_review_text(item, parent_key=parent_key) for item in value)
    if parent_key:
        return str(value or "").strip()
    return ""


def _relevance_review(
    agent_key: str,
    data: Mapping[str, Any],
    *,
    request_summary: str = "",
) -> OrchestratorOutputReviewScore:
    metadata_score, metadata_gaps = _human_metadata_score(data)
    if agent_key == "gmail_triage":
        if any(bucket in data for bucket in ("urgent", "important", "can_wait", "ignore")):
            bucket_items = [
                item
                for bucket in ("urgent", "important", "can_wait", "ignore")
                for item in data.get(bucket, []) or []
            ]
            checks = [
                _field_present(data, "request_summary"),
                _field_present(data, "source_message_count"),
                bool(bucket_items),
                _record_field_coverage(
                    bucket_items,
                    "bucket",
                    "priority",
                    "recommended_action",
                    "recommended_labels",
                )
                >= 0.8,
            ]
        else:
            checks = [
                _field_present(data, "category"),
                _field_present(data, "priority"),
                _field_present(data, "recommended_action"),
                _field_present(data, "recommended_labels"),
            ]
    elif agent_key == "business_research_analyst":
        checks = [
            _field_present(data, "fit_summary", "why_it_matters", "summary"),
            _field_present(data, "sources", "source_ids_used"),
            _field_present(data, "evidence", "facts", "key_findings"),
            _field_present(data, "missing_information", "unknowns"),
        ]
    elif agent_key == "opportunity_scout":
        records = list(data.get("records") or [])
        if _scout_no_result_explained(data):
            checks = [
                _field_present(data, "topic"),
                _field_present(data, "audit_notes", "source_bundle_quality_notes"),
                isinstance(data.get("raw_search_result_count"), int),
                data.get("outreach_generated") is False,
            ]
        else:
            checks = [
                _field_present(data, "topic"),
                bool(records),
                _record_field_coverage(records, "keystone_fit_reason", "recommended_next_step")
                >= 0.5,
                _record_field_coverage(records, "sources") >= 0.8,
            ]
            if not records:
                checks[1] = _field_present(data, "audit_notes", "source_bundle_quality_notes")
    elif agent_key == "outreach_composer":
        if _is_outreach_variant_set(data):
            variants = _outreach_variant_drafts(data)
            checks = [
                _field_present(data, "outreach_goal"),
                bool(_outreach_variant_labels(data)),
                _record_field_coverage(
                    variants,
                    "personalization_rationale",
                    "facts_used",
                    "source_ids_used",
                )
                >= 0.9,
                not any(draft.get("unsupported_claims_flagged") for draft in variants),
            ]
        else:
            checks = [
                _field_present(data, "outreach_goal", "personalization_rationale"),
                _field_present(data, "facts_used"),
                _field_present(data, "source_ids_used"),
                not data.get("unsupported_claims_flagged"),
            ]
    else:
        checks = [_field_present(data, "summary", "audit_notes")]
    score = min(int(sum(bool(check) for check in checks) / len(checks) * 100), metadata_score)
    alignment_cap, alignment_gaps = _request_alignment_gap(
        request_summary=request_summary,
        output_text=_alignment_review_text(data),
    )
    if alignment_cap is not None:
        score = min(score, alignment_cap)
    if metadata_gaps:
        suggestions = [*metadata_gaps, *alignment_gaps]
    elif score >= 85:
        suggestions = []
    else:
        suggestions = [
            *(alignment_gaps or []),
            "Tie the output more directly to the request and evidence.",
        ]
    return _review_score(
        "relevance",
        score,
        (
            "Relevance is checked against expected human decision fields, Keystone fit, "
            "evidence, and whether metadata is useful to an operator."
        ),
        suggestions,
    )


def _review_agent_key(agent_name: str) -> str:
    text = agent_name.lower().replace(" ", "_")
    if "gmail" in text:
        return "gmail_triage"
    if "account" in text or "research" in text or "company" in text:
        return "business_research_analyst"
    if "opportunity" in text or "scout" in text:
        return "opportunity_scout"
    if "outreach" in text or "composer" in text:
        return "outreach_composer"
    return text or "unknown"


def review_specialist_output(
    *,
    agent_name: str,
    output: Any,
    request_summary: str = "",
    run_type: str = "fixture",
) -> OrchestratorOutputReview:
    """Review a specialist output without model calls, credentials, or side effects."""

    data = _to_plain_mapping(output)
    agent_key = _review_agent_key(agent_name)
    text = _safe_review_text({"request_summary": request_summary, "output": data})
    send_hits = _truthy_send_side_effects(data)
    structure = _structure_review(agent_key, data)
    tone = _tone_review(agent_key, data, text)
    readability = _readability_review(data, text)
    relevance = _relevance_review(agent_key, data, request_summary=request_summary)
    dimensions = [structure, tone, readability, relevance]
    overall = int(round(sum(dimension.score for dimension in dimensions) / len(dimensions)))
    observed_gaps = [
        suggestion for dimension in dimensions for suggestion in dimension.suggestions if suggestion
    ]
    approval_boundary_ok = not send_hits
    if not approval_boundary_ok:
        overall = min(overall, 50)
        observed_gaps.append(
            "Output reported send, scheduling, publishing, or write-side-effect fields."
        )
    status = "fail" if not approval_boundary_ok else _review_status(overall)
    strengths = [
        f"{dimension.dimension} passed" for dimension in dimensions if dimension.status == "pass"
    ]
    next_step = (
        "Resolve side-effect boundary fields before any downstream review."
        if not approval_boundary_ok
        else (
            "Ready for human review using the specialist report."
            if status == "pass"
            else "Address observed gaps, then rerun the specialist and orchestrator review."
        )
    )
    return OrchestratorOutputReview(
        agent_name=agent_key,
        output_type=type(output).__name__ if not isinstance(output, Mapping) else "dict",
        review_mode="deterministic",
        overall_score=overall,
        status=status,
        structure=structure,
        tone=tone,
        readability=readability,
        relevance=relevance,
        human_readable=readability.status != "fail",
        metadata_relevance_ok=not any("non-human metadata" in gap for gap in observed_gaps),
        approval_boundary_ok=approval_boundary_ok,
        send_enabled=False,
        can_send_email=False,
        llm_review_used=False,
        deterministic_baseline={},
        cost_guard={
            "mode": "deterministic",
            "model_call": False,
            "full_body_fields_omitted": sorted(_FULL_BODY_KEYS),
        },
        observed_gaps=observed_gaps or ["None observed by deterministic review."],
        strengths=strengths,
        recommended_next_step=next_step,
        test_pack_checks={
            "Structure": structure.status,
            "Tone": tone.status,
            "Readability": readability.status,
            "Relevance": relevance.status,
            "Preserves no-send behavior": "pass" if approval_boundary_ok else "fail",
        },
        audit_notes=[
            "Orchestrator review used deterministic Python checks only.",
            f"Run type: {run_type}.",
            (
                "This deterministic review step made no model, network, email, Slack, "
                "CRM, LinkedIn, scheduling, or publishing calls."
            ),
        ],
    )


_REVIEW_STATUS_RANK: dict[OutputReviewStatus, int] = {
    "fail": 0,
    "partial": 1,
    "pass": 2,
}


def _combine_review_status(
    deterministic_review: OrchestratorOutputReview,
    llm_review: OrchestratorOutputReview,
) -> OutputReviewStatus:
    if not deterministic_review.approval_boundary_ok:
        return "fail"
    if llm_review.status == "fail":
        return "fail"
    if deterministic_review.status == "fail":
        return "partial"
    if "partial" in {deterministic_review.status, llm_review.status}:
        return "partial"
    return "pass"


def _status_score_cap(status: OutputReviewStatus) -> int:
    if status == "fail":
        return 69
    if status == "partial":
        return 84
    return 100


def _merge_unique_review_notes(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for note in group:
            cleaned = str(note or "").strip()
            if cleaned and cleaned not in merged:
                merged.append(cleaned)
    return merged


def _hybridize_llm_review(
    *,
    deterministic_review: OrchestratorOutputReview,
    llm_review: OrchestratorOutputReview,
) -> OrchestratorOutputReview:
    """Merge LLM qualitative review with deterministic Python safety gates."""

    llm_status = llm_review.status
    final_status = _combine_review_status(deterministic_review, llm_review)
    baseline = OrchestratorReviewBaseline(
        status=deterministic_review.status,
        overall_score=deterministic_review.overall_score,
        structure=deterministic_review.structure.status,
        tone=deterministic_review.tone.status,
        readability=deterministic_review.readability.status,
        relevance=deterministic_review.relevance.status,
        approval_boundary_ok=deterministic_review.approval_boundary_ok,
    )
    llm_score = int(llm_review.overall_score)
    deterministic_score = int(deterministic_review.overall_score)
    blended_score = round((llm_score * 0.65) + (deterministic_score * 0.35))
    final_score = min(blended_score, _status_score_cap(final_status))
    if not deterministic_review.approval_boundary_ok:
        final_score = min(final_score, 50)

    observed_gaps = _merge_unique_review_notes(
        [
            gap
            for gap in deterministic_review.observed_gaps
            if gap != "None observed by deterministic review."
        ],
        llm_review.observed_gaps,
    )
    strengths = _merge_unique_review_notes(llm_review.strengths, deterministic_review.strengths)
    audit_notes = _merge_unique_review_notes(
        llm_review.audit_notes,
        [
            "Hybrid review merged LLM judgment with deterministic Python gates.",
            ("Deterministic no-send, approval, and side-effect checks remain authoritative."),
            f"Hybrid status: deterministic={deterministic_review.status}, "
            f"llm={llm_review.status}, final={final_status}.",
        ],
    )
    next_step = llm_review.recommended_next_step
    if final_status == "fail":
        next_step = "Resolve blocking deterministic or LLM review gaps before reuse."
    elif final_status == "partial":
        next_step = "Treat as usable only after human review or targeted revision."

    cost_guard_payload = {
        **_to_plain_mapping(llm_review.cost_guard),
        "mode": "llm_hybrid",
        "deterministic_hard_gates_authoritative": True,
        "status_policy": (
            "hard deterministic violation or LLM fail => fail; safe deterministic fail "
            "with LLM pass/partial => partial; any partial => partial"
        ),
    }
    cost_guard = OrchestratorReviewCostGuard(
        mode=str(cost_guard_payload.get("mode") or ""),
        model_call=bool(cost_guard_payload.get("model_call", False)),
        full_body_fields_omitted=list(cost_guard_payload.get("full_body_fields_omitted") or []),
        max_serialized_chars=int(cost_guard_payload.get("max_serialized_chars") or 0),
        max_string_chars=int(cost_guard_payload.get("max_string_chars") or 0),
        max_list_items=int(cost_guard_payload.get("max_list_items") or 0),
        max_mapping_keys=int(cost_guard_payload.get("max_mapping_keys") or 0),
        scope=str(cost_guard_payload.get("scope") or ""),
        deterministic_hard_gates_authoritative=bool(
            cost_guard_payload.get("deterministic_hard_gates_authoritative", False)
        ),
        status_policy=str(cost_guard_payload.get("status_policy") or ""),
    )

    llm_review.review_mode = "llm"
    llm_review.overall_score = final_score
    llm_review.status = final_status
    llm_review.approval_boundary_ok = deterministic_review.approval_boundary_ok
    llm_review.send_enabled = False
    llm_review.can_send_email = False
    llm_review.llm_review_used = True
    llm_review.deterministic_baseline = baseline
    llm_review.cost_guard = cost_guard
    llm_review.observed_gaps = observed_gaps or ["None observed by hybrid review."]
    llm_review.strengths = strengths
    llm_review.recommended_next_step = next_step
    llm_review.test_pack_checks = OrchestratorReviewChecks.from_mapping(
        {
            "Raw LLM review status": llm_status,
            "Deterministic review status": deterministic_review.status,
            "Preserves no-send behavior": (
                "pass" if deterministic_review.approval_boundary_ok else "fail"
            ),
            "Hybrid final status": final_status,
        }
    )
    llm_review.audit_notes = audit_notes
    return llm_review


def review_specialist_output_llm(
    *,
    agent_name: str,
    output: Any,
    request_summary: str = "",
    run_type: str = "fixture",
    run_config: Any | None = None,
    live: bool = False,
    model: str | None = None,
    fallback_to_deterministic: bool = True,
) -> OrchestratorOutputReview:
    """Review specialist output with the Orchestrator LLM when explicitly requested."""

    deterministic_review = review_specialist_output(
        agent_name=agent_name,
        output=output,
        request_summary=request_summary,
        run_type=run_type,
    )
    if run_config is None and not live:
        if fallback_to_deterministic:
            return deterministic_review.model_copy(
                update={
                    "review_mode": "llm_unavailable",
                    "audit_notes": [
                        *deterministic_review.audit_notes,
                        "LLM review was requested but no local run_config or live=True "
                        "was supplied; deterministic review was returned.",
                    ],
                }
            )
        raise RuntimeError(
            "LLM orchestrator review requires an explicit fake/local run_config or "
            "live=True for credential-gated model execution."
        )

    payload = _safe_review_payload(
        agent_name=agent_name,
        output=output,
        request_summary=request_summary,
        run_type=run_type,
        deterministic_review=deterministic_review,
    )
    sdk_result = run_typed_sdk_agent(
        agent=build_orchestrator_review_agent(
            model=model,
            request_text=skill_request_text(payload),
        ),
        typed_input=json.dumps(payload, ensure_ascii=True, sort_keys=True),
        output_type=OrchestratorOutputReview,
        run_config=run_config,
        live=live,
        workflow_name="Keystone orchestrator LLM output review",
        trace_metadata={
            "agent": "orchestrator",
            "review_mode": "llm",
            "reviewed_agent": _review_agent_key(agent_name),
        },
    )
    candidate = sdk_result.output
    candidate.review_mode = "llm"
    candidate.reviewed_by = "orchestrator"
    candidate.agent_name = _review_agent_key(candidate.agent_name or agent_name)
    candidate.send_enabled = False
    candidate.can_send_email = False
    candidate.llm_review_used = True
    candidate.cost_guard = {
        "mode": "llm",
        "full_body_fields_omitted": sorted(_FULL_BODY_KEYS),
        "max_serialized_chars": _REVIEW_TEXT_CHARS,
        "max_string_chars": _REVIEW_FIELD_CHARS,
        "max_list_items": _REVIEW_LIST_ITEMS,
        "max_mapping_keys": _REVIEW_MAPPING_KEYS,
        "scope": "structure, relevance, human readability, professional tone",
    }
    candidate.audit_notes = [
        *candidate.audit_notes,
        "LLM review was explicitly requested through the orchestrator.",
        "Deterministic baseline review was included before the LLM review.",
        "Review payload was compacted and full body fields were omitted to limit cost.",
        "Python validation preserved no-send fields after LLM review.",
        *_sdk_cost_audit_notes(sdk_result),
    ]
    return _hybridize_llm_review(
        deterministic_review=deterministic_review,
        llm_review=candidate,
    )


def _post_process_llm_route(
    candidate: OrchestratorResult,
    *,
    request_text: str,
    approved_context_present: bool,
    workflow_state: Mapping[str, Any],
) -> OrchestratorResult:
    if candidate.route == "outreach_composer" and not approved_context_present:
        return _missing_outreach_context_refusal(workflow_state=workflow_state).model_copy(
            update={
                "routing_mode": "llm",
                "audit_notes": [
                    *candidate.audit_notes,
                    "LLM route candidate requested outreach without approved context; "
                    "Python gate refused it.",
                ],
            }
        )

    handoff = _HANDOFF_BY_ROUTE.get(candidate.route)
    candidate.routing_mode = "llm"
    candidate.target_agent = handoff.agent_name if handoff else None
    candidate.workflow = [candidate.route] if handoff else []
    candidate.requires_human_review = True
    candidate.approval_required = True
    candidate.approved_context_present = approved_context_present
    candidate.send_enabled = False
    candidate.can_send_email = False
    if "send_email" not in candidate.forbidden_actions:
        candidate.forbidden_actions.append("send_email")
    candidate.intended_handoffs = _handoff_specs()
    candidate.retrieval_hint = _route_retrieval_hint(
        candidate.route,
        request_text=request_text,
        current_hint=candidate.retrieval_hint,
    )
    candidate.state_context_used = not _workflow_state_is_empty(workflow_state)
    candidate.workflow_state_summary = dict(workflow_state)
    if candidate.route == "outreach_composer":
        candidate.approval_scope = ApprovalScope.EXTERNAL_USE
        candidate.external_use_approval_required = True
        if not candidate.approval_rationale:
            candidate.approval_rationale = (
                "Drafting may proceed from approved context; external use remains gated."
            )
    candidate.audit_notes = [
        *candidate.audit_notes,
        "LLM route candidate accepted after deterministic Python gates.",
        "SDK handoff metadata is traceable; no send path is enabled.",
    ]
    return candidate


def _sdk_cost_audit_notes(result: TypedAgentRunResult[Any]) -> list[str]:
    """Return compact SDK cost/cache notes without raw prompt or session data."""

    usage = result.usage or {}
    cost = result.cost or {}
    request_cache = result.request_cache or {}
    notes = [
        (
            "SDK usage: "
            f"input_tokens={usage.get('input_tokens')}; "
            f"cached_input_tokens={usage.get('cached_input_tokens')}; "
            f"cache_hit_rate={usage.get('cache_hit_rate')}; "
            f"output_tokens={usage.get('output_tokens')}; "
            f"reasoning_output_tokens={usage.get('reasoning_output_tokens')}."
        ),
        (
            "SDK estimated cost: "
            f"estimated_usd={cost.get('estimated_usd', cost.get('amount_usd'))}; "
            f"source={cost.get('source')}."
        ),
    ]
    if request_cache:
        static_prefix = str(request_cache.get("static_prefix_sha256") or "")[:12]
        dynamic_prompt = str(request_cache.get("dynamic_prompt_sha256") or "")[:12]
        notes.append(
            "SDK request cache diagnostics: "
            f"static_prefix={static_prefix}; "
            f"dynamic_prompt={dynamic_prompt}; "
            f"dynamic_chars={request_cache.get('dynamic_prompt_chars')}; "
            f"session_attached={bool(request_cache.get('session_attached'))}."
        )
    return notes


def _looks_like_chief_of_staff_operational_request(lower: str) -> bool:
    if "chief of staff" in lower or "slack ops" in lower or "slack operations" in lower:
        return True
    if _looks_like_source_link_followup(lower):
        return True
    action_markers = (
        "audit",
        "review",
        "inspect",
        "diagnose",
        "debug",
        "evaluate",
        "assess",
        "recommend",
        "propose",
        "identify",
        "summarize",
        "list",
        "check",
    )
    if not any(marker in lower for marker in action_markers):
        return False
    if "slack" in lower and any(
        marker in lower
        for marker in (
            "thread",
            "selected message",
            "selected slack",
            "operator request",
            "unresolved",
            "follow-up",
            "follow up",
            "summarize",
            "channel",
            "route",
            "routing",
            "socket",
            "post",
        )
    ):
        return True
    operational_markers = (
        "business-agent architecture",
        "business agent architecture",
        "agent architecture",
        "architecture changes",
        "orchestrator",
        "planner",
        "manager loop",
        "implementation step",
        "implementation steps",
        "automation",
        "automations",
        "workitem",
        "work item",
        "bridge",
        "runtime state",
        "operator request",
        "operator requests",
        "backlog",
    )
    return any(marker in lower for marker in operational_markers)


def _looks_like_source_link_followup(lower: str) -> bool:
    normalized = " ".join(str(lower or "").lower().split())
    if not normalized:
        return False
    if not re.search(r"\b(?:summari[sz]e|explain|read|review|describe)\b", normalized):
        return False
    return bool(
        re.search(
            r"\b(?:link|source|url)\s*(?:#?\d+|one|two|three|first|second|third)\b",
            normalized,
        )
        or re.search(
            r"\b(?:first|second|third|1st|2nd|3rd)\s+(?:link|source|url)\b",
            normalized,
        )
    )


def _route_ambiguous_with_llm(
    request: str | Mapping[str, Any] | None,
    *,
    approved_context_present: bool,
    workflow_state: Mapping[str, Any],
    llm_router: LLMRouter | None = None,
    run_config: Any | None = None,
    live: bool = False,
    model: str | None = None,
) -> OrchestratorResult | None:
    prompt = _llm_route_prompt(request, workflow_state)
    request_text = _payload_text(request)
    if llm_router is not None:
        candidate = _coerce_route_candidate(llm_router(prompt, workflow_state))
        return _post_process_llm_route(
            candidate,
            request_text=request_text,
            approved_context_present=approved_context_present,
            workflow_state=workflow_state,
        )
    if run_config is None and not live:
        return None
    sdk_result = run_typed_sdk_agent(
        agent=build_orchestrator_agent(model=model, request_text=request_text),
        typed_input=prompt,
        output_type=OrchestratorResult,
        run_config=run_config,
        live=live,
        workflow_name="Keystone orchestrator LLM routing",
        trace_metadata={"agent": "orchestrator", "routing_mode": "llm"},
    )
    candidate = sdk_result.output
    candidate.audit_notes = [*candidate.audit_notes, *_sdk_cost_audit_notes(sdk_result)]
    return _post_process_llm_route(
        candidate,
        request_text=request_text,
        approved_context_present=approved_context_present,
        workflow_state=workflow_state,
    )


def _manual_plan_for_request(
    request: str | Mapping[str, Any] | None,
    *,
    provided: ManualRequestPlan | Mapping[str, Any] | None,
    enabled: bool,
) -> ManualRequestPlan | None:
    if provided is not None:
        authority = ExecutionIntentAuthority.from_value(provided)
        if authority.plan is not None:
            return authority.plan
        return ManualRequestPlan(
            source="invalid_supplied_plan",
            target_agent="clarification",
            intent="clarification",
            task_objective="clarification",
            missing_required_information=["valid canonical manual request plan"],
            rationale=(
                "A supplied execution plan was invalid. Orchestrator did not "
                "reinterpret the raw request through compatibility routing."
            ),
        )
    if not enabled:
        return None
    return infer_manual_request_plan(request)


def _route_from_manual_plan(
    plan: ManualRequestPlan | None,
    *,
    request_text: str,
    approved_context_present: bool,
    workflow_state: Mapping[str, Any],
) -> OrchestratorResult | None:
    if plan is None:
        return None
    audit_notes = [
        f"Manual request plan used before deterministic fallback: source={plan.source}.",
    ]
    if plan.rationale:
        audit_notes.append(f"Manual plan rationale: {plan.rationale}")
    audit_notes.extend(plan.planner_warnings)
    semantic_authority = ExecutionIntentAuthority.from_value(plan).canonical
    if plan.intent == "blocked_send":
        result = _send_refusal(
            approved_context_present=approved_context_present,
            workflow_state=workflow_state,
        )
        result.audit_notes = [*result.audit_notes, *audit_notes]
        return result
    route = plan.target_agent
    planned_workflow = list(
        dict.fromkeys(
            str(step)
            for step in plan.workflow
            if str(step)
            in {
                "gmail_triage",
                "business_research_analyst",
                "opportunity_scout",
                "outreach_composer",
                *_CONTEXT_AGENT_ROUTES,
            }
        )
    )
    if _chief_owns_context_summary_plan(plan):
        result = _result(
            route="chief_of_staff",
            rationale=plan.objective
            or "Chief of Staff owns the combined read-only context answer.",
            artifact=("input_type", plan.target_type),
            approval_required=False,
            requires_human_review=False,
            approval_scope=ApprovalScope.RESEARCH,
            approval_state=ApprovalState.APPROVED_FOR_RESEARCH,
            approval_rationale=(
                "The selected sources are read-only advisor inputs; Chief of Staff "
                "owns the combined answer and no provider write is authorized."
            ),
            routing_mode="llm" if semantic_authority else "deterministic",
            workflow_state=workflow_state,
            audit_notes=[
                *audit_notes,
                "Context workflow entries retained as Chief advisory tools.",
            ],
            workflow=planned_workflow,
        )
        return _with_crm_write_boundary(result, request_text=request_text)
    if len(planned_workflow) > 1:
        if "outreach_composer" in planned_workflow and not approved_context_present:
            outreach_index = planned_workflow.index("outreach_composer")
            has_context_building_step = any(
                step
                in {
                    "gmail_triage",
                    "business_research_analyst",
                    "opportunity_scout",
                }
                for step in planned_workflow[:outreach_index]
            )
            if not has_context_building_step:
                result = _missing_outreach_context_refusal(workflow_state=workflow_state)
                result.audit_notes = [*result.audit_notes, *audit_notes]
                return result
        first_route = planned_workflow[0]
        result = _result(
            route=first_route,
            rationale=plan.objective
            or "The manual request plan selected an ordered multi-owner workflow.",
            approved_context_present=approved_context_present,
            approval_scope=(
                ApprovalScope.EXTERNAL_USE
                if "outreach_composer" in planned_workflow
                else ApprovalScope.DRAFTING
            ),
            approval_rationale=(
                "The final copy remains internal and draft-only unless a separate "
                "scoped external-use approval permits publication."
            ),
            artifact=("input_type", plan.target_type),
            routing_mode="llm" if semantic_authority else "deterministic",
            workflow_state=workflow_state,
            audit_notes=[
                *audit_notes,
                "Python validated the planner's ordered workflow before execution.",
            ],
            retrieval_hint=_route_retrieval_hint(
                first_route,
                request_text=request_text,
            ),
            workflow=planned_workflow,
        )
        return _with_crm_write_boundary(result, request_text=request_text)
    if (
        not semantic_authority
        and
        not approved_context_present
        and route == "outreach_composer"
        and _requires_research_before_outreach(request_text)
        and _requested_cross_agent_workflow(
            request_text,
            start_route="business_research_analyst",
        )
    ):
        return _research_first_outreach_workflow_result(
            request_text=request_text,
            workflow_state=workflow_state,
            routing_mode="llm" if semantic_authority else "deterministic",
            audit_notes=audit_notes,
        )
    if not semantic_authority and _mixed_outreach_request_requires_context_gate(
        request_text,
        approved_context_present=approved_context_present,
    ):
        requested_workflow = _requested_cross_agent_workflow(
            request_text,
            start_route=route if route not in {"orchestrator", "clarification"} else "business_research_analyst",
        )
        has_research_first_path = "outreach_composer" in requested_workflow and any(
            step in requested_workflow[: requested_workflow.index("outreach_composer")]
            for step in ("business_research_analyst", "opportunity_scout", "gmail_triage")
        )
        if not has_research_first_path and (route != "gmail_triage" or not _gmail_cross_agent_workflow(request_text)):
            result = _missing_outreach_context_refusal(
                workflow_state=workflow_state,
                owning_route=(
                    route
                    if route not in {"orchestrator", "clarification"}
                    else "outreach_composer"
                ),
            )
            result.audit_notes = [*result.audit_notes, *audit_notes]
            return result
    if route == "clarification":
        return _result(
            route="clarification",
            rationale=plan.objective or "The manual request plan requires clarification.",
            stop_reason="uncertain_input",
            clarification_request=(
                "Please identify the exact item and requested change before any action."
            ),
            workflow_state=workflow_state,
            audit_notes=audit_notes,
        )
    if route == "orchestrator":
        return None
    if route == "outreach_composer":
        if not plan.requires_approved_context:
            return _result(
                route="outreach_composer",
                rationale=plan.objective
                or (
                    "The manual request plan selected Outreach Composer for a "
                    "provider-free response task."
                ),
                approved_context_present=approved_context_present,
                approval_scope=ApprovalScope.DRAFTING,
                approval_rationale=(
                    "No outreach artifact or external-use action was requested; "
                    "ordinary send and publish gates remain unchanged."
                ),
                routing_mode="llm" if semantic_authority else "deterministic",
                workflow_state=workflow_state,
                audit_notes=[
                    *audit_notes,
                    "Outreach approval context was not required by the semantic plan.",
                ],
            )
        thread_local_draft = bool(
            not semantic_authority
            and _looks_like_thread_local_draft_request(request_text)
        )
        if not approved_context_present and not thread_local_draft:
            result = _missing_outreach_context_refusal(workflow_state=workflow_state)
            result.audit_notes = [*result.audit_notes, *audit_notes]
            return result
        return _result(
            route="outreach_composer",
            rationale=plan.objective
            or (
                "The manual request plan selected thread-local draft-only outreach."
                if thread_local_draft
                else "The manual request plan selected draft-only outreach from approved context."
            ),
            approved_context_present=approved_context_present,
            approval_scope=ApprovalScope.EXTERNAL_USE,
            approval_rationale=(
                "Manual request plan selected outreach; any draft remains pending "
                "external-use approval."
            ),
            routing_mode="llm" if semantic_authority else "deterministic",
            workflow_state=workflow_state,
            audit_notes=audit_notes,
        )
    if route in _CONTEXT_AGENT_ROUTES:
        return _result(
            route=route,
            rationale=plan.objective
            or f"The manual request plan selected {route} for read-only context lookup.",
            artifact=("input_type", plan.target_type),
            approval_required=False,
            requires_human_review=False,
            approval_scope=ApprovalScope.RESEARCH,
            approval_state=ApprovalState.APPROVED_FOR_RESEARCH,
            approval_rationale=(
                "The canonical plan authorizes a read-only provider lookup; no "
                "separate drafting or external-use approval is required."
            ),
            routing_mode="llm" if semantic_authority else "deterministic",
            workflow_state=workflow_state,
            audit_notes=audit_notes,
        )
    if route in {
        "gmail_triage",
        "business_research_analyst",
        "opportunity_scout",
        "chief_of_staff",
    }:
        if (
            not semantic_authority
            and route == "opportunity_scout"
            and _looks_like_discovery_outreach_workflow(request_text)
        ):
            result = _safe_discovery_outreach_workflow_result(
                request_text=request_text,
                approved_context_present=approved_context_present,
                workflow_state=workflow_state,
            )
            result.audit_notes = [*result.audit_notes, *audit_notes]
            return result
        workflow = None
        if not semantic_authority and route == "gmail_triage":
            workflow = _gmail_cross_agent_workflow(request_text) or None
        elif (
            not semantic_authority
            and workflow is None
            and plan.requested_agent == "orchestrator"
        ):
            workflow = _requested_cross_agent_workflow(request_text, start_route=route) or None
        result = _result(
            route=route,
            rationale=plan.objective
            or f"The manual request plan selected {route} for this request.",
            artifact=("input_type", plan.target_type),
            routing_mode="llm" if semantic_authority else "deterministic",
            workflow_state=workflow_state,
            audit_notes=audit_notes,
            retrieval_hint=_route_retrieval_hint(
                route,
                request_text=request_text,
            ),
            workflow=workflow,
        )
        return _with_crm_write_boundary(result, request_text=request_text)
    return None


def _chief_owns_context_summary_plan(plan: ManualRequestPlan) -> bool:
    return bool(
        plan.target_agent == "chief_of_staff"
        and plan.intent == "context_lookup"
        and plan.task_objective == "context_lookup"
        and plan.expected_artifact_type == "context_summary"
        and plan.ask_shape.permission_state == "read_only"
    )


def _manual_plan_is_read_only_context_lookup(
    plan: ManualRequestPlan | None,
    *,
    request_text: str,
) -> bool:
    if plan is None:
        return False
    if not (plan.intent == "context_lookup" and plan.target_agent in {
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
        "rss_context_agent",
        "preprints_context_agent",
    }):
        return False
    cleaned = _without_negated_context_send_clauses(request_text)
    return not _looks_like_send_side_effect(cleaned)


def _manual_plan_is_internal_provider_write(
    plan: ManualRequestPlan | None,
    *,
    request_text: str,
) -> bool:
    """Keep an interpreted internal write out of the generic outbound-send gate."""

    if plan is None or not (
        plan.intent == "business_system_write"
        and plan.side_effect_policy == "internal_write_approval_required"
        and plan.target_agent
        in {
            "chief_of_staff",
            "airtable_context_agent",
            "google_workspace_context_agent",
            "gmail_triage",
            "zotero_context_agent",
        }
    ):
        return False
    actionable = _without_negated_route_action_clauses(request_text)
    return not bool(
        re.search(
            r"\b(?:send|post|publish|share|deliver)\b",
            actionable,
            re.IGNORECASE,
        )
    )


def route_request(
    request: str | Mapping[str, Any] | None,
    *,
    approved_company_profile: CompanyProfile | Mapping[str, Any] | None = None,
    approved_opportunity_record: BaseModel | Mapping[str, Any] | None = None,
    workflow_state: Mapping[str, Any] | None = None,
    database_url: str | None = None,
    use_llm: bool = False,
    llm_router: LLMRouter | None = None,
    run_config: Any | None = None,
    live: bool = False,
    model: str | None = None,
    manual_plan: ManualRequestPlan | Mapping[str, Any] | None = None,
    use_manual_plan: bool = False,
    include_operator_feedback_request: bool = False,
) -> OrchestratorResult:
    """Route a request with Python hard gates and optional LLM handling for ambiguity."""

    text = _payload_text(request)
    lower_text = text.lower()
    route_text = _without_negated_route_action_clauses(text)
    route_lower_text = route_text.lower()
    loaded_state_context = (
        load_workflow_state_context(database_url=database_url) if database_url else None
    )
    state_context = _merge_workflow_state_context(loaded_state_context, workflow_state)
    profile_context = approved_company_profile or _mapping_value(
        request,
        "approved_company_profile",
        "company_profile",
        "profile",
    )
    opportunity_context = approved_opportunity_record or _mapping_value(
        request,
        "approved_opportunity_record",
        "opportunity_record",
        "opportunity",
    )
    approved_context_present = (
        _is_approved_context(
            profile_context,
            explicit_approved=approved_company_profile is not None
            or _mapping_value(request, "approved_company_profile") is not None,
        )
        or _is_approved_context(
            opportunity_context,
            explicit_approved=approved_opportunity_record is not None
            or _mapping_value(request, "approved_opportunity_record") is not None,
        )
        or bool(state_context.get("approved_context_available"))
        or _request_has_inline_approved_outreach_context(text)
    )
    resolved_manual_plan = _manual_plan_for_request(
        request,
        provided=manual_plan,
        enabled=use_manual_plan or manual_plan is not None,
    )
    send_side_effect = _looks_like_send_side_effect(text)
    read_only_context_lookup = _manual_plan_is_read_only_context_lookup(
        resolved_manual_plan,
        request_text=text,
    )
    internal_provider_write = _manual_plan_is_internal_provider_write(
        resolved_manual_plan,
        request_text=text,
    )

    def finish(result: OrchestratorResult) -> OrchestratorResult:
        return _with_operator_feedback_request(
            _with_crm_write_boundary(result, request_text=text),
            include=include_operator_feedback_request,
        )

    if (
        send_side_effect
        and not read_only_context_lookup
        and not internal_provider_write
        and _looks_like_discovery_outreach_workflow(text)
    ):
        return finish(
            _safe_discovery_outreach_workflow_result(
                request_text=text,
                approved_context_present=approved_context_present,
                workflow_state=state_context,
            )
        )

    if send_side_effect and not read_only_context_lookup and not internal_provider_write:
        return finish(
            _send_refusal(
                approved_context_present=approved_context_present,
                workflow_state=state_context,
            )
        )

    safety_refusal = _hard_safety_refusal(
        text,
        approved_context_present=approved_context_present,
        workflow_state=state_context,
        owning_route=(
            resolved_manual_plan.target_agent
            if resolved_manual_plan is not None
            and resolved_manual_plan.target_agent
            not in {"orchestrator", "clarification"}
            else "clarification"
        ),
    )
    if safety_refusal is not None:
        return finish(safety_refusal)

    resume_result = _resume_from_state_result(text, workflow_state=state_context)
    if resume_result is not None:
        return finish(resume_result)

    planned_result = _route_from_manual_plan(
        resolved_manual_plan,
        request_text=text,
        approved_context_present=approved_context_present,
        workflow_state=state_context,
    )
    if planned_result is not None:
        return finish(planned_result)

    if _looks_like_chief_of_staff_operational_request(lower_text):
        return finish(
            _result(
                route="chief_of_staff",
                rationale=(
                    "The request asks for internal operational planning, architecture review, "
                    "Slack-thread follow-up, automation audit, or WorkItem/bridge diagnostics."
                ),
                artifact=("input_type", "slack_channel"),
                workflow_state=state_context,
            )
        )

    if _looks_like_explicit_business_research_request(text):
        return finish(
            _result(
                route="business_research_analyst",
                rationale=(
                    "The request explicitly asks Business Research to handle a "
                    "source-backed company or evidence-planning task."
                ),
                workflow_state=state_context,
                retrieval_hint=_route_retrieval_hint(
                    "business_research_analyst",
                    request_text=text,
                ),
            )
        )

    if _OUTREACH_RE.search(route_lower_text) and _request_has_inline_approved_outreach_context(text):
        return finish(
            _result(
                route="outreach_composer",
                rationale="The request asks for draft outreach from approved inline context.",
                approved_context_present=True,
                approval_scope=ApprovalScope.EXTERNAL_USE,
                approval_rationale=(
                    "Inline context is approved for draft-only use; any external use remains "
                    "approval-gated."
                ),
                workflow_state=state_context,
            )
        )

    if _looks_like_email(request, text):
        return finish(
            _result(
                route="gmail_triage",
                rationale="The request asks for Gmail or email-thread triage.",
                artifact=("input_type", "email"),
                workflow_state=state_context,
                workflow=_gmail_cross_agent_workflow(text) or None,
            )
        )

    if _looks_like_discovery_outreach_workflow(text):
        return finish(
            _safe_discovery_outreach_workflow_result(
                request_text=text,
                approved_context_present=approved_context_present,
                workflow_state=state_context,
            )
        )

    if _OUTREACH_RE.search(route_lower_text):
        if (
            not approved_context_present
            and _requires_research_before_outreach(text)
            and _requested_cross_agent_workflow(
                text,
                start_route="business_research_analyst",
            )
        ):
            return finish(
                _research_first_outreach_workflow_result(
                    request_text=text,
                    workflow_state=state_context,
                )
            )
        if _mixed_outreach_request_requires_context_gate(
            text,
            approved_context_present=approved_context_present,
        ):
            return finish(_missing_outreach_context_refusal(workflow_state=state_context))
        if not approved_context_present:
            return finish(_missing_outreach_context_refusal(workflow_state=state_context))
        return finish(
            _result(
                route="outreach_composer",
                rationale="The request asks for draft outreach and approved context is present.",
                approved_context_present=True,
                approval_scope=ApprovalScope.EXTERNAL_USE,
                approval_rationale=(
                    "Drafting context is approved; any draft produced remains pending "
                    "external-use approval."
                ),
                workflow_state=state_context,
            )
        )

    if _looks_like_company(text) and re.search(
        r"\b(?:research\s+brief|company\s+research|company\s+profile|"
        r"source-attributed|source\s+attributed|confirmed\s+facts)\b",
        route_lower_text,
    ):
        return finish(
            _result(
                route="business_research_analyst",
                rationale=(
                    "The request asks for a source-backed company research brief; "
                    "partnership or outreach language is context, not a Scout handoff."
                ),
                workflow_state=state_context,
                retrieval_hint=_route_retrieval_hint(
                    "business_research_analyst",
                    request_text=text,
                ),
            )
        )

    if _OPPORTUNITY_RE.search(route_lower_text):
        return finish(
            _result(
                route="opportunity_scout",
                rationale=(
                    "The request asks to find opportunities, leads, grants, partners, or companies."
                ),
                workflow_state=state_context,
                retrieval_hint=_route_retrieval_hint(
                    "opportunity_scout",
                    request_text=text,
                ),
            )
        )

    if _looks_like_company(text):
        return finish(
            _result(
                route="business_research_analyst",
                rationale=(
                    "The request appears to provide a company, broader research target, "
                    "local collection, URL, or domain."
                ),
                workflow_state=state_context,
                retrieval_hint=_route_retrieval_hint(
                    "business_research_analyst",
                    request_text=text,
                ),
            )
        )

    deterministic_result = _result(
        route="clarification",
        rationale="The request does not contain enough information for a safe specialist handoff.",
        stop_reason="uncertain_input",
        clarification_request=(
            "Please provide an email, company name or URL, opportunity scouting request, "
            "or an approved context for draft outreach."
        ),
        workflow_state=state_context,
    )
    if use_llm:
        llm_result = _route_ambiguous_with_llm(
            request,
            approved_context_present=approved_context_present,
            workflow_state=state_context,
            llm_router=llm_router,
            run_config=run_config,
            live=live,
            model=model,
        )
        if llm_result is not None:
            return finish(llm_result)
        deterministic_result.routing_mode = "llm_unavailable"
        deterministic_result.audit_notes.append(
            "LLM routing was requested but no live or local run_config/router was supplied; "
            "returned deterministic fallback."
        )
    return finish(deterministic_result)


@function_tool
def route_request_placeholder(request: str) -> str:
    """Route a request in deterministic fixture mode without live model or tool calls."""

    return route_request(request).model_dump_json()


@function_tool(**keystone_tool_guardrail_kwargs())
def load_orchestrator_workflow_state(
    max_results: int = 5,
    database_url: str | None = None,
) -> str:
    """Load redacted local workflow state for routing without exposing raw artifacts."""

    return json.dumps(
        load_workflow_state_context(database_url=database_url, max_results=max_results),
        ensure_ascii=True,
        sort_keys=True,
    )


def _attach_handoff_metadata(agent: Agent) -> Agent:
    metadata = [handoff.model_dump() for handoff in INTENDED_HANDOFFS]
    try:
        agent.intended_handoffs = metadata
        agent.handoff_contract = {"handoffs": metadata, "send_enabled": False}
    except Exception:
        pass
    return agent


def _build_read_only_specialist_tools(
    *,
    raw_operator_request: str = "",
    manual_request_plan: ManualRequestPlan | Mapping[str, Any] | None = None,
) -> list[Any]:
    return build_specialist_agent_tools(
        manager_agent_name="orchestrator",
        raw_operator_request=raw_operator_request,
        manual_request_plan=manual_request_plan,
        include_routes={"business_research_analyst", "opportunity_scout"},
        route_tool_name_overrides={
            "business_research_analyst": BUSINESS_RESEARCH_TOOL_NAME,
            "opportunity_scout": OPPORTUNITY_SCOUT_TOOL_NAME,
        },
        route_descriptions={
            "business_research_analyst": (
                "Run the Business Research Analyst for internal, source-attributed "
                "research synthesis only. This tool is read/plan-only and cannot send, "
                "publish, schedule, create approval items, or persist memory; Python gates "
                "and approval policy remain authoritative."
            ),
            "opportunity_scout": (
                "Run Opportunity Scout for read-only discovery, ranking, and internal "
                "candidate synthesis. This tool cannot save opportunities, persist memory, "
                "write Airtable/Google Workspace records, send, post, schedule, or create "
                "approval items."
            ),
        },
    )


def build_orchestrator_agent(
    model: str | None = None,
    *,
    include_handoffs: bool = True,
    include_specialist_tools: bool | None = None,
    request_text: str = "",
    manual_request_plan: ManualRequestPlan | Mapping[str, Any] | None = None,
    context_flags: Mapping[str, bool] | None = None,
    include_all_skills: bool = False,
    tool_tier: str | int | None = None,
) -> Agent:
    """Build the orchestrator agent with subordinate agent handoffs."""

    instructions = compose_instructions(
        "keystone_profile.md",
        "safety_policy.md",
        "tools.md",
        "orchestrator.md",
        skill_files=select_agent_skill_names(
            "orchestrator",
            request_text=request_text,
            context_flags=context_flags,
            include_all=include_all_skills,
        ),
    )
    handoffs = [
        spec.build_agent() for spec in SPECIALIST_AGENT_SPECS if spec.handoff_enabled
    ] if include_handoffs else []
    specialist_tools = (
        _build_read_only_specialist_tools(
            raw_operator_request=request_text,
            manual_request_plan=manual_request_plan,
        )
        if (
            include_specialist_tools
            if include_specialist_tools is not None
            else _env_flag_enabled(ORCHESTRATOR_SPECIALIST_TOOLS_ENV)
        )
        else []
    )
    tools = append_configured_file_search_tools(
        "orchestrator",
        [
            list_local_context_sources,
            search_local_context,
            read_local_context_file,
            retrieve_memory,
            airtable_get_base_schema,
            airtable_read_records,
            airtable_write_record,
            airtable_upload_attachment,
            airtable_create_expense_from_receipt,
            search_web,
            structure_web_data_for_schema,
            render_page,
            capture_browser_diagnostics,
            summarize_rendered_page_diagnostics,
            route_request_placeholder,
            load_orchestrator_workflow_state,
            load_pending_approval_items,
            extract_research_claims_from_html,
            *specialist_tools,
            *google_workspace_tools(),
        ],
    )
    if tool_tier is not None:
        tools = filter_tools_for_tier("orchestrator", tools, tool_tier)
    agent = build_sdk_agent(
        name="orchestrator",
        instructions=instructions,
        output_type=OrchestratorResult,
        tools=tools,
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="orchestrator",
        model_settings=build_model_settings(
            reasoning_effort=ORCHESTRATOR_REASONING_EFFORT,
            verbosity=ORCHESTRATOR_REVIEW_VERBOSITY,
        ),
        handoffs=handoffs,
        handoff_description=(
            "Use to route Keystone business-agent requests to the correct specialist while "
            "preserving approval gates and no-send policy."
        ),
    )
    return _attach_handoff_metadata(agent)


def build_orchestrator_review_agent(
    model: str | None = None,
    *,
    request_text: str = "",
    include_all_skills: bool = False,
) -> Agent:
    """Build the orchestrator in output-review mode."""

    instructions = compose_instructions(
        "keystone_profile.md",
        "safety_policy.md",
        "tools.md",
        "orchestrator.md",
        skill_files=select_agent_skill_names(
            "orchestrator",
            request_text=request_text,
            include_all=include_all_skills,
        ),
    )
    return build_sdk_agent(
        name="orchestrator",
        instructions=instructions,
        output_type=OrchestratorOutputReview,
        tools=[
            list_local_context_sources,
            search_local_context,
            read_local_context_file,
            load_orchestrator_workflow_state,
            extract_research_claims_from_html,
        ],
        guardrails=keystone_guardrails(),
        model=model,
        model_settings=build_model_settings(
            reasoning_effort=ORCHESTRATOR_REASONING_EFFORT,
            verbosity=ORCHESTRATOR_REVIEW_VERBOSITY,
            max_tokens=ORCHESTRATOR_REVIEW_MAX_TOKENS,
        ),
        handoff_description=(
            "Use to review specialist outputs for human readability, structure, "
            "relevance, professional tone, and no-send boundaries."
        ),
    )


def run_orchestrator_sdk(
    typed_input: str | Mapping[str, Any],
    *,
    run_config: Any | None = None,
    live: bool = False,
    model: str | None = None,
    session: Any | None = None,
    include_specialist_tools: bool | None = None,
    tool_tier: str | int | None = None,
    manual_request_plan: ManualRequestPlan | Mapping[str, Any] | None = None,
) -> TypedAgentRunResult[OrchestratorResult]:
    """Run the orchestrator through the shared typed SDK harness."""

    typed_input_for_run = _orchestrator_sdk_input(typed_input, live=live)
    resolved_tool_tier = tool_tier or _default_orchestrator_sdk_tool_tier(
        typed_input,
        live=live,
    )
    return run_typed_sdk_agent(
        agent=build_orchestrator_agent(
            model=model,
            include_handoffs=False,
            include_specialist_tools=include_specialist_tools,
            request_text=skill_request_text(typed_input),
            manual_request_plan=manual_request_plan,
            tool_tier=resolved_tool_tier,
        ),
        typed_input=typed_input_for_run,
        output_type=OrchestratorResult,
        run_config=run_config,
        live=live,
        session=session,
    )


def _default_orchestrator_sdk_tool_tier(
    typed_input: str | Mapping[str, Any],
    *,
    live: bool,
) -> str:
    """Infer a conservative default tool tier for Orchestrator SDK runs."""

    budget = business_research_quality_budget(
        request_text=skill_request_text(typed_input),
        live_search=live,
    )
    return budget.tool_tier or "core_read"
