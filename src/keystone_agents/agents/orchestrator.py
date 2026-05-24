"""Orchestrator agent builder and deterministic fixture-mode routing."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel

from keystone_agents.agent_registry import SPECIALIST_AGENT_SPECS, specialist_handoff_specs
from keystone_agents.feedback import build_operator_feedback_request
from keystone_agents.guardrails import (
    assess_text_guardrails,
    keystone_guardrails,
    keystone_tool_guardrail_kwargs,
)
from keystone_agents.manual_request import infer_manual_request_plan
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.orchestrator.routing import (
    OPPORTUNITY_RE as _OPPORTUNITY_RE,
)
from keystone_agents.orchestrator.routing import (
    OUTREACH_RE as _OUTREACH_RE,
)
from keystone_agents.orchestrator.routing import (
    RESEARCH_RE as _RESEARCH_RE,
)
from keystone_agents.orchestrator.routing import (
    RESUME_RE as _RESUME_RE,
)
from keystone_agents.orchestrator.routing import (
    SEND_RE as _SEND_RE,
)
from keystone_agents.orchestrator.routing import (
    looks_like_company as _looks_like_company,
)
from keystone_agents.orchestrator.routing import (
    looks_like_email as _looks_like_email,
)
from keystone_agents.orchestrator.routing import (
    mapping_value as _mapping_value,
)
from keystone_agents.orchestrator.routing import (
    payload_text as _payload_text,
)
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
    run_typed_sdk_sync,
)
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env, redact_secrets
from keystone_agents.tools.browser_diagnostics_tool import (
    capture_browser_diagnostics,
    summarize_rendered_page_diagnostics,
)
from keystone_agents.tools.html_review_tool import extract_research_claims_from_html
from keystone_agents.tools.internal_data_tools import (
    airtable_get_base_schema,
    airtable_read_records,
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
BUSINESS_RESEARCH_TOOL_NAME = "business_research_analyst_research_brief"


_HANDOFF_BY_ROUTE = {handoff.route: handoff for handoff in INTENDED_HANDOFFS}


def _orchestrator_sdk_input(
    typed_input: str | Mapping[str, Any], *, live: bool
) -> str | Mapping[str, Any]:
    """Add live-safe operating context for SDK Orchestrator string prompts."""

    if not live or not isinstance(typed_input, str):
        return typed_input
    return {
        "request": typed_input,
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
_HARD_REFUSAL_FLAGS = frozenset({"possible_phi", "security", "legal_review", "professional_advice"})
_APPROVED_CONTEXT_OBJECT_TYPES = frozenset({"company_profile", "opportunity"})
_SAFE_STATE_TEXT_CHARS = 140
_REVIEW_TEXT_CHARS = 5000
_REVIEW_FIELD_CHARS = 700
_REVIEW_LIST_ITEMS = 8
_REVIEW_MAPPING_KEYS = 60
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

LLMRouter = Callable[[str, Mapping[str, Any]], OrchestratorResult | Mapping[str, Any]]


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
        "send_enabled": False,
    }
    normalized["approved_context_available"] = bool(
        context.get("approved_context_available") or _state_has_approved_context(normalized)
    )
    return normalized


def _workflow_state_is_empty(state: Mapping[str, Any] | None) -> bool:
    if not state:
        return True
    return not any(
        state.get(key)
        for key in (
            "pending_approvals",
            "approved_approval_items",
            "companies",
            "opportunities",
            "outreach_drafts",
            "prior_route_decisions",
        )
    ) and not bool(state.get("counts"))


def _state_has_pending_approval(state: Mapping[str, Any]) -> bool:
    return bool(state.get("pending_approvals"))


def _state_has_approved_context(state: Mapping[str, Any]) -> bool:
    for item in state.get("approved_approval_items") or []:
        mapping = _to_mapping(item) or {}
        object_type = str(mapping.get("object_type") or "").lower()
        if object_type in _APPROVED_CONTEXT_OBJECT_TYPES:
            return True
    return False


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
    approval_rationale: str = "",
    routing_mode: str = "deterministic",
    workflow_state: Mapping[str, Any] | None = None,
    audit_notes: list[str] | None = None,
    retrieval_hint: RetrievalHint | None = None,
) -> OrchestratorResult:
    handoff = _HANDOFF_BY_ROUTE.get(route)
    artifacts = {artifact[0]: artifact[1]} if artifact else {}
    workflow = [route] if route != "clarification" else []
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
        workflow=workflow,
        routing_mode=routing_mode,
        rationale=rationale,
        clarification_request=clarification_request,
        stop_reason=stop_reason,
        requires_human_review=True,
        approval_required=True,
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
                "approval_required",
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
) -> OrchestratorResult | None:
    assessment = assess_text_guardrails(text, check_outreach_claims=False)
    blocking_flags = sorted(_HARD_REFUSAL_FLAGS.intersection(assessment.risk_flags))
    if not blocking_flags:
        return None
    reasons = ", ".join(assessment.reasons) or "safety policy"
    return _result(
        route="clarification",
        rationale="The request is blocked by deterministic Keystone safety gates.",
        refused=True,
        stop_reason=f"Blocked by Python safety gate: {reasons}.",
        clarification_request=(
            "Remove PHI, security, legal, or professional-advice content before routing."
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
        rationale="The request asks for email sending, which is outside the v1 safety boundary.",
        refused=True,
        stop_reason=(
            "Email sending is not allowed. The orchestrator can only route to draft-only workflows."
        ),
        clarification_request="Confirm whether you want a draft-only workflow for human approval.",
        approved_context_present=approved_context_present,
        approval_scope=ApprovalScope.EXTERNAL_USE,
        approval_rationale="External-use approval does not enable automatic email sending.",
        workflow_state=workflow_state,
        audit_notes=["Hard Python send gate ran before any LLM routing."],
    )


def _missing_outreach_context_refusal(
    *,
    workflow_state: Mapping[str, Any],
) -> OrchestratorResult:
    return _result(
        route="clarification",
        rationale="Outreach drafting requires approved company or opportunity context.",
        refused=True,
        stop_reason=(
            "Approved CompanyProfile or approved OpportunityRecord is required before outreach."
        ),
        clarification_request=(
            "Provide an approved CompanyProfile or OpportunityRecord before requesting "
            "an outreach draft."
        ),
        approval_scope=ApprovalScope.DRAFTING,
        approval_rationale=(
            "Outreach drafting is blocked until a human-approved drafting context is present."
        ),
        workflow_state=workflow_state,
        audit_notes=["Hard Python approval-context gate blocked outreach routing."],
    )


def _resume_from_state_result(
    text: str,
    *,
    workflow_state: Mapping[str, Any],
) -> OrchestratorResult | None:
    if not _RESUME_RE.search(text):
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
    return _field_present(data, "topic", "audit_notes") and has_explanation and search_attempted


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


def _relevance_review(agent_key: str, data: Mapping[str, Any]) -> OrchestratorOutputReviewScore:
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
    if metadata_gaps:
        suggestions = metadata_gaps
    elif score >= 85:
        suggestions = []
    else:
        suggestions = ["Tie the output more directly to the request and evidence."]
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
    relevance = _relevance_review(agent_key, data)
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
                "No model, network, email, Slack, CRM, LinkedIn, scheduling, or "
                "publishing calls were made."
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
    _, candidate = run_typed_sdk_sync(
        build_orchestrator_review_agent(model=model),
        json.dumps(payload, ensure_ascii=True, sort_keys=True),
        OrchestratorOutputReview,
        run_config=run_config,
        live=live,
        workflow_name="Keystone orchestrator LLM output review",
        trace_metadata={
            "agent": "orchestrator",
            "review_mode": "llm",
            "reviewed_agent": _review_agent_key(agent_name),
        },
    )
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
    _, candidate = run_typed_sdk_sync(
        build_orchestrator_agent(model=model),
        prompt,
        OrchestratorResult,
        run_config=run_config,
        live=live,
        workflow_name="Keystone orchestrator LLM routing",
        trace_metadata={"agent": "orchestrator", "routing_mode": "llm"},
    )
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
        return (
            provided
            if isinstance(provided, ManualRequestPlan)
            else ManualRequestPlan.model_validate(provided)
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
    if plan.intent == "blocked_send":
        result = _send_refusal(
            approved_context_present=approved_context_present,
            workflow_state=workflow_state,
        )
        result.audit_notes = [*result.audit_notes, *audit_notes]
        return result
    route = plan.target_agent
    if route in {"orchestrator", "clarification"}:
        return None
    if route == "outreach_composer":
        if not approved_context_present:
            result = _missing_outreach_context_refusal(workflow_state=workflow_state)
            result.audit_notes = [*result.audit_notes, *audit_notes]
            return result
        return _result(
            route="outreach_composer",
            rationale=plan.objective
            or "The manual request plan selected draft-only outreach from approved context.",
            approved_context_present=True,
            approval_scope=ApprovalScope.EXTERNAL_USE,
            approval_rationale=(
                "Manual request plan selected outreach; any draft remains pending "
                "external-use approval."
            ),
            routing_mode=plan.source
            if plan.source in {"llm", "llm_unavailable"}
            else "deterministic",
            workflow_state=workflow_state,
            audit_notes=audit_notes,
        )
    if route in {
        "gmail_triage",
        "business_research_analyst",
        "opportunity_scout",
        "chief_of_staff",
    }:
        return _result(
            route=route,
            rationale=plan.objective
            or f"The manual request plan selected {route} for this request.",
            artifact=("input_type", plan.target_type),
            routing_mode=plan.source
            if plan.source in {"llm", "llm_unavailable"}
            else "deterministic",
            workflow_state=workflow_state,
            audit_notes=audit_notes,
            retrieval_hint=_route_retrieval_hint(
                route,
                request_text=request_text,
            ),
        )
    return None


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
    state_context = (
        load_workflow_state_context(database_url=database_url)
        if database_url
        else _normalize_workflow_state(workflow_state)
    )
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
    )
    resolved_manual_plan = _manual_plan_for_request(
        request,
        provided=manual_plan,
        enabled=use_manual_plan or manual_plan is not None,
    )

    def finish(result: OrchestratorResult) -> OrchestratorResult:
        return _with_operator_feedback_request(
            result,
            include=include_operator_feedback_request,
        )

    if _SEND_RE.search(lower_text):
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

    if _OUTREACH_RE.search(lower_text):
        if _RESEARCH_RE.search(lower_text) and not approved_context_present:
            return finish(
                _result(
                    route="business_research_analyst",
                    rationale=(
                        "The request asks for research plus later outreach; research must "
                        "run first so any draft can use approved source-backed context."
                    ),
                    approval_scope=ApprovalScope.DRAFTING,
                    approval_state=ApprovalState.PENDING,
                    approval_rationale=(
                        "Outreach remains blocked until the research artifact is reviewed "
                        "and approved for drafting context."
                    ),
                    workflow_state=state_context,
                    retrieval_hint=_route_retrieval_hint(
                        "business_research_analyst",
                        request_text=text,
                    ),
                )
            )
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

    if _looks_like_email(request, text):
        return finish(
            _result(
                route="gmail_triage",
                rationale="The request contains email-like fields or message formatting.",
                artifact=("input_type", "email"),
                workflow_state=state_context,
            )
        )

    if _OPPORTUNITY_RE.search(lower_text):
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


def _build_read_only_specialist_tools() -> list[Any]:
    from keystone_agents.agents.business_research_analyst import (
        build_business_research_analyst_research_brief_agent,
    )

    business_research_agent = build_business_research_analyst_research_brief_agent()
    business_research_agent.tools = [
        tool
        for tool in business_research_agent.tools
        if _tool_name(tool) in _READ_ONLY_BUSINESS_RESEARCH_TOOL_NAMES
    ]

    as_tool = getattr(business_research_agent, "as_tool", None)
    if not callable(as_tool):
        return []

    return [
        as_tool(
            tool_name=BUSINESS_RESEARCH_TOOL_NAME,
            tool_description=(
                "Run the Business Research Analyst for internal, source-attributed "
                "research synthesis only. This tool is read-only and cannot send, publish, "
                "schedule, create approval items, or persist memory; Python gates and "
                "approval policy remain authoritative."
            ),
            max_turns=6,
        )
    ]


def build_orchestrator_agent(
    model: str | None = None,
    *,
    include_handoffs: bool = True,
    include_specialist_tools: bool | None = None,
) -> Agent:
    """Build the orchestrator agent with subordinate agent handoffs."""

    instructions = compose_instructions(
        "keystone_profile.md",
        "safety_policy.md",
        "skills.md",
        "tools.md",
        "orchestrator.md",
    )
    handoffs = [spec.build_agent() for spec in SPECIALIST_AGENT_SPECS] if include_handoffs else []
    specialist_tools = (
        _build_read_only_specialist_tools()
        if (
            include_specialist_tools
            if include_specialist_tools is not None
            else _env_flag_enabled(ORCHESTRATOR_SPECIALIST_TOOLS_ENV)
        )
        else []
    )
    agent = build_sdk_agent(
        name="orchestrator",
        instructions=instructions,
        output_type=OrchestratorResult,
        tools=[
            list_local_context_sources,
            search_local_context,
            read_local_context_file,
            retrieve_memory,
            airtable_get_base_schema,
            airtable_read_records,
            airtable_write_record,
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


def build_orchestrator_review_agent(model: str | None = None) -> Agent:
    """Build the orchestrator in output-review mode."""

    instructions = compose_instructions(
        "keystone_profile.md",
        "safety_policy.md",
        "skills.md",
        "tools.md",
        "orchestrator.md",
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
) -> TypedAgentRunResult[OrchestratorResult]:
    """Run the orchestrator through the shared typed SDK harness."""

    return run_typed_sdk_agent(
        agent=build_orchestrator_agent(
            model=model,
            include_handoffs=False,
            include_specialist_tools=include_specialist_tools,
        ),
        typed_input=_orchestrator_sdk_input(typed_input, live=live),
        output_type=OrchestratorResult,
        run_config=run_config,
        live=live,
        session=session,
    )
