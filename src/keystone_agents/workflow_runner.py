"""Deterministic WorkItem workflow runner for safe single-step advancement."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from email.utils import parseaddr
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlparse

from keystone_agents.agents.business_research_analyst import (
    build_company_research_queries,
    compare_company_profiles_for_decision,
    run_business_research_analyst_research_brief_sdk,
)
from keystone_agents.agents.chief_of_staff import (
    plan_chief_of_staff_request,
    run_chief_of_staff_sdk,
)
from keystone_agents.agents.gmail_triage import EmailFixture, triage_email_fixture
from keystone_agents.agents.opportunity_scout import scout_opportunities_fixture
from keystone_agents.agents.opportunity_search_planner import resolve_opportunity_search_plan
from keystone_agents.agents.orchestrator import review_specialist_output, route_request
from keystone_agents.agents.outreach_composer import (
    build_approved_outreach_drafting_context,
    build_outreach_composer_compact_synthesis_agent,
    compose_outreach_draft_fixture,
    compose_outreach_draft_llm_constrained,
    load_style_profile,
)
from keystone_agents.cli_sdk import jsonable
from keystone_agents.company_research import research_company_fixture
from keystone_agents.contact_enrichment import build_contact_enrichment_artifact
from keystone_agents.cost_tracking import parse_cost_tracking_directive
from keystone_agents.gmail_triage.execution_plan import infer_gmail_execution_plan
from keystone_agents.live_retrieval import (
    retrieve_company_profile_live,
    run_opportunity_scout_live,
)
from keystone_agents.memory import (
    MANAGER_LOOP_EFFICIENCY_METRIC_NAME,
    MANAGER_LOOP_EFFICIENCY_METRIC_VERSION,
    manager_loop_efficiency_memory_item,
    retrieval_tool_performance_memory_item,
)
from keystone_agents.models import OutreachComposerSDKInput, ResearchSDKInput
from keystone_agents.orchestrator.routing import looks_like_send_side_effect
from keystone_agents.run import run_retrieved_sdk_synthesis
from keystone_agents.response_synthesis import (
    format_user_response_synthesis,
    latest_user_request,
    synthesize_user_facing_work_item_response_sdk_result,
)
from keystone_agents.schemas.approval import ApprovalScope, ApprovalState
from keystone_agents.schemas.company_profile import CompanyProfile, SourceRecord
from keystone_agents.schemas.opportunity import (
    OpportunityRecord as ScoutOpportunityRecord,
    OpportunityScoutResult,
)
from keystone_agents.schemas.outreach import (
    OpportunityRecord as OutreachOpportunityRecord,
)
from keystone_agents.schemas.outreach import (
    OutreachLLMDraftPayload,
)
from keystone_agents.schemas.outreach import OutreachDraft
from keystone_agents.schemas.research import ResearchBrief
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkflowRunResult,
    WorkItem,
    WorkItemApprovalGate,
    WorkItemArtifactRef,
    WorkItemBlocker,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemStatus,
    WorkItemTarget,
    utc_now_iso,
)
from keystone_agents.sdk_sessions import (
    build_sdk_session,
    context_file_session_components,
    resolve_sdk_session_spec,
)
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env
from keystone_agents.tools.approval_tool import build_approval_queue_item
from keystone_agents.work_items import (
    add_blocker,
    attach_artifact,
    build_context_pack_for_route,
    create_or_load_work_item,
    derive_case_status,
    drafting_ready,
    infer_route_for_continue,
    normalize_target_text,
    opportunity_ready,
    record_event,
    research_ready,
    selected_artifacts,
    set_next_action,
)
from keystone_agents.zotero_research import (
    build_zotero_article_research_brief,
    build_zotero_collection_research_brief,
    build_zotero_live_source_context,
    extract_zotero_article_query,
    extract_zotero_collection_hint,
    looks_like_zotero_article_request,
    looks_like_zotero_collection_request,
)

DEFAULT_MANAGER_LOOP_MAX_STEPS = 3
DEFAULT_MANAGER_LOOP_MAX_REPAIRS_PER_ROUTE = 1
_MANAGER_LOOP_STOP_STATUSES = {
    WorkItemStatus.NEEDS_CONTEXT,
    WorkItemStatus.NEEDS_APPROVAL,
    WorkItemStatus.BLOCKED,
    WorkItemStatus.DONE,
    WorkItemStatus.ARCHIVED,
}
_SLACK_CONSERVATIVE_DEEP_RESEARCH_RE = re.compile(
    r"\b(?:deep research|deeper research|more research|research deeper|run again|"
    r"find contact|contact|email|linkedin)\b",
    flags=re.I,
)
_SLACK_COST_CONTROLLED_PROFILES = frozenset(
    {
        "slack_conservative",
        "slack-cost-conservative",
        "slack_context_light",
        "slack_manager_balanced",
        "slack_research_balanced",
        "slack_opportunity_balanced",
        "slack_research_deep",
    }
)
_SLACK_RUNTIME_ENV_HINTS = frozenset(
    {
        "KNI_SLACK_ENV_FILE",
        "KNI_BUSINESS_AGENTS_REPO",
        "KNI_BUSINESS_AGENTS_DATABASE_URL",
        "KNI_BUSINESS_AGENTS_LIVE_SEARCH",
        "KNI_BUSINESS_AGENTS_LIVE_SDK",
        "KNI_BUSINESS_AGENTS_BACKGROUND_RUNS",
    }
)


def _normalize_workflow_request_for_context(
    request: WorkflowRunRequest,
    *,
    store: SQLiteStore | None = None,
    work_item: WorkItem | None = None,
) -> WorkflowRunRequest:
    """Apply conservative defaults when a request is known to come from Slack context."""

    if _is_slack_conservative_cost_profile(request):
        return request
    context_work_item = work_item
    if context_work_item is None and request.work_item_id and store is not None:
        context_work_item = store.get_work_item(request.work_item_id)
    if not _request_has_slack_context(request, store=store, work_item=context_work_item):
        return request
    route = _request_route_for_slack_cost_profile(request, work_item=context_work_item)
    deep_research = route in {
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        WorkItemRoute.OPPORTUNITY_SCOUT.value,
    } and bool(_SLACK_CONSERVATIVE_DEEP_RESEARCH_RE.search(request.request_text or ""))
    defaults = _slack_cost_defaults_for_route(route, deep_research=deep_research)
    return request.model_copy(
        update={
            "cost_profile": defaults["cost_profile"],
            "allow_manager_loop_repair": bool(defaults["allow_manager_loop_repair"]),
            "include_contact_enrichment": bool(defaults["include_contact_enrichment"]),
            "hosted_web_search_max_calls": (
                request.hosted_web_search_max_calls
                if request.hosted_web_search_max_calls is not None
                else defaults["hosted_web_search_max_calls"]
            ),
            "reuse_existing_research": request.reuse_existing_research
            or bool(defaults["reuse_existing_research"]),
        }
    )


def _request_has_slack_context(
    request: WorkflowRunRequest,
    *,
    store: SQLiteStore | None = None,
    work_item: WorkItem | None = None,
) -> bool:
    if _context_payload_is_slack(request.external_context):
        return True
    if request.context_file_path and _context_file_is_slack_context(request.context_file_path):
        return True
    if work_item is not None and _work_item_has_slack_context(work_item):
        return True
    if request.work_item_id and store is not None:
        existing = store.get_work_item(request.work_item_id)
        return existing is not None and _work_item_has_slack_context(existing)
    if _request_has_slack_runtime_env():
        return True
    return False


def _request_has_slack_runtime_env() -> bool:
    """Return true when invoked by the Slack bridge without a context file."""

    return any(str(os.environ.get(key) or "").strip() for key in _SLACK_RUNTIME_ENV_HINTS)


def _request_route_for_slack_cost_profile(
    request: WorkflowRunRequest,
    *,
    work_item: WorkItem | None = None,
) -> str:
    if work_item is not None and work_item.current_route:
        route = str(work_item.current_route.value)
        if route and route != WorkItemRoute.ORCHESTRATOR.value:
            return route
    if request.requested_route is not None:
        return str(request.requested_route.value)
    plan = request.manual_request_plan
    if hasattr(plan, "model_dump"):
        plan_payload = plan.model_dump(mode="json")
    elif isinstance(plan, dict):
        plan_payload = plan
    else:
        plan_payload = {}
    known_routes = {route.value for route in WorkItemRoute}
    for key in ("target_agent", "requested_agent", "route", "intent"):
        value = str(plan_payload.get(key) or "").strip().lower().replace(" ", "_")
        if value in known_routes:
            return value
        if "business_research" in value or "research_analyst" in value:
            return WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
        if "opportunity" in value or "scout" in value:
            return WorkItemRoute.OPPORTUNITY_SCOUT.value
        if "gmail" in value or "email_triage" in value:
            return WorkItemRoute.GMAIL_TRIAGE.value
        if "outreach" in value or "composer" in value:
            return WorkItemRoute.OUTREACH_COMPOSER.value
        if "chief" in value or "staff" in value or "manager" in value:
            return WorkItemRoute.CHIEF_OF_STAFF.value
    return WorkItemRoute.ORCHESTRATOR.value


def _slack_cost_defaults_for_route(route: str, *, deep_research: bool) -> dict[str, object]:
    if deep_research:
        return {
            "cost_profile": "slack_research_deep",
            "allow_manager_loop_repair": True,
            "include_contact_enrichment": True,
            "hosted_web_search_max_calls": 2,
            "reuse_existing_research": False,
        }
    if route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value:
        return {
            "cost_profile": "slack_research_balanced",
            "allow_manager_loop_repair": False,
            "include_contact_enrichment": False,
            "hosted_web_search_max_calls": 1,
            "reuse_existing_research": True,
        }
    if route == WorkItemRoute.OPPORTUNITY_SCOUT.value:
        return {
            "cost_profile": "slack_opportunity_balanced",
            "allow_manager_loop_repair": False,
            "include_contact_enrichment": False,
            "hosted_web_search_max_calls": 2,
            "reuse_existing_research": True,
        }
    if route in {WorkItemRoute.CHIEF_OF_STAFF.value, WorkItemRoute.ORCHESTRATOR.value}:
        return {
            "cost_profile": "slack_manager_balanced",
            "allow_manager_loop_repair": False,
            "include_contact_enrichment": False,
            "hosted_web_search_max_calls": 1,
            "reuse_existing_research": True,
        }
    return {
        "cost_profile": "slack_context_light",
        "allow_manager_loop_repair": False,
        "include_contact_enrichment": False,
        "hosted_web_search_max_calls": 0,
        "reuse_existing_research": True,
    }


def _context_file_is_slack_context(context_file_path: str) -> bool:
    try:
        payload = json.loads(Path(context_file_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return _context_payload_is_slack(payload)


def _context_payload_is_slack(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    schema = str(payload.get("schema") or payload.get("schema_") or "").strip()
    return schema.startswith("keystone.slack.")


def _work_item_has_slack_context(work_item: WorkItem) -> bool:
    slack_context = work_item.target.metadata.get("slack_context")
    return isinstance(slack_context, dict) and bool(slack_context)


def advance_work_item(request: WorkflowRunRequest) -> WorkflowRunResult:
    """Advance a WorkItem by one deterministic, side-effect-safe step."""

    request = _normalize_workflow_request_for_context(request)
    store = SQLiteStore(request.database_url or database_url_from_env()) if request.save else None
    state_followup = _maybe_answer_manager_loop_state_followup(request, store=store)
    if state_followup is not None:
        return state_followup
    return _advance_work_item_one_step(request, synthesize_user_response=True)


def _advance_work_item_one_step(
    request: WorkflowRunRequest,
    *,
    synthesize_user_response: bool,
) -> WorkflowRunResult:
    """Advance one WorkItem step, optionally deferring final response synthesis."""

    store = SQLiteStore(request.database_url or database_url_from_env()) if request.save else None
    cost_directive = parse_cost_tracking_directive(request.request_text)
    cost_tracking_requested = bool(request.cost_tracking_requested or cost_directive.requested)
    input_text = (cost_directive.cleaned_text or request.request_text).strip()
    request = request.model_copy(update={"request_text": input_text})
    route = _select_route(request, input_text, store)
    work_item = create_or_load_work_item(
        store=store,
        request_text=input_text,
        work_item_id=request.work_item_id,
        route=route,
    )
    if input_text and input_text.lower() not in {"continue", "resume"}:
        work_item = work_item.model_copy(update={"request_text": input_text})
    work_item = _apply_manual_request_plan(work_item, request.manual_request_plan)
    external_context = _load_external_context(request)
    work_item = _apply_external_context(
        work_item,
        external_context,
        context_file_path=request.context_file_path,
    )
    work_item = _attach_slack_prompt_context(work_item, latest_request=input_text)
    request = _normalize_workflow_request_for_context(request, work_item=work_item)
    sdk_session_spec = _sdk_session_spec_for_work_item(request, work_item)
    if route != WorkItemRoute.ORCHESTRATOR:
        work_item = work_item.model_copy(
            update={
                "current_route": route,
                "kind": create_or_load_work_item(
                    store=None,
                    request_text=work_item.request_text or input_text,
                    route=route,
                ).kind,
            }
        )
    work_item = _apply_requested_context_manifest(
        work_item,
        request,
        external_context=external_context,
    )

    context_pack = build_context_pack_for_route(work_item, route, store=store)
    context_source_manifest = _context_source_manifest_event_payload(work_item)
    record_event(
        work_item,
        event_type="advance_started",
        summary=f"Advancing via {route.value}.",
        metadata={
            "live_search": request.live_search,
            "live_sdk": request.live_sdk,
            "cost_profile": request.cost_profile,
            "allow_manager_loop_repair": request.allow_manager_loop_repair,
            "include_contact_enrichment": request.include_contact_enrichment,
            "hosted_web_search_max_calls": request.hosted_web_search_max_calls,
            "reuse_existing_research": request.reuse_existing_research,
            "cost_tracking_requested": cost_tracking_requested,
            "sdk_session": sdk_session_spec.log_metadata(),
            "context_pack": {
                "pack_type": context_pack.pack_type,
                "ready": context_pack.ready,
                "readiness_gates": [
                    {
                        "name": gate.name,
                        "ready": gate.ready,
                        "required": gate.required,
                    }
                    for gate in context_pack.readiness_gates
                ],
            },
            "manual_request_plan": _manual_plan_event_payload(request.manual_request_plan),
            "orchestrator_preflight": _orchestrator_preflight_event_payload(
                request.orchestrator_preflight
            ),
            "external_context": _external_context_event_payload(
                external_context,
                context_file_path=request.context_file_path,
            ),
            "context_source_manifest": context_source_manifest,
        },
        store=store,
    )
    _record_preflight_sdk_cost_events(work_item, request=request, store=store)

    sdk_session = build_sdk_session(sdk_session_spec) if request.live_sdk else None

    if route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST:
        result = _advance_research(
            work_item,
            request=request,
            store=store,
            sdk_session=sdk_session,
        )
    elif route == WorkItemRoute.OPPORTUNITY_SCOUT:
        result = _advance_opportunity(work_item, request=request, store=store)
    elif route == WorkItemRoute.OUTREACH_COMPOSER:
        result = _advance_outreach(work_item, request=request, store=store)
    elif route == WorkItemRoute.GMAIL_TRIAGE:
        result = _advance_gmail_triage(work_item, request=request, store=store)
    elif route == WorkItemRoute.CHIEF_OF_STAFF:
        result = _advance_chief_of_staff(
            work_item,
            request=request,
            store=store,
            sdk_session=sdk_session,
        )
    else:
        result = _block_unsupported_route(work_item, route=route, store=store)

    final_context_pack = build_context_pack_for_route(result.work_item, result.route, store=store)
    result = result.model_copy(
        update={
            "manual_request_plan": request.manual_request_plan,
            "orchestrator_preflight": request.orchestrator_preflight,
            "context_pack": final_context_pack.model_dump(mode="json"),
        }
    )
    if synthesize_user_response:
        result = _maybe_synthesize_user_facing_response(
            result,
            request=request,
            sdk_session=sdk_session,
            store=store,
        )
    if store is not None:
        store.save_work_item(result.work_item)
    return result


def advance_work_item_manager_loop(
    request: WorkflowRunRequest,
    *,
    max_steps: int = DEFAULT_MANAGER_LOOP_MAX_STEPS,
    feedback_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> WorkflowRunResult:
    """Advance a WorkItem through a bounded manager loop without new route schemas.

    The loop keeps specialist outputs and WorkItems as the durable contract. It
    adds only compact review and loop-decision events, then stops at safety gates,
    terminal states, or when the operator did not ask for a multi-step workflow.
    """

    step_limit = max(1, min(5, int(max_steps or DEFAULT_MANAGER_LOOP_MAX_STEPS)))
    started_at = perf_counter()
    store = SQLiteStore(request.database_url or database_url_from_env()) if request.save else None
    request = _normalize_workflow_request_for_context(request, store=store)
    state_followup = _maybe_answer_manager_loop_state_followup(request, store=store)
    if state_followup is not None:
        return state_followup
    current_request = request
    final_result: WorkflowRunResult | None = None
    loop_steps: list[dict[str, Any]] = []
    repair_attempts_by_route: dict[str, int] = {}

    for step_index in range(1, step_limit + 1):
        result = _advance_work_item_one_step(current_request, synthesize_user_response=False)
        result = _review_manager_loop_step(
            result,
            original_request=request,
            step_index=step_index,
            store=store,
            feedback_callback=feedback_callback,
            defer_block_for_repair=_manager_loop_can_consider_repair(
                result,
                original_request=request,
                repair_attempts_by_route=repair_attempts_by_route,
            ),
        )
        latest_review = _manager_loop_latest_review(result.work_item)
        loop_steps.append(_manager_loop_step_summary(result, step_index=step_index))
        if _manager_loop_review_requested_repair(latest_review):
            repair_attempts_by_route[result.route.value] = (
                repair_attempts_by_route.get(result.route.value, 0) + 1
            )
            result = _attempt_manager_loop_repair(
                result,
                original_request=request,
                step_index=step_index,
                store=store,
                feedback_callback=feedback_callback,
            )
            loop_steps.append(
                _manager_loop_step_summary(result, step_index=step_index, repair=True)
            )
        final_result = result
        stop_reason = _manager_loop_stop_reason(
            original_request=request,
            result=result,
            loop_steps=loop_steps,
            step_index=step_index,
            max_steps=step_limit,
        )
        if stop_reason:
            final_result = _finalize_manager_loop_result(
                result,
                original_request=request,
                stop_reason=stop_reason,
                loop_steps=loop_steps,
                store=store,
                feedback_callback=feedback_callback,
            )
            break
        current_request = request.model_copy(
            update={
                "request_text": "continue",
                "work_item_id": result.work_item.id,
                "requested_route": None,
            }
        )

    if final_result is None:
        final_result = _advance_work_item_one_step(request, synthesize_user_response=False)
    final_sdk_session = (
        build_sdk_session(_sdk_session_spec_for_work_item(request, final_result.work_item))
        if request.live_sdk
        else None
    )
    final_result = _maybe_synthesize_user_facing_response(
        final_result,
        request=request,
        sdk_session=final_sdk_session,
        store=store,
    )
    final_result = _record_manager_loop_efficiency_metrics(
        final_result,
        original_request=request,
        loop_steps=loop_steps,
        repair_attempts_by_route=repair_attempts_by_route,
        elapsed_seconds=perf_counter() - started_at,
        store=store,
    )
    return final_result


def _maybe_answer_manager_loop_state_followup(
    request: WorkflowRunRequest,
    *,
    store: SQLiteStore | None,
) -> WorkflowRunResult | None:
    if store is None or not request.work_item_id:
        return None
    full_request = str(request.request_text or "").strip()
    latest_request = latest_user_request(full_request).strip()
    if not latest_request or latest_request == full_request:
        return None
    if not _looks_like_existing_state_followup(latest_request):
        return None
    work_item = store.get_work_item(request.work_item_id)
    if work_item is None:
        return None

    cost_directive = parse_cost_tracking_directive(latest_request)
    focused_request = (cost_directive.cleaned_text or latest_request).strip()
    cost_tracking_requested = bool(request.cost_tracking_requested or cost_directive.requested)
    events = store.list_work_item_events(work_item.id)
    summary = _render_existing_state_followup_answer(
        work_item,
        latest_request=focused_request,
        events=events,
        cost_tracking_requested=cost_tracking_requested,
    )
    record_event(
        work_item,
        event_type="manager_loop_state_followup_answered",
        actor="orchestrator",
        summary="Answered same-thread follow-up from existing WorkItem state.",
        metadata={
            "latest_user_request": focused_request,
            "cost_tracking_requested": cost_tracking_requested,
            "source": "existing_work_item_state",
        },
        store=store,
    )
    _record_preflight_sdk_cost_events(work_item, request=request, store=store)
    return WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.ORCHESTRATOR,
        status=work_item.status,
        advanced=True,
        artifact_refs=list(work_item.artifact_refs),
        blockers=[blocker for blocker in work_item.blockers if not blocker.resolved],
        next_action=work_item.next_action,
        human_summary=summary,
        audit_notes=[
            "Answered from existing WorkItem state; no specialist, retrieval, or drafting step ran."
        ],
        manual_request_plan=request.manual_request_plan,
        orchestrator_preflight=request.orchestrator_preflight,
    )


def _looks_like_existing_state_followup(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    if not normalized:
        return False
    if re.search(
        r"\b(?:answer only from|use only|based only on)\b.*\b(?:prior|previous|existing|run state|workitem)\b",
        normalized,
    ):
        return True
    if "without doing more research" in normalized or "without new research" in normalized:
        return True
    return bool(
        re.search(
            r"\b(?:why|explain|did|was|were|what ran|what happened|next safe step)\b",
            normalized,
        )
        and re.search(r"\b(?:blocked|label|status|prior run|previous run|orchestrator|execute|executed|ran)\b", normalized)
    )


def _render_existing_state_followup_answer(
    work_item: WorkItem,
    *,
    latest_request: str,
    events: list[Any],
    cost_tracking_requested: bool = False,
) -> str:
    route_sequence = _manager_loop_event_route_sequence(events)
    artifacts = [
        ref
        for ref in work_item.artifact_refs
        if ref.artifact_type != "orchestrator_plan_summary"
    ]
    unresolved = [blocker for blocker in work_item.blockers if not blocker.resolved]
    advisory = _manager_loop_event_advisory_limitations(events)
    did_execute = bool(route_sequence or artifacts)
    if route_sequence:
        execution_line = (
            "Orchestrator selected or managed the route; the prior WorkItem then ran "
            "specialist step(s): "
            + " -> ".join(route_sequence)
            + "."
        )
    elif artifacts:
        execution_line = "It produced specialist artifacts from prior WorkItem state."
    else:
        execution_line = "I do not see evidence of specialist execution in this WorkItem state."

    lines = [
        "Prior run state",
        "",
        execution_line if did_execute else execution_line,
        f"Current WorkItem status: {work_item.status.value}.",
    ]
    if unresolved:
        lines.append(
            "Open hard blocker(s): "
            + "; ".join(f"{blocker.code}: {blocker.message}" for blocker in unresolved[:3])
        )
    elif advisory:
        lines.append(
            "The useful output is limited rather than hard-blocked. Limitation(s): "
            + "; ".join(f"{item['code']}: {item['message']}" for item in advisory[:3])
        )
    else:
        lines.append("No open hard blocker is recorded on the WorkItem.")

    next_step = (
        work_item.next_action.description
        if work_item.next_action is not None and work_item.next_action.description
        else "Review the existing artifact, choose the next specialist step, then run only that step."
    )
    lines.extend(
        [
            "",
            "Next safe step:",
            f"- {next_step}",
            "",
            "No new research, drafting, external write, or send action was run for this follow-up.",
        ]
    )
    if latest_request and cost_tracking_requested:
        lines.append("Cost tracking remains backend/audit-only for this path.")
    return "\n".join(lines).strip()


def _manager_loop_event_route_sequence(events: list[Any]) -> list[str]:
    for event in reversed(events):
        if getattr(event, "event_type", "") != "manager_loop_efficiency":
            continue
        metadata = getattr(event, "metadata", {}) or {}
        sequence = metadata.get("route_sequence")
        if isinstance(sequence, list):
            return [str(route) for route in sequence if str(route or "").strip()]
    for event in reversed(events):
        if getattr(event, "event_type", "") != "manager_loop_completed":
            continue
        metadata = getattr(event, "metadata", {}) or {}
        steps = metadata.get("steps")
        if isinstance(steps, list):
            return [
                str(step.get("route"))
                for step in steps
                if isinstance(step, dict) and str(step.get("route") or "").strip()
            ]
    return []


def _manager_loop_event_advisory_limitations(events: list[Any]) -> list[dict[str, str]]:
    for event in reversed(events):
        if getattr(event, "event_type", "") != "manager_loop_completed":
            continue
        metadata = getattr(event, "metadata", {}) or {}
        limitations = metadata.get("advisory_limitations")
        if not isinstance(limitations, list):
            continue
        normalized: list[dict[str, str]] = []
        for item in limitations:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "code": str(item.get("code") or "").strip(),
                    "message": str(item.get("message") or "").strip(),
                }
            )
        return [item for item in normalized if item["code"] or item["message"]]
    return []


def _record_manager_loop_efficiency_metrics(
    result: WorkflowRunResult,
    *,
    original_request: WorkflowRunRequest,
    loop_steps: list[dict[str, Any]],
    repair_attempts_by_route: dict[str, int],
    elapsed_seconds: float,
    store: SQLiteStore | None,
) -> WorkflowRunResult:
    elapsed = round(float(elapsed_seconds), 3)
    step_count = len(loop_steps)
    specialist_step_count = sum(1 for step in loop_steps if not step.get("repair"))
    repair_count = sum(1 for step in loop_steps if step.get("repair"))
    blocker_count = len(result.blockers)
    completion_without_blockers = bool(result.advanced and blocker_count == 0)
    metrics = {
        "schema": "keystone.manager_loop_efficiency.v1",
        "metric_name": MANAGER_LOOP_EFFICIENCY_METRIC_NAME,
        "metric_version": MANAGER_LOOP_EFFICIENCY_METRIC_VERSION,
        "elapsed_seconds": elapsed,
        "latency_bucket": _manager_loop_latency_bucket(elapsed),
        "step_count": step_count,
        "specialist_step_count": specialist_step_count,
        "repair_count": repair_count,
        "repair_rate": round(repair_count / max(1, step_count), 3),
        "repair_attempts_by_route": dict(repair_attempts_by_route),
        "route_sequence": [str(step.get("route") or "") for step in loop_steps],
        "final_route": result.route.value,
        "final_status": result.status.value,
        "advanced": bool(result.advanced),
        "artifact_count": len(result.artifact_refs),
        "blocker_count": blocker_count,
        "live_search": bool(original_request.live_search),
        "live_sdk": bool(original_request.live_sdk),
        "final_synthesis_executed": any(
            "Live user-facing response synthesis executed." == note
            for note in result.audit_notes
        ),
        "seconds_per_specialist_step": round(elapsed / max(1, specialist_step_count), 3),
        "completion_without_blockers": completion_without_blockers,
    }
    metrics["efficiency_signal"] = _manager_loop_efficiency_signal(metrics)
    memory_id = _persist_manager_loop_efficiency_memory(
        metrics,
        object_id=f"work_item:{result.work_item.id}",
        store=store,
    )
    if memory_id is not None:
        metrics["memory_id"] = memory_id
    target_metadata = dict(result.work_item.target.metadata)
    target_metadata["manager_loop_efficiency"] = metrics
    note = (
        "Manager loop efficiency: "
        f"{metrics['step_count']} step(s), {metrics['repair_count']} repair(s), "
        f"{metrics['elapsed_seconds']}s elapsed."
    )
    work_item = result.work_item.model_copy(
        update={
            "target": result.work_item.target.model_copy(update={"metadata": target_metadata}),
            "audit_notes": list(dict.fromkeys([*result.work_item.audit_notes, note])),
        }
    ).touch()
    if store is not None:
        record_event(
            work_item,
            event_type="manager_loop_efficiency",
            actor="orchestrator",
            summary=note,
            metadata=metrics,
            store=store,
        )
        store.save_work_item(work_item)
    return result.model_copy(
        update={
            "work_item": work_item,
            "audit_notes": list(dict.fromkeys([*result.audit_notes, note])),
        }
    )


def _manager_loop_latency_bucket(elapsed_seconds: float) -> str:
    if elapsed_seconds < 5:
        return "under_5s"
    if elapsed_seconds < 15:
        return "5_to_15s"
    if elapsed_seconds < 45:
        return "15_to_45s"
    if elapsed_seconds < 120:
        return "45_to_120s"
    return "over_120s"


def _manager_loop_efficiency_signal(metrics: dict[str, Any]) -> str:
    if not metrics.get("completion_without_blockers"):
        return "incomplete_or_blocked"
    if int(metrics.get("repair_count") or 0) > 0:
        return "completed_after_repair"
    if str(metrics.get("latency_bucket") or "") in {"under_5s", "5_to_15s"}:
        return "fast_completion"
    return "completed_high_latency"


def _review_manager_loop_step(
    result: WorkflowRunResult,
    *,
    original_request: WorkflowRunRequest,
    step_index: int,
    store: SQLiteStore | None,
    feedback_callback: Callable[[str, dict[str, Any]], None] | None,
    defer_block_for_repair: bool = False,
) -> WorkflowRunResult:
    focused_request = latest_user_request(original_request.request_text)
    review_payload = {
        "status": result.status.value,
        "advanced": result.advanced,
        "full_user_request": original_request.request_text,
        "latest_user_request": focused_request,
        "human_summary": result.human_summary,
        "artifact_refs": [
            {
                "artifact_type": ref.artifact_type,
                "source_agent": ref.source_agent,
                "approval_state": ref.approval_state,
                "title": ref.title,
                "summary": ref.summary,
            }
            for ref in result.artifact_refs[:8]
        ],
        "blockers": [
            {"code": blocker.code, "message": blocker.message, "severity": blocker.severity}
            for blocker in result.blockers[:6]
        ],
        "next_action": (
            result.next_action.model_dump(mode="json") if result.next_action is not None else None
        ),
        "send_enabled": False,
        "can_send_email": False,
    }
    review = review_specialist_output(
        agent_name=result.route.value,
        output=review_payload,
        request_summary=focused_request or original_request.request_text,
        run_type="work_item_manager_loop",
    )
    note = (
        f"Manager loop review step {step_index}: "
        f"{review.status} ({review.overall_score}/100)."
    )
    review_context = {
        "step": step_index,
        "route": result.route.value,
        "status": result.status.value,
        "latest_user_request": focused_request,
        "review_status": review.status,
        "overall_score": review.overall_score,
        "approval_boundary_ok": review.approval_boundary_ok,
        "observed_gaps": review.observed_gaps[:6],
        "recommended_next_step": review.recommended_next_step,
    }
    depth_gap = _manager_loop_business_research_depth_gap(original_request, result)
    if depth_gap:
        review_context["review_status"] = "fail"
        review_context["overall_score"] = min(int(review_context["overall_score"]), 55)
        review_context["observed_gaps"] = [
            *list(review_context.get("observed_gaps") or []),
            depth_gap,
        ][:6]
        review_context["recommended_next_step"] = (
            "Deepen Business Research with current-year independent sources before "
            "treating the profile as decision-ready."
        )
        note = (
            f"Manager loop review step {step_index}: "
            f"{review_context['review_status']} ({review_context['overall_score']}/100)."
        )
    review_blocking = _manager_review_should_block(
        review_context,
        result,
        original_request=original_request,
    )
    repair_eligible = bool(
        review_blocking
        and defer_block_for_repair
        and _manager_loop_repair_allowed(
            review_context,
            result,
            original_request=original_request,
        )
    )
    review_decision = (
        "repair"
        if repair_eligible
        else "block"
        if review_blocking
        else "pass"
        if review_context["review_status"] == "pass"
        else "warn"
    )
    review_context.update(
        {
            "blocking": review_blocking and not repair_eligible,
            "repair_eligible": repair_eligible,
            "advisory": (
                review_context["review_status"] != "pass"
                and not review_blocking
                and not repair_eligible
            ),
            "review_decision": review_decision,
        }
    )
    target_metadata = dict(result.work_item.target.metadata)
    prior_reviews = [
        item
        for item in target_metadata.get("orchestrator_reviews", [])
        if isinstance(item, dict)
    ]
    target_metadata["orchestrator_reviews"] = [*prior_reviews, review_context][-5:]
    work_item = result.work_item.model_copy(
        update={
            "target": result.work_item.target.model_copy(
                update={"metadata": target_metadata}
            ),
            "audit_notes": list(dict.fromkeys([*result.work_item.audit_notes, note])),
        }
    ).touch()
    blockers = list(result.blockers)
    next_action = result.next_action
    if repair_eligible:
        next_action = WorkItemNextAction(
            action="repair_or_deepen_specialist_output",
            agent=result.route,
            description=str(review_context.get("recommended_next_step") or "")
            or "Repair the specialist output using the Orchestrator review gaps.",
        )
        work_item = set_next_action(work_item, next_action)
    elif review_blocking:
        blocker = WorkItemBlocker(
            code="manager_loop_review_failed",
            message=(
                "Orchestrator review marked this specialist step as failed; "
                "repair or deepen the run before treating the artifact as usable."
            ),
        )
        work_item = add_blocker(work_item, blocker)
        next_action = WorkItemNextAction(
            action="repair_or_deepen_specialist_output",
            agent=result.route,
            description=str(review_context.get("recommended_next_step") or "")
            or "Repair the specialist output using the Orchestrator review gaps.",
        )
        work_item = set_next_action(work_item, next_action)
        work_item = work_item.model_copy(update={"status": derive_case_status(work_item)}).touch()
        blockers = [*blockers, blocker]
    record_event(
        work_item,
        event_type="manager_loop_review",
        summary=note,
        metadata={
            **review_context,
            "planner_memo": _manager_loop_planner_memo(original_request, result),
        },
        store=store,
    )
    if feedback_callback is not None:
        feedback_callback(
            "manager_loop_review",
            {
                "step": step_index,
                "route": result.route.value,
                "status": result.status.value,
                "review_status": review_context["review_status"],
                "overall_score": review_context["overall_score"],
                "blocking": review_blocking and not repair_eligible,
                "repair_eligible": repair_eligible,
                "advisory": review_context["advisory"],
                "review_decision": review_decision,
                "recommended_next_step": review_context["recommended_next_step"],
            },
        )
    if store is not None:
        store.save_work_item(work_item)
    return result.model_copy(
        update={
            "work_item": work_item,
            "audit_notes": list(dict.fromkeys([*result.audit_notes, note])),
            "status": work_item.status,
            "blockers": blockers,
            "next_action": next_action,
        }
    )


def _manager_loop_can_consider_repair(
    result: WorkflowRunResult,
    *,
    original_request: WorkflowRunRequest,
    repair_attempts_by_route: dict[str, int],
) -> bool:
    return (
        original_request.allow_manager_loop_repair
        and repair_attempts_by_route.get(result.route.value, 0)
        < DEFAULT_MANAGER_LOOP_MAX_REPAIRS_PER_ROUTE
    )


def _manager_loop_latest_review(work_item: WorkItem) -> dict[str, Any]:
    reviews = work_item.target.metadata.get("orchestrator_reviews", [])
    if not isinstance(reviews, list) or not reviews:
        return {}
    latest = reviews[-1]
    return latest if isinstance(latest, dict) else {}


def _manager_loop_review_requested_repair(review_context: dict[str, Any]) -> bool:
    return str(review_context.get("review_decision") or "") == "repair" and bool(
        review_context.get("repair_eligible")
    )


def _manager_loop_repair_allowed(
    review_context: dict[str, Any],
    result: WorkflowRunResult,
    *,
    original_request: WorkflowRunRequest,
) -> bool:
    if review_context.get("approval_boundary_ok") is False:
        return False
    if looks_like_send_side_effect(original_request.request_text):
        return False
    if not result.advanced or result.blockers:
        return False
    if result.status in _MANAGER_LOOP_STOP_STATUSES:
        return False
    if result.next_action is not None and result.next_action.requires_approval:
        return False
    return _manager_review_failure_is_authoritative(review_context, result)


def _attempt_manager_loop_repair(
    result: WorkflowRunResult,
    *,
    original_request: WorkflowRunRequest,
    step_index: int,
    store: SQLiteStore | None,
    feedback_callback: Callable[[str, dict[str, Any]], None] | None,
) -> WorkflowRunResult:
    review_context = _manager_loop_latest_review(result.work_item)
    repair_payload = {
        "step": step_index,
        "route": result.route.value,
        "work_item_id": result.work_item.id,
        "review_status": review_context.get("review_status"),
        "observed_gaps": list(review_context.get("observed_gaps") or [])[:6],
        "recommended_next_step": review_context.get("recommended_next_step") or "",
        "send_enabled": False,
    }
    record_event(
        result.work_item,
        event_type="manager_loop_repair_started",
        summary=(
            "Orchestrator requested one bounded specialist repair pass for "
            f"{result.route.value}."
        ),
        metadata=repair_payload,
        store=store,
    )
    if feedback_callback is not None:
        feedback_callback("manager_loop_repair_started", repair_payload)
    repair_request = original_request.model_copy(
        update={
            "request_text": original_request.request_text or result.work_item.request_text,
            "work_item_id": result.work_item.id,
            "requested_route": result.route,
        }
    )
    repaired = _advance_work_item_one_step(repair_request, synthesize_user_response=False)
    repaired = _review_manager_loop_step(
        repaired,
        original_request=original_request,
        step_index=step_index,
        store=store,
        feedback_callback=feedback_callback,
        defer_block_for_repair=False,
    )
    completed_payload = {
        "step": step_index,
        "route": repaired.route.value,
        "work_item_id": repaired.work_item.id,
        "status": repaired.status.value,
        "review_decision": _manager_loop_latest_review(repaired.work_item).get(
            "review_decision"
        ),
        "send_enabled": False,
    }
    record_event(
        repaired.work_item,
        event_type="manager_loop_repair_completed",
        summary=f"Bounded specialist repair pass completed for {repaired.route.value}.",
        metadata=completed_payload,
        store=store,
    )
    if feedback_callback is not None:
        feedback_callback("manager_loop_repair_completed", completed_payload)
    return repaired


def _manager_review_should_block(
    review_context: dict[str, Any],
    result: WorkflowRunResult,
    *,
    original_request: WorkflowRunRequest,
) -> bool:
    if str(review_context.get("review_status") or "") != "fail":
        return False
    if not result.advanced or result.blockers:
        return False
    if result.status in _MANAGER_LOOP_STOP_STATUSES:
        return False
    if not _manager_review_failure_is_authoritative(review_context, result):
        return False
    if (
        result.next_action is not None
        and result.next_action.agent not in {None, result.route}
        and _operator_requested_manager_continuation(
            original_request.request_text,
            next_action_agent=result.next_action.agent,
        )
    ):
        return False
    return True


def _manager_loop_stop_reason(
    *,
    original_request: WorkflowRunRequest,
    result: WorkflowRunResult,
    loop_steps: list[dict[str, Any]],
    step_index: int,
    max_steps: int,
) -> str:
    if not result.advanced:
        return "stopped because the current step did not safely advance"
    if result.status in _MANAGER_LOOP_STOP_STATUSES:
        return f"stopped at WorkItem status {result.status.value}"
    if step_index >= max_steps:
        return f"stopped at manager loop max_steps={max_steps}"
    if result.next_action is None:
        return "stopped because no next action was available"
    if result.next_action.requires_approval:
        return "stopped because the next action requires approval"
    if result.next_action.agent in {None, result.route}:
        return "stopped because the next action did not require a distinct specialist"
    previous_routes = {
        str(step.get("route") or "")
        for step in loop_steps[:-1]
        if str(step.get("route") or "")
    }
    if result.next_action.agent.value in previous_routes:
        return "stopped before repeating a specialist already used in this manager loop"
    if not _operator_requested_manager_continuation(
        original_request.request_text,
        next_action_agent=result.next_action.agent,
    ):
        return "stopped after one specialist step; no multi-step workflow was requested"
    return ""


def _operator_requested_manager_continuation(
    text: str,
    *,
    next_action_agent: WorkItemRoute | None = None,
) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    if not normalized:
        return False
    if next_action_agent == WorkItemRoute.OPPORTUNITY_SCOUT and re.search(
        r"\b(?:whether|if)\b.*\bbefore\s+opportunity\s+scout\s+should\s+act\b"
        r"|\bbefore\s+opportunity\s+scout\s+should\s+act\b",
        normalized,
    ):
        return False
    broad_continuation_markers = (
        "multi-step",
        "multistep",
        "end to end",
        "end-to-end",
        "workflow",
        "loop",
        "continue until",
    )
    sequencing_markers = (
        " and then ",
        " then ",
        "after that",
        "next,",
    )
    padded = f" {normalized} "
    if any(marker in padded for marker in broad_continuation_markers):
        return True
    if any(marker in padded for marker in sequencing_markers):
        return _manager_loop_mentions_next_agent(padded, next_action_agent)
    if " and " not in padded:
        return False
    return _manager_loop_mentions_next_agent(padded, next_action_agent)


def _manager_loop_mentions_next_agent(
    padded_normalized_text: str,
    next_action_agent: WorkItemRoute | None,
) -> bool:
    if next_action_agent == WorkItemRoute.OPPORTUNITY_SCOUT:
        return _manager_continuation_has_marker(
            padded_normalized_text,
            (
                r"\bopportunit",
                r"\bscout\b",
                r"\bleads?\b",
                r"\bcandidates?\b",
                r"\bpipeline\b",
                r"\bprospects?\b",
            ),
        )
    if next_action_agent == WorkItemRoute.BUSINESS_RESEARCH_ANALYST:
        return _manager_continuation_has_marker(
            padded_normalized_text,
            (
                r"\bresearch\s+(?:it|them|that|the\s+company|the\s+candidate|"
                r"candidate|company|top|best|selected|shortlist)",
                r"\b(?:company|candidate|target)\s+(?:research|profile)\b",
                r"\bprofile\s+(?:it|them|that|the\s+company|the\s+candidate|"
                r"candidate|company|top|best|selected|shortlist)",
                r"\bsource[- ]backed\b",
                r"\bevidence[- ]based\b",
                r"\bsource\s+(?:the\s+)?(?:company|candidate|claims|facts)",
            ),
        )
    if next_action_agent == WorkItemRoute.OUTREACH_COMPOSER:
        return _manager_continuation_has_marker(
            padded_normalized_text,
            (
                r"\bdraft\b",
                r"\boutreach\b",
                r"\bemail\b",
                r"\blinkedin\b",
                r"\bmessage\b",
                r"\bfollow\s+up\b",
            ),
        )
    return False


def _manager_continuation_has_marker(padded_normalized_text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, padded_normalized_text) for pattern in patterns)


def _manager_loop_planner_memo(
    request: WorkflowRunRequest,
    result: WorkflowRunResult,
) -> dict[str, Any]:
    manual = _manual_plan_event_payload(request.manual_request_plan)
    preflight = _orchestrator_preflight_event_payload(request.orchestrator_preflight)
    next_action = result.next_action.model_dump(mode="json") if result.next_action else None
    return {
        "request_summary": request.request_text[:240],
        "manual_plan": manual,
        "orchestrator_preflight": preflight,
        "chosen_route": result.route.value,
        "chosen_next_action": next_action,
        "missing_context": [blocker.message for blocker in result.blockers[:4]],
    }


def _manager_loop_step_summary(
    result: WorkflowRunResult,
    *,
    step_index: int,
    repair: bool = False,
) -> dict[str, Any]:
    return {
        "step": step_index,
        "repair": repair,
        "route": result.route.value,
        "status": result.status.value,
        "advanced": result.advanced,
        "artifact_types": [ref.artifact_type for ref in result.artifact_refs],
        "next_action": result.next_action.action if result.next_action else "",
    }


def _specialist_orchestrator_context_payload(
    request: WorkflowRunRequest,
    work_item: WorkItem,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "raw_request": request.request_text or work_item.request_text,
        "work_item_id": work_item.id,
        "work_item_route": work_item.current_route.value,
        "side_effect_policy": "draft_or_read_only; no sends/posts/writes without approval gates",
    }
    manual_plan = _manual_plan_event_payload(request.manual_request_plan)
    if manual_plan:
        payload["manual_request_plan"] = manual_plan
    preflight = _orchestrator_preflight_event_payload(request.orchestrator_preflight)
    if preflight:
        payload["orchestrator_preflight"] = preflight
    checklist = _specialist_response_quality_checklist(request, work_item)
    if checklist:
        payload["response_quality_checklist"] = checklist
    feedback = _specialist_orchestrator_feedback_payload(work_item)
    if feedback:
        payload["orchestrator_feedback"] = feedback
    context_source_manifest = work_item.target.metadata.get("context_source_manifest")
    if isinstance(context_source_manifest, dict):
        payload["context_source_manifest"] = context_source_manifest
    slack_context = work_item.target.metadata.get("slack_context")
    if isinstance(slack_context, dict):
        payload["slack_context"] = {
            key: slack_context[key]
            for key in (
                "channel_id",
                "channel_name",
                "selected_message_ts",
                "thread_ts",
                "request_ts",
                "thread_fetch_status",
                "permalink",
                "read_context",
                "context_window_days",
                "channel_history_policy",
                "thread_transcript",
                "latest_user_follow_up",
                "prompt_context_layout",
            )
            if slack_context.get(key) not in (None, "", [], {})
        }
    return payload


def _specialist_orchestrator_feedback_payload(work_item: WorkItem) -> dict[str, Any]:
    reviews = work_item.target.metadata.get("orchestrator_reviews", [])
    if not isinstance(reviews, list):
        return {}
    route = work_item.current_route.value
    for review in reversed(reviews):
        if not isinstance(review, dict):
            continue
        if str(review.get("route") or "") != route:
            continue
        if str(review.get("review_status") or "") == "pass":
            continue
        gaps = [str(gap) for gap in list(review.get("observed_gaps") or [])[:6] if str(gap)]
        return {
            "review_status": str(review.get("review_status") or ""),
            "review_decision": str(review.get("review_decision") or ""),
            "overall_score": review.get("overall_score"),
            "observed_gaps": gaps,
            "recommended_next_step": str(review.get("recommended_next_step") or ""),
            "repair_instruction": (
                "Address these Orchestrator review gaps in this pass. If they cannot be "
                "resolved with available context and tools, explain the remaining blocker "
                "precisely instead of presenting the prior answer as complete."
            ),
        }
    return {}


def _specialist_response_quality_checklist(
    request: WorkflowRunRequest,
    work_item: WorkItem,
) -> list[str]:
    text = str(request.request_text or work_item.request_text or "")
    normalized = text.lower()
    route = work_item.current_route.value
    checklist = [
        (
            "Use this memo as advisory guidance, then derive task-specific success criteria "
            f"from the raw request, selected specialist route `{route}`, attached context, "
            "safety policy, and available tools."
        ),
        (
            "Identify the concrete target or entity, the operator's exact question, the "
            "expected output shape, the evidence depth needed for a useful answer, and what "
            "would make the response incomplete."
        ),
        (
            "Choose tools from the task shape and evidence needs; if context, source basis, "
            "or tool access is missing, return a precise blocker instead of substituting a "
            "generic result."
        ),
        (
            "Use Orchestrator, Slack, Gmail, source-bundle, and WorkItem context as bounded "
            "inputs; verify or deepen with retrieval when the request requires current, "
            "recent, source-backed, or decision-ready output."
        ),
        (
            "Before finalizing, make sure the response resolves the request, separates "
            "known facts from unknowns, cites evidence when applicable, respects no-send/"
            "no-write gates, and states downstream readiness or the next blocker."
        ),
    ]
    if _SLACK_PLACEHOLDER_TARGET_RE.search(text):
        checklist.append(
            "If the target is contextual, resolve it from the attached Slack/context pack before researching; do not research Slack itself unless Slack is the explicit target."
        )
    if re.search(r"\b(?:2026|current|recent|latest|doing|activity|update|roadmap)\b", normalized):
        checklist.append(
            "For temporal or current-activity wording, reason about freshness and whether independent or recent sources are needed before treating the answer as complete."
        )
    if re.search(
        r"\b(?:before|for|to)\s+(?:opportunity scout|outreach composer|gmail triage|business research analyst|chief of staff)\b",
        normalized,
    ):
        checklist.append(
            "Treat downstream agent mentions as readiness criteria unless the operator explicitly asked to run that downstream agent."
        )
    return checklist


def _specialist_orchestrator_context_text(
    request: WorkflowRunRequest,
    work_item: WorkItem,
) -> str:
    payload = _specialist_orchestrator_context_payload(request, work_item)
    return (
        "Orchestrator memo for this specialist WorkItem run:\n"
        + json.dumps(payload, ensure_ascii=True, sort_keys=True)
    )


def _finalize_manager_loop_result(
    result: WorkflowRunResult,
    *,
    original_request: WorkflowRunRequest,
    stop_reason: str,
    loop_steps: list[dict[str, Any]],
    store: SQLiteStore | None,
    feedback_callback: Callable[[str, dict[str, Any]], None] | None,
) -> WorkflowRunResult:
    note = f"Manager loop {stop_reason}."
    plan_summary = _orchestrator_plan_summary_for_manager_loop(
        original_request=original_request,
        result=result,
        loop_steps=loop_steps,
        stop_reason=stop_reason,
    )
    missing_stage_blockers = _manager_loop_missing_stage_blockers(
        original_request=original_request,
        result=result,
        loop_steps=loop_steps,
    )
    review_blockers = [] if plan_summary else _manager_loop_review_completion_blockers(result)
    advisory_limitations: list[WorkItemBlocker] = []
    if plan_summary:
        missing_stage_blockers, advisory_limitations = _split_manager_loop_plan_blockers(
            missing_stage_blockers
        )
    all_blockers = [*missing_stage_blockers, *review_blockers]
    work_item = result.work_item.model_copy(
        update={"audit_notes": list(dict.fromkeys([*result.work_item.audit_notes, note]))}
    ).touch()
    plan_artifact: WorkItemArtifactRef | None = None
    if plan_summary:
        target_metadata = dict(work_item.target.metadata)
        target_metadata["orchestrator_plan_summary"] = {
            **plan_summary,
            "advisory_limitations": [
                {"code": blocker.code, "message": blocker.message}
                for blocker in advisory_limitations
            ],
        }
        plan_artifact = WorkItemArtifactRef(
            artifact_type="orchestrator_plan_summary",
            artifact_id=f"{work_item.id}:orchestrator_plan_summary",
            source_agent=WorkItemRoute.ORCHESTRATOR.value,
            approval_state="pending",
            title="Orchestrator workflow plan",
            summary=str(plan_summary.get("summary") or "")[:240],
            metadata=target_metadata["orchestrator_plan_summary"],
        )
        work_item = work_item.model_copy(
            update={
                "target": work_item.target.model_copy(update={"metadata": target_metadata}),
                "next_action": None,
            }
        )
        work_item = attach_artifact(work_item, plan_artifact)
    for blocker in all_blockers:
        work_item = add_blocker(work_item, blocker)
    if review_blockers and (
        result.next_action is None or result.next_action.agent in {None, result.route}
    ):
        work_item = set_next_action(
            work_item,
            WorkItemNextAction(
                action="repair_or_deepen_specialist_output",
                agent=result.route,
                description="Repair the latest specialist output using Orchestrator review gaps.",
            ),
        )
    work_item = _normalize_review_failed_next_action(work_item, result.route)
    if all_blockers:
        work_item = work_item.model_copy(update={"status": derive_case_status(work_item)}).touch()
    elif plan_summary:
        work_item = work_item.model_copy(update={"status": derive_case_status(work_item)}).touch()
    elif (
        result.advanced
        and work_item.artifact_refs
        and not any(not blocker.resolved for blocker in work_item.blockers)
    ):
        # A one-step specialist result can be complete even when it carries an
        # optional next action such as "review opportunities". Keep the action
        # as guidance, but do not leave Slack-facing runs stuck as in-progress.
        work_item = work_item.model_copy(update={"status": WorkItemStatus.DONE}).touch()
    if plan_artifact is not None:
        _persist_artifact_and_event(
            work_item,
            plan_artifact,
            summary="Attached Orchestrator workflow plan summary.",
            store=store,
        )
    record_event(
        work_item,
        event_type="manager_loop_completed",
        summary=note,
        metadata={
            "stop_reason": stop_reason,
            "steps": loop_steps,
            "missing_required_stages": [
                {"code": blocker.code, "message": blocker.message}
                for blocker in all_blockers
            ],
            "advisory_limitations": [
                {"code": blocker.code, "message": blocker.message}
                for blocker in advisory_limitations
            ],
            "send_enabled": False,
            "schema_policy": "reused WorkflowRunResult, WorkItem events, and specialist schemas",
        },
        store=store,
    )
    human_summary = (
        _render_orchestrator_plan_summary(plan_summary) if plan_summary else result.human_summary
    )
    result_route = WorkItemRoute.ORCHESTRATOR if plan_summary else result.route
    artifact_refs = (
        [plan_artifact] if plan_artifact is not None else []
    ) or result.artifact_refs
    if feedback_callback is not None:
        feedback_callback(
            "manager_loop_completed",
            {
                "stop_reason": stop_reason,
                "steps": loop_steps,
                "missing_required_stages": [
                    {"code": blocker.code, "message": blocker.message}
                    for blocker in all_blockers
                ],
                "status": work_item.status.value,
                "route": result_route.value,
            },
        )
    if store is not None:
        store.save_work_item(work_item)
    return result.model_copy(
        update={
            "work_item": work_item,
            "status": work_item.status,
            "blockers": [*result.blockers, *all_blockers],
            "audit_notes": list(dict.fromkeys([*result.audit_notes, note])),
            "route": result_route,
            "artifact_refs": artifact_refs,
            "human_summary": human_summary,
            "next_action": work_item.next_action,
        }
    )


def _split_manager_loop_plan_blockers(
    blockers: list[WorkItemBlocker],
) -> tuple[list[WorkItemBlocker], list[WorkItemBlocker]]:
    """Keep safety/write boundaries hard; report missing stages as limitations."""

    hard_codes = {
        "manager_loop_send_blocked",
        "manager_loop_crm_write_blocked",
    }
    hard: list[WorkItemBlocker] = []
    advisory: list[WorkItemBlocker] = []
    for blocker in blockers:
        if blocker.code in hard_codes:
            hard.append(blocker)
            continue
        advisory.append(blocker.model_copy(update={"severity": "advisory"}))
    return hard, advisory


def _normalize_review_failed_next_action(
    work_item: WorkItem,
    route: WorkItemRoute,
) -> WorkItem:
    if not any(blocker.code == "manager_loop_review_failed" for blocker in work_item.blockers):
        return work_item
    if work_item.next_action is not None and work_item.next_action.action in {
        "repair_or_deepen_specialist_output",
        "review_failed_research_limitations",
    }:
        return work_item
    return set_next_action(
        work_item,
        WorkItemNextAction(
            action="review_failed_research_limitations",
            agent=route,
            description=(
                "Review the failed research limitations. Retry only with a targeted "
                "independent-source plan, or treat the artifact as not decision-ready."
            ),
        ),
    )


def _orchestrator_plan_summary_for_manager_loop(
    *,
    original_request: WorkflowRunRequest,
    result: WorkflowRunResult,
    loop_steps: list[dict[str, Any]],
    stop_reason: str,
) -> dict[str, Any] | None:
    if not _manager_loop_needs_orchestrator_plan_summary(original_request, loop_steps):
        return None
    if result.blockers or result.status in _MANAGER_LOOP_STOP_STATUSES:
        non_review_blockers = [
            blocker
            for blocker in result.blockers
            if blocker.code != "manager_loop_review_failed"
        ]
        only_review_blockers = bool(result.blockers) and not non_review_blockers
        if non_review_blockers or (
            result.status in _MANAGER_LOOP_STOP_STATUSES and not only_review_blockers
        ):
            return None
    routes = [str(step.get("route") or "") for step in loop_steps if step.get("route")]
    workflow = list(dict.fromkeys(routes))
    artifacts = [
        {
            "artifact_type": ref.artifact_type,
            "title": ref.title,
            "summary": ref.summary,
            "source_agent": ref.source_agent,
        }
        for ref in result.work_item.artifact_refs[:8]
    ]
    reviews = result.work_item.target.metadata.get("orchestrator_reviews", [])
    review_gaps: list[str] = []
    if isinstance(reviews, list):
        for review in reviews[-3:]:
            if not isinstance(review, dict):
                continue
            for gap in review.get("observed_gaps") or []:
                text = str(gap or "").strip()
                if text and text not in review_gaps:
                    review_gaps.append(text)
    assumptions = [
        "Used Keystone's default business-development context because no narrower target was supplied.",
        "Kept all actions read-only and draft-only; no send, save, CRM, Gmail, or Slack write was performed.",
    ]
    if any(step.get("route") == WorkItemRoute.OPPORTUNITY_SCOUT.value for step in loop_steps):
        assumptions.append("Started with opportunity scouting to identify plausible directions.")
    if any(step.get("route") == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value for step in loop_steps):
        assumptions.append("Used business research to add source-backed company context.")
    top_findings = [
        f"{item['artifact_type']}: {item['title'] or item['summary'] or 'artifact attached'}"
        for item in artifacts[:5]
    ] or ["No source-backed artifact was produced."]
    additional_input = [
        "Preferred target segment, geography, role type, company list, or time window.",
        "Whether the next run should prioritize research depth, opportunity volume, or outreach readiness.",
    ]
    if review_gaps:
        additional_input.append("Repair/deepen review gaps: " + "; ".join(review_gaps[:3]))
    next_action = (
        "Review the attached artifacts, choose one direction, then rerun the selected specialist "
        "with explicit constraints before drafting outreach."
    )
    if result.next_action is not None and result.next_action.description:
        next_action = result.next_action.description
    return {
        "summary": "Orchestrator selected and reviewed a bounded multi-agent workflow.",
        "assumptions_made": assumptions,
        "selected_workflow": workflow,
        "agents_used_or_proposed": workflow,
        "top_findings": top_findings,
        "recommended_next_action": next_action,
        "additional_input_would_improve": additional_input,
        "stop_reason": stop_reason,
        "review_gaps": review_gaps[:6],
        "send_enabled": False,
    }


def _manager_loop_needs_orchestrator_plan_summary(
    original_request: WorkflowRunRequest,
    loop_steps: list[dict[str, Any]],
) -> bool:
    manual = _manual_plan_event_payload(original_request.manual_request_plan)
    if manual.get("requested_agent") not in {None, "", WorkItemRoute.ORCHESTRATOR.value}:
        return False
    text = str(original_request.request_text or "").lower()
    markers = (
        "assumptions made",
        "selected workflow",
        "which agents",
        "agents were used",
        "agents used",
        "some combination",
        "decide whether this needs",
        "plan the safest workflow",
        "decide the workflow",
        "what should the agents do",
        "what additional input",
        "recommended next action",
    )
    if any(marker in text for marker in markers):
        return True
    routes = {str(step.get("route") or "") for step in loop_steps}
    return len(routes) > 1 and "orchestrator" in text


def _render_orchestrator_plan_summary(summary: dict[str, Any]) -> str:
    def lines_for(key: str) -> list[str]:
        values = summary.get(key) or []
        if not isinstance(values, list):
            values = [values]
        return [f"- {value}" for value in values if str(value or "").strip()]

    sections = [
        "Orchestrator workflow plan",
        "",
        "Assumptions made:",
        *lines_for("assumptions_made"),
        "",
        "Selected workflow:",
        "- " + " -> ".join(summary.get("selected_workflow") or ["orchestrator"]),
        "",
        "Agents used or proposed:",
        *lines_for("agents_used_or_proposed"),
        "",
        "Top findings:",
        *lines_for("top_findings"),
        "",
        "Recommended next action:",
        f"- {summary.get('recommended_next_action')}",
        "",
        "Additional input that would improve the next run:",
        *lines_for("additional_input_would_improve"),
    ]
    return "\n".join(str(line) for line in sections if line is not None).strip()


def _manager_loop_review_completion_blockers(result: WorkflowRunResult) -> list[WorkItemBlocker]:
    if result.blockers or result.status in {
        WorkItemStatus.BLOCKED,
        WorkItemStatus.NEEDS_APPROVAL,
        WorkItemStatus.NEEDS_CONTEXT,
        WorkItemStatus.ARCHIVED,
    }:
        return []
    reviews = result.work_item.target.metadata.get("orchestrator_reviews", [])
    if not isinstance(reviews, list) or not reviews:
        return []
    latest = reviews[-1]
    if not isinstance(latest, dict) or latest.get("review_status") != "fail":
        return []
    if (
        latest.get("blocking") is False
        or latest.get("advisory") is True
        or str(latest.get("review_decision") or "") == "warn"
    ):
        return []
    if not _manager_review_failure_is_authoritative(latest, result):
        return []
    return [
        WorkItemBlocker(
            code="manager_loop_review_failed",
            message=(
                "Orchestrator review marked the latest specialist step as failed; "
                "repair or deepen the run before treating the artifact as usable."
            ),
        )
    ]


def _manager_review_failure_is_authoritative(
    review_context: dict[str, Any],
    result: WorkflowRunResult,
) -> bool:
    if review_context.get("approval_boundary_ok") is False:
        return True
    if not result.work_item.artifact_refs and not result.artifact_refs:
        return True
    gaps = " ".join(str(gap or "").lower() for gap in review_context.get("observed_gaps") or [])
    critical_markers = (
        "term overlap is low",
        "unrelated",
        "wrong lane",
        "wrong response",
        "did not answer",
        "does not answer",
        "stale",
        "unsafe",
        "send enabled",
        "approval boundary",
        "unsupported claim",
        "missing approved",
    )
    return any(marker in gaps for marker in critical_markers)


def _manager_loop_business_research_depth_gap(
    original_request: WorkflowRunRequest,
    result: WorkflowRunResult,
) -> str:
    if result.route != WorkItemRoute.BUSINESS_RESEARCH_ANALYST:
        return ""
    request_text = latest_user_request(original_request.request_text).lower()
    if not re.search(r"\b(?:2026|current|recent|latest|doing|activity|update|roadmap)\b", request_text):
        return ""
    source_refs: list[dict[str, Any]] = []
    for artifact in [*result.artifact_refs, *result.work_item.artifact_refs]:
        if artifact.artifact_type != "company_profile":
            continue
        refs = artifact.metadata.get("source_refs")
        if isinstance(refs, list):
            source_refs.extend(item for item in refs if isinstance(item, dict))
    if not source_refs:
        return ""
    independent_refs = [
        ref
        for ref in source_refs
        if str(
            (ref.get("source_quality") or {}).get("source_type")
            if isinstance(ref.get("source_quality"), dict)
            else ref.get("source_type")
            or ""
        ).lower()
        not in {"company_site", "official_company_site", "fixture"}
    ]
    if independent_refs:
        return ""
    return (
        "Limited independent evidence for current/2026 business activity: the result is "
        "usable with caveats, but deepen retrieval beyond company-controlled pages for "
        "funding, partnerships, product updates, hiring, roadmap, and independent "
        "validation before treating it as decision-ready."
    )


def _manager_loop_missing_stage_blockers(
    *,
    original_request: WorkflowRunRequest,
    result: WorkflowRunResult,
    loop_steps: list[dict[str, Any]],
) -> list[WorkItemBlocker]:
    text = " ".join(str(original_request.request_text or "").lower().split())
    if not text:
        return []
    routes = {str(step.get("route") or "") for step in loop_steps}
    artifact_types = {artifact.artifact_type for artifact in result.work_item.artifact_refs}
    blockers: list[WorkItemBlocker] = []
    if (
        ("gmail context" in text or "check recent gmail" in text or "email context" in text)
        and WorkItemRoute.GMAIL_TRIAGE.value not in routes
    ):
        blockers.append(
            WorkItemBlocker(
                code="manager_loop_gmail_context_not_checked",
                message=(
                    "The request asked for a Gmail/email context check, but this manager "
                    "loop did not run Gmail Triage. Provide selected Gmail context or run "
                    "the Gmail step explicitly before relying on email-thread facts."
                ),
            )
        )
    if (
        _manager_loop_requests_research(text)
        and WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value not in routes
        and "company_profile" not in artifact_types
        and "source_summary" not in artifact_types
    ):
        blockers.append(
            WorkItemBlocker(
                code="manager_loop_research_not_completed",
                message=(
                    "The request asked for company/source research, but the manager "
                    "loop did not run Business Research Analyst before stopping."
                ),
            )
        )
    if (
        _manager_loop_requests_opportunity_record(text)
        and WorkItemRoute.OPPORTUNITY_SCOUT.value not in routes
        and "opportunity_record" not in artifact_types
    ):
        blockers.append(
            WorkItemBlocker(
                code="manager_loop_opportunity_not_created",
                message=(
                    "The request asked for an opportunity record or pipeline step, but "
                    "the manager loop did not run Opportunity Scout before stopping."
                ),
            )
        )
    gmail_only_draft_request = (
        WorkItemRoute.GMAIL_TRIAGE.value in routes
        and not _manager_loop_requests_research(text)
        and not _manager_loop_requests_opportunity_record(text)
        and not re.search(r"\b(?:outreach|linkedin)\b", text, flags=re.I)
    )
    if (
        _manager_loop_requests_outreach_draft(text)
        and not gmail_only_draft_request
        and WorkItemRoute.OUTREACH_COMPOSER.value not in routes
        and "outreach_draft" not in artifact_types
    ):
        blockers.append(
            WorkItemBlocker(
                code="manager_loop_outreach_not_drafted",
                message=(
                    "The request asked for draft outreach, but the manager loop did not "
                    "run Outreach Composer. Approve source-backed company/opportunity "
                    "context before drafting."
                ),
            )
        )
    if looks_like_send_side_effect(text):
        blockers.append(
            WorkItemBlocker(
                code="manager_loop_send_blocked",
                message=(
                    "The request included an external send action. Sending remains "
                    "blocked; the manager loop may only prepare draft-only outputs "
                    "after required context and human approval gates are satisfied."
                ),
            )
        )
    if _manager_loop_requests_crm_write(text):
        blockers.append(
            WorkItemBlocker(
                code="manager_loop_crm_write_blocked",
                message=(
                    "The request asked to save or write CRM records. No CRM write was "
                    "performed; only draft-only CRM-ready fields may be prepared after "
                    "source-backed context and explicit approval are available."
                ),
            )
        )
    if (
        ("score the workflow" in text or "scorecard" in text or "score the" in text)
        and "chief_of_staff_plan" not in artifact_types
    ):
        blockers.append(
            WorkItemBlocker(
                code="manager_loop_scorecard_not_generated",
                message=(
                    "The request asked for a workflow scorecard, but the manager loop "
                    "did not generate a scorecard artifact before stopping."
                ),
            )
        )
    return blockers


def _manager_loop_requests_research(normalized_text: str) -> bool:
    context_reference = re.sub(
        r"\b(?:same|attached|provided|current|existing)\s+company\s+research\s+brief\b",
        " ",
        normalized_text,
        flags=re.I,
    )
    return bool(
        re.search(
            r"\b(?:research\s+(?:the\s+)?company|research\s+it|company\s+research|"
            r"company\s+brief|company\s+profile|source-backed|source\s+backed)\b",
            context_reference,
            flags=re.I,
        )
    )


def _manager_loop_requests_opportunity_record(normalized_text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:create|save|add|prepare|record|pipeline)\b[^.\n]{0,160}"
            r"\b(?:opportunit|crm|record|pipeline)\b",
            normalized_text,
            flags=re.I,
        )
    ) or bool(re.search(r"\bopportunity record\b", normalized_text, flags=re.I))


def _manager_loop_requests_outreach_draft(normalized_text: str) -> bool:
    return bool(re.search(r"\boutreach draft\b(?!ing)", normalized_text, flags=re.I)) or bool(
        re.search(
            r"\b(?:draft|write|compose|prepare|send)\b[^.\n]{0,160}"
            r"\b(?:outreach|email|linkedin|message|note)\b",
            normalized_text,
            flags=re.I,
        )
    ) or (
        (
            _manager_loop_requests_research(normalized_text)
            or _manager_loop_requests_opportunity_record(normalized_text)
        )
        and bool(
            re.search(
                r"\b(?:draft|write|compose|prepare)\b[^.\n]{0,160}\b(?:response|reply)\b",
                normalized_text,
                flags=re.I,
            )
        )
    )


def _manager_loop_requests_crm_write(normalized_text: str) -> bool:
    if re.search(
        r"\b(?:do\s+not|don't|without|no)\b[^.\n]{0,120}"
        r"\b(?:crm|airtable|salesforce|hubspot|external\s+systems?)\b",
        normalized_text,
        flags=re.I,
    ):
        return False
    return bool(
        re.search(
            r"\b(?:save|write|add|sync|push|update|create|log)\b[^.\n]{0,160}"
            r"\b(?:crm|airtable|salesforce|hubspot)\b",
            normalized_text,
            flags=re.I,
        )
        or re.search(
            r"\b(?:crm|airtable|salesforce|hubspot)\b[^.\n]{0,160}"
            r"\b(?:save|write|add|sync|push|update|create|log)\b",
            normalized_text,
            flags=re.I,
        )
    )


def _select_route(
    request: WorkflowRunRequest,
    input_text: str,
    store: SQLiteStore | None,
) -> WorkItemRoute:
    if request.requested_route is not None:
        return request.requested_route
    existing: WorkItem | None = None
    if request.work_item_id and store is not None:
        existing = store.get_work_item(request.work_item_id)
    if existing is not None and input_text.lower() in {"", "continue", "resume"}:
        continued = infer_route_for_continue(existing)
        if continued is not None:
            return continued
        return existing.current_route
    planned_route = _route_from_manual_plan(request.manual_request_plan)
    if planned_route is not None:
        return planned_route
    if input_text and _looks_like_outreach_request(input_text):
        return WorkItemRoute.OUTREACH_COMPOSER
    decision = route_request(input_text)
    try:
        return WorkItemRoute(decision.route)
    except ValueError:
        return WorkItemRoute.CLARIFICATION


def _route_from_manual_plan(plan: dict | None) -> WorkItemRoute | None:
    if not isinstance(plan, dict):
        return None
    target_agent = str(plan.get("target_agent") or "").strip()
    if target_agent in {"", WorkItemRoute.ORCHESTRATOR.value, WorkItemRoute.CLARIFICATION.value}:
        return None
    try:
        return WorkItemRoute(target_agent)
    except ValueError:
        return None


def _sdk_session_spec_for_work_item(request: WorkflowRunRequest, work_item: WorkItem) -> Any:
    context_scope = context_file_session_components(request.context_file_path)
    if context_scope is not None:
        scope, components = context_scope
    else:
        scope = "workitem"
        components = (work_item.id,)
    return resolve_sdk_session_spec(
        scope=scope,
        components=components,
        enabled=request.sdk_session_enabled,
        explicit_session_id=request.sdk_session_id,
        database_path=request.sdk_session_db_path,
        default_enabled=request.live_sdk,
    )


def _apply_manual_request_plan(work_item: WorkItem, plan: dict | None) -> WorkItem:
    if not isinstance(plan, dict) or not plan:
        return work_item
    metadata = {
        **work_item.target.metadata,
        "manual_request_plan": _manual_plan_event_payload(plan),
    }
    for key in (
        "constraints",
        "desired_count",
        "task_objective",
        "expected_artifact_type",
        "required_entities",
        "required_terms",
        "gmail_query",
        "lookback_days",
        "draft_policy",
        "recipient",
        "outreach_channel",
        "tone",
    ):
        value = plan.get(key)
        if value not in (None, "", []):
            metadata[f"manual_{key}"] = value
    primary_target = str(plan.get("primary_target") or "").strip()
    target_type = str(plan.get("target_type") or "").strip()
    target = work_item.target
    if primary_target and not target.name:
        target = target.model_copy(update={"name": primary_target})
    if target_type and target_type != "unknown":
        target = target.model_copy(update={"object_type": target_type})
    return work_item.model_copy(
        update={"target": target.model_copy(update={"metadata": metadata})}
    ).touch()


def _manual_primary_target(work_item: WorkItem) -> str:
    plan = work_item.target.metadata.get("manual_request_plan")
    if not isinstance(plan, dict):
        return ""
    return " ".join(str(plan.get("primary_target") or "").strip().split())


_SLACK_PLACEHOLDER_TARGET_RE = re.compile(
    r"\b(?:company|target)\s+being\s+discussed\b"
    r"|\bselected\s+slack\s+(?:thread|context|post|message)\b"
    r"|\bprior\s+(?:post|message|slack\s+post|slack\s+message)\b"
    r"|\babove\s+(?:post|message)\b",
    flags=re.I,
)

_SLACK_CONTEXT_COMPANY_RE = re.compile(
    r"\b(?:company(?:\s+being\s+discussed)?|target\s+company|company\s+name)"
    r"\s*[:=-]\s*"
    r"(?P<name>[A-Z][A-Za-z0-9&.'-]*(?:\s+[A-Z][A-Za-z0-9&.'-]*){0,5})",
)


def _slack_context_resolved_target(work_item: WorkItem) -> str:
    slack_context = work_item.target.metadata.get("slack_context")
    if not isinstance(slack_context, dict):
        return ""
    texts: list[str] = []
    for key in ("read_context",):
        value = str(slack_context.get(key) or "").strip()
        if value:
            texts.append(value)
    selected = slack_context.get("selected_message")
    if isinstance(selected, dict):
        value = str(selected.get("text") or "").strip()
        if value:
            texts.append(value)
    messages = slack_context.get("thread_messages")
    if isinstance(messages, list):
        for item in messages:
            if isinstance(item, dict):
                value = str(item.get("text") or "").strip()
                if value:
                    texts.append(value)
    for text in texts:
        match = _SLACK_CONTEXT_COMPANY_RE.search(text)
        if not match:
            continue
        target = _clean_slack_context_target(match.group("name"))
        if target:
            return target
    return ""


def _clean_slack_context_target(value: str) -> str:
    cleaned = re.split(r"[\n\r;|]|(?:\s+-\s+)", str(value or ""), maxsplit=1)[0]
    cleaned = cleaned.strip(" .,:;\"'`*_")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return ""
    if cleaned.lower() in {"company", "target", "slack", "selected", "prior", "post"}:
        return ""
    return cleaned[:120]


def _target_needs_slack_context_resolution(target: str, request_text: str) -> bool:
    combined = f"{target}\n{request_text}"
    return bool(_SLACK_PLACEHOLDER_TARGET_RE.search(combined))


def _maybe_synthesize_user_facing_response(
    result: WorkflowRunResult,
    *,
    request: WorkflowRunRequest,
    sdk_session: Any | None,
    store: SQLiteStore | None = None,
) -> WorkflowRunResult:
    if not request.live_sdk:
        return result
    if _result_has_canonical_outreach_draft(result):
        return result.model_copy(
            update={
                "audit_notes": list(
                    dict.fromkeys(
                        [
                            *result.audit_notes,
                            (
                                "Skipped generic user-facing response synthesis because the "
                                "Outreach Composer draft artifact is canonical."
                            ),
                        ]
                    )
                )
            }
        )
    try:
        sdk_result = synthesize_user_facing_work_item_response_sdk_result(
            result,
            user_request=request.request_text or result.work_item.request_text,
            live=True,
            session=sdk_session,
        )
        synthesis = sdk_result.output
    except Exception as exc:
        note = f"User-facing response synthesis failed: {type(exc).__name__}: {exc}"
        return result.model_copy(
            update={"audit_notes": list(dict.fromkeys([*result.audit_notes, note]))}
        )
    if store is not None:
        _record_workflow_sdk_cost_event(
            result.work_item,
            event_type="workflow_sdk_usage",
            summary="Recorded user-facing response synthesis SDK usage.",
            agent_name="user_response_synthesizer",
            usage=sdk_result.usage,
            cost=sdk_result.cost,
            request_cache=sdk_result.request_cache,
            store=store,
        )
    text = format_user_response_synthesis(synthesis)
    if not text:
        return result
    return result.model_copy(
        update={
            "human_summary": text,
            "audit_notes": list(
                dict.fromkeys(
                    [
                        *result.audit_notes,
                        "Live user-facing response synthesis executed.",
                    ]
                )
            ),
        }
    )


def _result_has_canonical_outreach_draft(result: WorkflowRunResult) -> bool:
    if result.route != WorkItemRoute.OUTREACH_COMPOSER:
        return False
    return any(
        artifact.artifact_type == "outreach_draft"
        and bool(artifact.metadata.get("canonical_draft_copy", True))
        for artifact in result.artifact_refs
    )


def _record_workflow_sdk_cost_event(
    work_item: WorkItem,
    *,
    event_type: str,
    summary: str,
    agent_name: str,
    usage: dict[str, Any] | None,
    cost: dict[str, Any] | None,
    request_cache: dict[str, Any] | None,
    store: SQLiteStore,
    run_stage: str = "",
) -> None:
    usage_payload = dict(usage or {})
    cost_payload = dict(cost or {})
    cache_payload = dict(request_cache or {})
    record_event(
        work_item,
        event_type=event_type,
        actor=agent_name,
        summary=summary,
        metadata={
            "schema": "keystone.workflow_sdk_usage.v1",
            "agent_name": agent_name,
            "run_stage": run_stage,
            "usage": {
                key: usage_payload.get(key)
                for key in (
                    "requests",
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                    "total_tokens",
                    "cache_hit_rate",
                    "prompt_cache_key_present",
                    "prompt_cache_key_hash",
                )
                if usage_payload.get(key) is not None
            },
            "cost": {
                key: cost_payload.get(key)
                for key in (
                    "estimated_usd",
                    "amount_usd",
                    "source",
                    "pricing_provider",
                    "pricing_model",
                    "pricing_as_of",
                    "input_usd",
                    "cached_input_usd",
                    "output_usd",
                )
                if cost_payload.get(key) is not None
            },
            "request_cache": {
                "static_prefix_sha256": cache_payload.get("static_prefix_sha256"),
                "instructions_sha256": cache_payload.get("instructions_sha256"),
                "tool_names_sha256": cache_payload.get("tool_names_sha256"),
                "output_schema_sha256": cache_payload.get("output_schema_sha256"),
                "dynamic_prompt_sha256": cache_payload.get("dynamic_prompt_sha256"),
                "dynamic_prompt_chars": cache_payload.get("dynamic_prompt_chars"),
                "session_attached": cache_payload.get("session_attached"),
                "session_id_hash": cache_payload.get("session_id_hash"),
                "session_scope": cache_payload.get("session_scope"),
                "session_source": cache_payload.get("session_source"),
            },
        },
        store=store,
    )


def _record_preflight_sdk_cost_events(
    work_item: WorkItem,
    *,
    request: WorkflowRunRequest,
    store: SQLiteStore | None,
) -> None:
    if store is None or not isinstance(request.orchestrator_preflight, dict):
        return
    events = request.orchestrator_preflight.get("sdk_usage_events")
    if not isinstance(events, list):
        return
    existing_keys = _existing_workflow_sdk_usage_keys(work_item, store=store)
    for event in events:
        if not isinstance(event, dict):
            continue
        agent_name = str(event.get("agent_name") or "orchestrator_preflight")
        run_stage = str(event.get("run_stage") or "orchestrator_preflight")
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        cost = event.get("cost") if isinstance(event.get("cost"), dict) else {}
        request_cache = (
            event.get("request_cache") if isinstance(event.get("request_cache"), dict) else {}
        )
        event_key = _workflow_sdk_usage_event_key(
            agent_name=agent_name,
            run_stage=run_stage,
            usage=usage,
            request_cache=request_cache,
        )
        if event_key in existing_keys:
            continue
        _record_workflow_sdk_cost_event(
            work_item,
            event_type="workflow_sdk_usage",
            summary=f"Recorded {run_stage} SDK usage.",
            agent_name=agent_name,
            usage=usage,
            cost=cost,
            request_cache=request_cache,
            store=store,
            run_stage=run_stage,
        )
        existing_keys.add(event_key)


def _existing_workflow_sdk_usage_keys(
    work_item: WorkItem,
    *,
    store: SQLiteStore,
) -> set[tuple[str, str, str, str, str]]:
    keys: set[tuple[str, str, str, str, str]] = set()
    for event in store.list_work_item_events(work_item.id):
        if event.event_type != "workflow_sdk_usage":
            continue
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
        request_cache = (
            metadata.get("request_cache")
            if isinstance(metadata.get("request_cache"), dict)
            else {}
        )
        keys.add(
            _workflow_sdk_usage_event_key(
                agent_name=str(metadata.get("agent_name") or event.actor or ""),
                run_stage=str(metadata.get("run_stage") or ""),
                usage=usage,
                request_cache=request_cache,
            )
        )
    return keys


def _workflow_sdk_usage_event_key(
    *,
    agent_name: str,
    run_stage: str,
    usage: dict[str, Any],
    request_cache: dict[str, Any],
) -> tuple[str, str, str, str, str]:
    return (
        agent_name,
        run_stage,
        str(usage.get("prompt_cache_key_hash") or ""),
        str(request_cache.get("static_prefix_sha256") or ""),
        str(request_cache.get("dynamic_prompt_sha256") or ""),
    )


def _record_retrieval_cost_event(
    work_item: WorkItem,
    *,
    metadata: dict[str, object],
    cost_profile: str,
    hosted_web_search_max_calls: int | None,
    store: SQLiteStore,
    agent_name: str = WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
) -> None:
    provider_usage = metadata.get("provider_usage")
    usage_payload = provider_usage if isinstance(provider_usage, dict) else {}
    aggregate = _aggregate_provider_usage(usage_payload)
    record_event(
        work_item,
        event_type="workflow_retrieval_usage",
        actor=agent_name,
        summary="Recorded live retrieval usage for WorkItem cost analysis.",
        metadata={
            "schema": "keystone.workflow_retrieval_usage.v1",
            "agent_name": agent_name,
            "cost_profile": cost_profile,
            "provider_summary": metadata.get("search_provider")
            or metadata.get("provider_summary")
            or "",
            "query_count": len(metadata.get("search_queries") or []),
            "raw_search_result_count": metadata.get("raw_search_result_count", 0),
            "hosted_web_search_max_calls": hosted_web_search_max_calls,
            "provider_usage": usage_payload,
            "aggregate_usage": aggregate,
            "search_quality": metadata.get("search_quality") or {},
        },
        store=store,
    )


def _aggregate_provider_usage(provider_usage: dict[Any, Any]) -> dict[str, Any]:
    aggregate = {
        "requests_attempted": 0,
        "requests_succeeded": 0,
        "raw_result_count": 0,
        "credits_used": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "estimated_usd": 0.0,
    }
    for usage in provider_usage.values():
        if not isinstance(usage, dict):
            continue
        for key in (
            "requests_attempted",
            "requests_succeeded",
            "raw_result_count",
            "credits_used",
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        ):
            aggregate[key] += int(float(usage.get(key) or 0))
        aggregate["estimated_usd"] = round(
            float(aggregate["estimated_usd"]) + float(usage.get("estimated_usd") or 0.0),
            8,
        )
    input_tokens = int(aggregate["input_tokens"])
    cached_input_tokens = int(aggregate["cached_input_tokens"])
    aggregate["cache_hit_rate"] = (
        round(cached_input_tokens / input_tokens, 4) if input_tokens else None
    )
    return aggregate


def _manual_expected_artifact_type(work_item: WorkItem) -> str:
    value = work_item.target.metadata.get("manual_expected_artifact_type")
    if value:
        return str(value).strip()
    plan = work_item.target.metadata.get("manual_request_plan")
    if isinstance(plan, dict):
        return str(plan.get("expected_artifact_type") or "").strip()
    return ""


def _manual_task_objective(work_item: WorkItem) -> str:
    value = work_item.target.metadata.get("manual_task_objective")
    if value:
        return str(value).strip()
    plan = work_item.target.metadata.get("manual_request_plan")
    if isinstance(plan, dict):
        return str(plan.get("task_objective") or "").strip()
    return ""


def _manual_required_terms(work_item: WorkItem) -> list[str]:
    raw = work_item.target.metadata.get("manual_required_terms")
    if not raw:
        plan = work_item.target.metadata.get("manual_request_plan")
        raw = plan.get("required_terms") if isinstance(plan, dict) else []
    values = raw if isinstance(raw, list | tuple | set) else [raw]
    return [str(item).strip() for item in values if str(item or "").strip()]


def _manual_plan_event_payload(plan: dict | None) -> dict:
    if not isinstance(plan, dict):
        return {}
    allowed = {
        "source",
        "requested_agent",
        "target_agent",
        "intent",
        "primary_target",
        "target_type",
        "objective",
        "task_objective",
        "expected_artifact_type",
        "desired_count",
        "constraints",
        "required_entities",
        "required_terms",
        "requires_live_search",
        "requires_approved_context",
        "side_effect_policy",
        "planner_warnings",
        "gmail_query",
        "lookback_days",
        "draft_policy",
        "recipient",
        "outreach_channel",
        "tone",
    }
    return {key: plan[key] for key in allowed if key in plan and plan[key] not in (None, "")}


def _orchestrator_preflight_event_payload(preflight: dict | None) -> dict:
    if not isinstance(preflight, dict):
        return {}
    payload: dict[str, Any] = {}
    for key in ("request_text", "requested_agent", "advisory_only", "selected_agent"):
        value = preflight.get(key)
        if value not in (None, ""):
            payload[key] = value
    if "blocked_by_orchestrator" in preflight:
        payload["blocked_by_orchestrator"] = bool(preflight.get("blocked_by_orchestrator"))
    manual_plan = _manual_plan_event_payload(preflight.get("manual_request_plan"))
    if manual_plan:
        payload["manual_request_plan"] = manual_plan
    route_result = preflight.get("route_result")
    if isinstance(route_result, dict):
        payload["route_result"] = {
            key: route_result[key]
            for key in (
                "route",
                "routing_mode",
                "rationale",
                "refused",
                "send_enabled",
                "stop_reason",
                "clarification_request",
            )
            if key in route_result and route_result[key] not in (None, "")
        }
    return payload


_CONTEXT_SOURCE_TOOL_GUIDANCE: dict[str, dict[str, Any]] = {
    "slack": {
        "label": "Slack posts and threads",
        "patterns": (
            r"\bslack\b",
            r"\b(?:selected|prior|above)\s+(?:post|message|thread)\b",
            r"\bthread\s+context\b",
        ),
        "tools": ("search_slack_repo_context", "read_slack_repo_context_file"),
        "instruction": (
            "Use attached Slack context first. If it is not attached, stop with a "
            "missing-context blocker instead of guessing from an unrelated thread."
        ),
    },
    "airtable": {
        "label": "Airtable records",
        "patterns": (r"\bairtable\b", r"\b(?:crm|tracker)\s+(?:record|table|row|field)s?\b"),
        "tools": ("airtable_get_base_schema", "airtable_read_records"),
        "instruction": (
            "Inspect Airtable schema before reading records; writes require explicit "
            "approval and live-write flags."
        ),
    },
    "google_docs": {
        "label": "Google Docs",
        "patterns": (
            r"\bgoogle\s+workspace\b",
            r"\bgoogle\s+docs?\b",
            r"\bgdocs?\b",
            r"\bgoogle\s+document\b",
        ),
        "tools": ("google_doc_read", "google_doc_write"),
        "instruction": "Use Google Docs tools for requested internal documents; draft writes only.",
    },
    "google_drive": {
        "label": "Google Drive",
        "patterns": (
            r"\bgoogle\s+workspace\b",
            r"\bgoogle\s+drive\b",
            r"\bshared\s+drive\b",
            r"\bdrive\s+folder\b",
        ),
        "tools": ("google_drive_list_folder",),
        "instruction": "Use Drive search/read tools when the requested evidence lives in Drive.",
    },
    "google_sheets": {
        "label": "Google Sheets",
        "patterns": (
            r"\bgoogle\s+workspace\b",
            r"\bgoogle\s+sheets?\b",
            r"\bspreadsheet\b",
            r"\bsheets?\s+(?:row|tab|data|workbook)\b",
        ),
        "tools": ("google_sheet_list", "google_sheet_read_table", "google_sheet_update_row"),
        "instruction": "Use Sheets tools for requested tabular context; writes require approval.",
    },
    "gmail": {
        "label": "Gmail messages and threads",
        "patterns": (r"\bgmail\b", r"\bemail\s+thread\b", r"\binbox\b", r"\bmailbox\b"),
        "tools": ("get_gmail_message", "create_gmail_draft_reply"),
        "instruction": (
            "Use Gmail thread/message context when requested. Sending is never allowed; "
            "drafts remain approval-gated."
        ),
    },
    "work_items": {
        "label": "WorkItems and run state",
        "patterns": (
            r"\bwork\s*items?\b",
            r"\bworkflow\s+state\b",
            r"\b(?:continue|resume)\s+(?:the\s+)?(?:case|work|workitem)\b",
            r"\bdecision_trace\b",
            r"\baudit\s+notes?\b",
        ),
        "tools": ("work_item_state", "decision_trace", "audit_notes"),
        "instruction": "Use current WorkItem state, sources, artifacts, blockers, and audit events.",
    },
    "kni_documents": {
        "label": "KNI documents and local context",
        "patterns": (
            r"\bkni\s+(?:docs?|documents?|runbook|notes?)\b",
            r"\blocal\s+context\b",
            r"\bkeystone\s+(?:docs?|documents?|runbook)\b",
            r"\brepo\s+docs?\b",
        ),
        "tools": ("list_local_context_sources", "search_local_context", "read_local_context_file"),
        "instruction": "Search allowlisted local/KNI context before answering document-backed asks.",
    },
}


def _apply_requested_context_manifest(
    work_item: WorkItem,
    request: WorkflowRunRequest,
    *,
    external_context: dict[str, Any],
) -> WorkItem:
    manifest = _requested_context_source_manifest(work_item, request, external_context)
    if not manifest:
        return work_item
    metadata = {**work_item.target.metadata, "context_source_manifest": manifest}
    audit_notes = list(work_item.audit_notes)
    requested = [
        str(source.get("source"))
        for source in manifest.get("sources", [])
        if isinstance(source, dict) and source.get("requested")
    ]
    if requested:
        note = "Requested context sources tracked for specialist run: " + ", ".join(requested)
        if note not in audit_notes:
            audit_notes.append(note)
    return work_item.model_copy(
        update={
            "target": work_item.target.model_copy(update={"metadata": metadata}),
            "audit_notes": audit_notes,
        }
    ).touch()


def _requested_context_source_manifest(
    work_item: WorkItem,
    request: WorkflowRunRequest,
    external_context: dict[str, Any],
) -> dict[str, Any]:
    evidence_texts = _context_request_evidence_texts(request)
    requested_sources: dict[str, list[str]] = {}
    for source, spec in _CONTEXT_SOURCE_TOOL_GUIDANCE.items():
        reasons: list[str] = []
        for label, text in evidence_texts:
            if not text:
                continue
            if any(re.search(pattern, text, flags=re.I) for pattern in spec["patterns"]):
                reasons.append(label)
        if reasons:
            requested_sources[source] = list(dict.fromkeys(reasons))

    attached_sources = _attached_context_sources(work_item, external_context)
    if not requested_sources and attached_sources <= {"work_items"}:
        return {}
    all_sources = sorted(set(requested_sources) | attached_sources)
    if not all_sources:
        return {}

    entries: list[dict[str, Any]] = []
    for source in all_sources:
        spec = _CONTEXT_SOURCE_TOOL_GUIDANCE[source]
        included = source in attached_sources
        requested = source in requested_sources
        status = "included" if included else "requested_available_as_tool"
        if source == "work_items":
            included = True
            status = "included"
        entries.append(
            {
                "source": source,
                "label": spec["label"],
                "requested": requested,
                "included": included,
                "status": status,
                "requested_by": requested_sources.get(source, []),
                "tools": list(spec["tools"]),
                "specialist_instruction": spec["instruction"],
            }
        )

    return {
        "schema": "keystone.context_source_manifest.v1",
        "policy": (
            "If a requested source is not already included, the specialist must use the "
            "listed read/context tools or return a precise missing-context blocker."
        ),
        "sources": entries,
    }


def _context_request_evidence_texts(request: WorkflowRunRequest) -> list[tuple[str, str]]:
    evidence: list[tuple[str, str]] = [("user_request", request.request_text or "")]
    if isinstance(request.manual_request_plan, dict):
        evidence.append(("manual_request_plan", _bounded_json_text(request.manual_request_plan)))
    if isinstance(request.orchestrator_preflight, dict):
        preflight = request.orchestrator_preflight
        compact_preflight = _orchestrator_preflight_event_payload(preflight)
        evidence.append(("orchestrator_preflight", _bounded_json_text(compact_preflight)))
        route_result = preflight.get("route_result")
        if isinstance(route_result, dict):
            evidence.append(("orchestrator_route_result", _bounded_json_text(route_result)))
        memo = preflight.get("preflight_memo")
        if isinstance(memo, dict):
            evidence.append(("orchestrator_preflight_memo", _bounded_json_text(memo)))
    return evidence


def _bounded_json_text(value: Any, *, max_chars: int = 5000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=True, sort_keys=True)
    except (TypeError, ValueError):
        text = str(value or "")
    return text[:max_chars]


def _attached_context_sources(
    work_item: WorkItem,
    external_context: dict[str, Any],
) -> set[str]:
    sources: set[str] = {"work_items"}
    metadata = work_item.target.metadata
    if isinstance(metadata.get("slack_context"), dict) or str(
        external_context.get("schema") or ""
    ).startswith("keystone.slack."):
        sources.add("slack")
    if metadata.get("external_context") or external_context:
        for source in _CONTEXT_SOURCE_TOOL_GUIDANCE:
            if source in {"work_items", "slack"}:
                continue
            if _external_context_mentions_source(source, external_context):
                sources.add(source)
    for source_ref in work_item.sources:
        source_type = " ".join(
            part
            for part in (
                source_ref.source_type,
                source_ref.provider,
                source_ref.source_id,
                source_ref.title,
            )
            if part
        ).lower()
        if "slack" in source_type:
            sources.add("slack")
        if "airtable" in source_type:
            sources.add("airtable")
        if "gmail" in source_type or "email" in source_type:
            sources.add("gmail")
        if "google_doc" in source_type or "google doc" in source_type:
            sources.add("google_docs")
        if "google_drive" in source_type or "google drive" in source_type:
            sources.add("google_drive")
        if "google_sheet" in source_type or "google sheet" in source_type:
            sources.add("google_sheets")
        if "local_context" in source_type or "kni" in source_type:
            sources.add("kni_documents")
    return sources


def _external_context_mentions_source(source: str, external_context: dict[str, Any]) -> bool:
    if not external_context:
        return False
    text = _bounded_json_text(external_context, max_chars=10000)
    spec = _CONTEXT_SOURCE_TOOL_GUIDANCE[source]
    return any(re.search(pattern, text, flags=re.I) for pattern in spec["patterns"])


def _context_source_manifest_event_payload(work_item: WorkItem) -> dict[str, Any]:
    manifest = work_item.target.metadata.get("context_source_manifest")
    if not isinstance(manifest, dict):
        return {}
    entries = manifest.get("sources")
    if not isinstance(entries, list):
        return {}
    return {
        "schema": manifest.get("schema"),
        "sources": [
            {
                "source": entry.get("source"),
                "requested": bool(entry.get("requested")),
                "included": bool(entry.get("included")),
                "status": entry.get("status"),
                "requested_by": entry.get("requested_by") or [],
            }
            for entry in entries
            if isinstance(entry, dict)
        ],
    }


def _load_external_context(request: WorkflowRunRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if isinstance(request.external_context, dict):
        payload.update(request.external_context)
    if request.context_file_path:
        file_payload = json.loads(Path(request.context_file_path).read_text(encoding="utf-8"))
        if not isinstance(file_payload, dict):
            raise ValueError("context_file_path must contain a JSON object.")
        payload.update(file_payload)
    return payload


def _apply_external_context(
    work_item: WorkItem,
    context: dict[str, Any],
    *,
    context_file_path: str,
) -> WorkItem:
    if not context:
        return work_item
    if str(context.get("schema") or "") == "keystone.slack.selected_message_context.v1":
        return _apply_slack_selected_context(
            work_item,
            context,
            context_file_path=context_file_path,
        )
    if str(context.get("schema") or "") == "keystone.slack.history_context.v1":
        return _apply_slack_history_context(
            work_item,
            context,
            context_file_path=context_file_path,
        )
    metadata = {
        **work_item.target.metadata,
        "external_context": _bounded_context_metadata(context),
    }
    if context_file_path:
        metadata["external_context_file_path"] = context_file_path
    return work_item.model_copy(
        update={"target": work_item.target.model_copy(update={"metadata": metadata})}
    ).touch()


def _apply_slack_selected_context(
    work_item: WorkItem,
    context: dict[str, Any],
    *,
    context_file_path: str,
) -> WorkItem:
    selected = (
        context.get("selected_message") if isinstance(context.get("selected_message"), dict) else {}
    )
    selected_text = _compact_context_text(selected.get("text"), max_chars=900)
    channel_id = _compact_context_text(context.get("channel_id"), max_chars=80)
    selected_ts = _compact_context_text(context.get("selected_message_ts"), max_chars=80)
    permalink = _compact_context_text(context.get("permalink"), max_chars=500)
    source_id = f"slack:{channel_id}:{selected_ts}".strip(":")
    warnings = _context_string_list(context.get("warnings"), max_items=5, max_chars=320)
    slack_metadata = {
        "schema": str(context.get("schema") or ""),
        "source": str(context.get("source") or "slack_message_action"),
        "team_id": _compact_context_text(context.get("team_id"), max_chars=80),
        "team_domain": _compact_context_text(context.get("team_domain"), max_chars=120),
        "channel_id": channel_id,
        "channel_name": _compact_context_text(context.get("channel_name"), max_chars=120),
        "selected_message_ts": selected_ts,
        "thread_ts": _compact_context_text(context.get("thread_ts"), max_chars=80),
        "permalink": permalink,
        "thread_fetch_status": _compact_context_text(
            context.get("thread_fetch_status"),
            max_chars=40,
        ),
        "warnings": warnings,
        "selected_message": {
            "ts": _compact_context_text(selected.get("ts"), max_chars=80),
            "user_id": _compact_context_text(selected.get("user_id"), max_chars=80),
            "username": _compact_context_text(selected.get("username"), max_chars=120),
            "text": selected_text,
            "permalink": _compact_context_text(
                selected.get("permalink") or permalink,
                max_chars=500,
            ),
        },
        "thread_messages": _context_messages_metadata(context.get("thread_messages")),
    }
    if context_file_path:
        slack_metadata["context_file_path"] = context_file_path
    metadata = {**work_item.target.metadata, "slack_context": slack_metadata}
    sources = list(work_item.sources)
    if source_id and all(source.source_id != source_id for source in sources):
        sources.append(
            WorkItemSourceRef(
                title="Selected Slack message",
                url=permalink,
                source_type="slack_message",
                source_id=source_id,
                supported_claim="Selected Slack context supplied by the user for this run.",
                provider="slack",
                extraction_status=str(context.get("thread_fetch_status") or "not_requested"),
                retrieved_at=utc_now_iso(),
                key_facts=[selected_text] if selected_text else [],
            )
        )
    audit_notes = list(work_item.audit_notes)
    for warning in warnings:
        note = f"Slack context warning: {warning}"
        if note not in audit_notes:
            audit_notes.append(note)
    return work_item.model_copy(
        update={
            "target": work_item.target.model_copy(update={"metadata": metadata}),
            "sources": sources,
            "audit_notes": audit_notes,
        }
    ).touch()


def _attach_slack_prompt_context(work_item: WorkItem, *, latest_request: str) -> WorkItem:
    metadata = work_item.target.metadata
    slack_context = metadata.get("slack_context")
    if not isinstance(slack_context, dict):
        return work_item
    transcript = _slack_prompt_transcript(slack_context, latest_request=latest_request)
    if not transcript:
        return work_item
    updated_slack_context = {
        **slack_context,
        "thread_transcript": transcript,
        "latest_user_follow_up": _compact_context_text(latest_request, max_chars=1200),
        "prompt_context_layout": (
            "stable agent instructions/tools/schema first; dynamic Slack thread transcript "
            "rendered in deterministic append-only order; latest operator follow-up last"
        ),
    }
    return work_item.model_copy(
        update={
            "target": work_item.target.model_copy(
                update={
                    "metadata": {
                        **metadata,
                        "slack_context": updated_slack_context,
                    }
                }
            )
        }
    ).touch()


def _slack_prompt_transcript(slack_context: dict[str, Any], *, latest_request: str) -> str:
    messages = _slack_prompt_messages(slack_context)
    if not messages and not latest_request.strip():
        return ""
    lines = [
        "Slack thread context (deterministic append-only order):",
        f"Channel: {slack_context.get('channel_name') or slack_context.get('channel_id') or ''}",
        f"Thread TS: {slack_context.get('thread_ts') or slack_context.get('selected_message_ts') or ''}",
    ]
    for index, message in enumerate(messages, start=1):
        speaker = message.get("username") or message.get("user_id") or "unknown"
        text = _compact_context_text(message.get("text"), max_chars=900)
        if not text:
            continue
        lines.append(f"{index}. [{message.get('ts') or ''}] {speaker}: {text}")
    if latest_request.strip():
        lines.extend(
            [
                "",
                "Latest operator follow-up:",
                _compact_context_text(latest_request, max_chars=1200),
            ]
        )
    return "\n".join(lines).strip()


def _slack_prompt_messages(slack_context: dict[str, Any]) -> list[dict[str, str]]:
    raw_messages = slack_context.get("thread_messages")
    messages = [item for item in raw_messages if isinstance(item, dict)] if isinstance(raw_messages, list) else []
    selected = slack_context.get("selected_message")
    if isinstance(selected, dict) and selected.get("text"):
        selected_ts = str(selected.get("ts") or "")
        if not any(str(message.get("ts") or "") == selected_ts for message in messages):
            messages.append(selected)
    deduped: dict[str, dict[str, str]] = {}
    for index, message in enumerate(messages):
        key = str(message.get("ts") or f"index:{index}")
        if key not in deduped:
            deduped[key] = {
                "ts": _compact_context_text(message.get("ts"), max_chars=80),
                "user_id": _compact_context_text(message.get("user_id"), max_chars=80),
                "username": _compact_context_text(message.get("username"), max_chars=120),
                "text": _compact_context_text(message.get("text"), max_chars=900),
            }
    return sorted(deduped.values(), key=lambda item: (_slack_ts_sort_value(item["ts"]), item["ts"]))


def _slack_ts_sort_value(value: str) -> float:
    try:
        return float(str(value or "0"))
    except (TypeError, ValueError):
        return 0.0


def _apply_slack_history_context(
    work_item: WorkItem,
    context: dict[str, Any],
    *,
    context_file_path: str,
) -> WorkItem:
    read_context = _compact_context_text(context.get("read_context"), max_chars=4000)
    channel_id = _compact_context_text(context.get("channel_id"), max_chars=80)
    thread_ts = _compact_context_text(context.get("thread_ts"), max_chars=80)
    request_ts = _compact_context_text(context.get("request_ts"), max_chars=80)
    warnings = _context_string_list(context.get("warnings"), max_items=5, max_chars=320)
    context_window_days = _slack_history_context_window_days(context)
    if _context_int(context.get("context_window_days")) > 7:
        warnings.append(
            "Slack history context was capped to a maximum 7-day window for cost control."
        )
    slack_metadata = {
        "schema": str(context.get("schema") or ""),
        "source": str(context.get("source") or "slack_app_mention_history"),
        "channel_id": channel_id,
        "channel_name": _compact_context_text(context.get("channel_name"), max_chars=120),
        "thread_ts": thread_ts,
        "request_ts": request_ts,
        "thread_fetch_status": _compact_context_text(
            context.get("thread_fetch_status") or ("ok" if read_context else "missing"),
            max_chars=40,
        ),
        "read_context": read_context,
        "context_window_days": context_window_days,
        "channel_history_policy": (
            "thread-first; broad channel history is compacted and capped to 7 days or less"
        ),
        "warnings": warnings,
    }
    if context_file_path:
        slack_metadata["context_file_path"] = context_file_path
    metadata = {**work_item.target.metadata, "slack_context": slack_metadata}
    sources = list(work_item.sources)
    source_id = f"slack:{channel_id}:{thread_ts or request_ts}".strip(":")
    if read_context and source_id and all(source.source_id != source_id for source in sources):
        sources.append(
            WorkItemSourceRef(
                title="Slack thread history context",
                url="",
                source_type="slack_message",
                source_id=source_id,
                supported_claim="Slack thread history supplied by the Slack runtime for this run.",
                provider="slack",
                extraction_status=str(slack_metadata["thread_fetch_status"]),
                retrieved_at=utc_now_iso(),
                key_facts=[read_context],
            )
        )
    audit_notes = list(work_item.audit_notes)
    for warning in warnings:
        note = f"Slack context warning: {warning}"
        if note not in audit_notes:
            audit_notes.append(note)
    return work_item.model_copy(
        update={
            "target": work_item.target.model_copy(update={"metadata": metadata}),
            "sources": sources,
            "audit_notes": audit_notes,
        }
    ).touch()


def _external_context_event_payload(
    context: dict[str, Any],
    *,
    context_file_path: str,
) -> dict[str, Any]:
    if not context:
        return {}
    payload = {
        "schema": str(context.get("schema") or ""),
        "source": str(context.get("source") or ""),
    }
    if context_file_path:
        payload["context_file_path"] = context_file_path
    if str(context.get("schema") or "") == "keystone.slack.selected_message_context.v1":
        payload.update(
            {
                "channel_id": _compact_context_text(context.get("channel_id"), max_chars=80),
                "selected_message_ts": _compact_context_text(
                    context.get("selected_message_ts"),
                    max_chars=80,
                ),
                "thread_ts": _compact_context_text(context.get("thread_ts"), max_chars=80),
                "permalink": _compact_context_text(context.get("permalink"), max_chars=500),
                "thread_fetch_status": _compact_context_text(
                    context.get("thread_fetch_status"),
                    max_chars=40,
                ),
                "warnings": _context_string_list(
                    context.get("warnings"),
                    max_items=5,
                    max_chars=320,
                ),
            }
        )
    return {key: value for key, value in payload.items() if value not in ("", [], {})}


def _slack_history_context_window_days(context: dict[str, Any]) -> int:
    raw_days = _context_int(context.get("context_window_days"))
    if raw_days <= 0:
        raw_days = _context_int(context.get("lookback_days"))
    if raw_days <= 0:
        raw_days = 7
    return max(1, min(7, raw_days))


def _context_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _bounded_context_metadata(context: dict[str, Any]) -> dict[str, Any]:
    allowed = {}
    for key in ("schema", "source", "permalink", "warnings"):
        if key in context:
            allowed[key] = context[key]
    return allowed


def _context_messages_metadata(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    messages: list[dict[str, str]] = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        messages.append(
            {
                "ts": _compact_context_text(item.get("ts"), max_chars=80),
                "user_id": _compact_context_text(item.get("user_id"), max_chars=80),
                "username": _compact_context_text(item.get("username"), max_chars=120),
                "text": _compact_context_text(item.get("text"), max_chars=900),
                "permalink": _compact_context_text(item.get("permalink"), max_chars=500),
            }
        )
    return messages


def _context_string_list(value: Any, *, max_items: int, max_chars: int) -> list[str]:
    if value is None:
        return []
    raw_values = value if isinstance(value, list) else [value]
    return [
        text
        for text in (
            _compact_context_text(item, max_chars=max_chars) for item in raw_values[:max_items]
        )
        if text
    ]


def _compact_context_text(value: Any, *, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."


def _effective_max_results(request: WorkflowRunRequest) -> int:
    count = request.max_results
    plan = request.manual_request_plan if isinstance(request.manual_request_plan, dict) else {}
    desired = plan.get("desired_count")
    if isinstance(desired, int):
        count = max(count, desired)
    return max(1, min(20, count))


_WORK_ITEM_COMPARISON_RE = re.compile(
    r"\b(?:compare|comparison\s+of)\s+"
    r"(?P<company_a>(?:[A-Z][\w&.-]*|[A-Z]{2,})(?:\s+(?:[A-Z][\w&.-]*|[A-Z]{2,})){0,5})"
    r"\s+(?:and|vs\.?|versus)\s+"
    r"(?P<company_b>(?:[A-Z][\w&.-]*|[A-Z]{2,})(?:\s+(?:[A-Z][\w&.-]*|[A-Z]{2,})){0,5})\b",
    re.I,
)


def _comparison_company_names(text: str) -> tuple[str, str] | None:
    match = _WORK_ITEM_COMPARISON_RE.search(str(text or ""))
    if not match:
        return None
    company_a = _clean_comparison_company_name(match.group("company_a"))
    company_b = _clean_comparison_company_name(match.group("company_b"))
    if not company_a or not company_b:
        return None
    return company_a, company_b


def _clean_comparison_company_name(value: str) -> str:
    cleaned = " ".join(str(value or "").split()).strip(" .,:;-[]")
    cleaned = re.split(
        r"\b(?:as|for|with|using|about|relevant|possible|potential)\b",
        cleaned,
        maxsplit=1,
        flags=re.I,
    )[0].strip(" .,:;-[]")
    return cleaned[:120]


def _advance_gmail_triage(
    work_item: WorkItem,
    *,
    request: WorkflowRunRequest,
    store: SQLiteStore | None,
) -> WorkflowRunResult:
    gmail_plan = infer_gmail_execution_plan(request.request_text, source="work_item")
    inline_fixture = _inline_gmail_fixture_from_request(request.request_text)
    metadata = {
        **work_item.target.metadata,
        "gmail_execution_plan": gmail_plan.model_dump(mode="json"),
        "gmail_inline_context_available": inline_fixture is not None,
    }
    work_item = work_item.model_copy(
        update={
            "last_agent": WorkItemRoute.GMAIL_TRIAGE.value,
            "target": work_item.target.model_copy(update={"metadata": metadata}),
        }
    )
    if inline_fixture is not None and _gmail_plan_allows_inline_read_only_triage(gmail_plan):
        triage = triage_email_fixture(inline_fixture)
        limitations = [
            *triage.triage_limitations,
            (
                "Read-only triage used sanitized inline email text, not a selected Gmail "
                "thread or live Gmail message."
            ),
            "No Gmail draft, label, send, save, post, or external write was performed.",
        ]
        if triage.draft_reply:
            limitations.append(
                "Draft reply text is local guidance only; no Gmail draft was created."
            )
        triage = triage.model_copy(
            update={
                "draft_created": False,
                "recommended_action": _read_only_gmail_recommended_action(triage),
                "triage_limitations": list(dict.fromkeys(limitations)),
            }
        )
        output_payload = triage.model_dump(mode="json")
        artifact_id = ""
        if store is not None:
            artifact_id = str(
                store.save_agent_run(
                    agent_name=WorkItemRoute.GMAIL_TRIAGE.value,
                    input_payload={
                        "request": request.request_text,
                        "mode": "inline_read_only_triage",
                        "gmail_execution_plan": gmail_plan.model_dump(mode="json"),
                    },
                    input_summary=request.request_text[:240],
                    output=output_payload,
                    model="fixture",
                    dry_run=True,
                    status="success",
                )
            )
        artifact = WorkItemArtifactRef(
            artifact_type="gmail_triage_report",
            artifact_id=artifact_id or f"unsaved:{work_item.id}:gmail_triage_report",
            source_agent=WorkItemRoute.GMAIL_TRIAGE.value,
            approval_state=ApprovalState.PENDING.value,
            title="Read-only Gmail triage",
            summary=triage.summary[:240],
            metadata={
                "category": triage.category,
                "priority": triage.priority,
                "needs_reply": triage.needs_reply,
                "draft_created": False,
                "labels_modified": False,
                "send_enabled": False,
                "inline_context": True,
                "message_id": triage.message_id,
                "thread_id": triage.thread_id,
            },
        )
        updated = attach_artifact(
            work_item.model_copy(
                update={
                    "status": WorkItemStatus.DONE,
                    "confidence": max(work_item.confidence, triage.confidence),
                    "audit_notes": [
                        *work_item.audit_notes,
                        (
                            "Gmail Triage completed read-only inline-email classification; "
                            "Gmail writes remain blocked without selected Gmail context and approval."
                        ),
                    ],
                    "next_action": WorkItemNextAction(
                        action="review_gmail_triage",
                        agent=WorkItemRoute.GMAIL_TRIAGE,
                        description=(
                            "Review the read-only triage. Select the Gmail thread/message only "
                            "if a Gmail draft, label, send, or thread-specific action is needed."
                        ),
                        requires_approval=False,
                    ),
                }
            ).touch(),
            artifact,
        )
        _persist_artifact_and_event(
            updated,
            artifact,
            summary="Attached read-only Gmail triage report.",
            store=store,
        )
        return WorkflowRunResult(
            work_item=updated,
            route=WorkItemRoute.GMAIL_TRIAGE,
            status=updated.status,
            advanced=True,
            artifact_refs=[artifact],
            next_action=updated.next_action,
            human_summary=_gmail_triage_human_summary(triage),
            audit_notes=[
                (
                    "Gmail Triage completed read-only inline-email classification; no Gmail "
                    "draft, label, send, save, post, or external write was performed."
                )
            ],
        )
    blocker = WorkItemBlocker(
        code="gmail_context_required",
        message=(
            "Gmail Triage requires selected Gmail thread/message context or explicit live "
            "Gmail retrieval before it can summarize, triage, or draft a reply. No Gmail "
            "draft or send action was performed."
        ),
    )
    next_action = WorkItemNextAction(
        action="provide_gmail_context",
        agent=WorkItemRoute.GMAIL_TRIAGE,
        description=(
            "Select the Gmail thread/message or rerun with an explicit live Gmail "
            "retrieval scope, then continue the WorkItem."
        ),
        requires_approval=False,
    )
    return _blocked_result(
        work_item,
        (blocker,),
        next_action,
        store=store,
        route=WorkItemRoute.GMAIL_TRIAGE,
        audit_notes=[
            "Gmail Triage WorkItem route recognized; stopped at Gmail context gate."
        ],
    )


def _gmail_plan_allows_inline_read_only_triage(gmail_plan: Any) -> bool:
    operation = str(getattr(gmail_plan, "operation", "") or "").strip()
    return operation in {"single_message_triage", "draft_reply", "priority_grouping", ""}


def _inline_gmail_fixture_from_request(request_text: str) -> EmailFixture | None:
    text = " ".join(str(request_text or "").replace("\n", " ").split()).strip()
    if not text:
        return None
    lower = text.lower()
    if "body:" not in lower and "email:" not in lower:
        return None

    from_text = _extract_inline_email_field(text, "from", stop_fields=("subject", "body"))
    subject = _extract_inline_email_field(text, "subject", stop_fields=("body",)) or "Manual Gmail triage request"
    body = _extract_inline_email_field(text, "body", stop_fields=())
    if body:
        body = re.split(
            r"\b(?:please identify|return priority|do not create|do not apply|do not send|"
            r"do not save|do not post|do not reread|also keep track)\b",
            body,
            maxsplit=1,
            flags=re.I,
        )[0].strip(" .")
    if not body:
        return None

    parsed_name, parsed_email = parseaddr(from_text)
    sender_name = parsed_name or from_text
    sender_email = parsed_email
    return EmailFixture(
        subject=subject.strip(" .") or "Manual Gmail triage request",
        body=body.strip(),
        sender_name=sender_name.strip(" .,:;"),
        sender_email=sender_email.strip(),
        message_id="inline-read-only-message",
        thread_id="inline-read-only-thread",
        snippet=body[:240],
    )


def _extract_inline_email_field(
    text: str,
    field_name: str,
    *,
    stop_fields: tuple[str, ...],
) -> str:
    stop_pattern = "|".join(re.escape(field) for field in stop_fields)
    if stop_pattern:
        pattern = rf"\b{re.escape(field_name)}:\s*(.*?)(?=\b(?:{stop_pattern}):|\Z)"
    else:
        pattern = rf"\b{re.escape(field_name)}:\s*(.*)\Z"
    match = re.search(pattern, text, flags=re.I | re.S)
    if not match:
        return ""
    return " ".join(match.group(1).split()).strip(" .")


def _read_only_gmail_recommended_action(triage: Any) -> str:
    if bool(getattr(triage, "needs_reply", False)):
        return (
            "Reply appears warranted. Review the local draft-only guidance, then select the "
            "real Gmail thread/message before creating any Gmail draft, label, send, or "
            "thread-specific action."
        )
    return "No reply appears required from the sanitized inline text; review manually if context changes."


def _gmail_triage_human_summary(triage: Any) -> str:
    lines = [
        f"Read-only Gmail triage for {triage.sender_name or triage.sender_email or 'inline email'}",
        "",
        f"Priority: {triage.priority}.",
        f"Reply needed: {'yes' if triage.needs_reply else 'no'}.",
        f"Action owner: {'operator review' if triage.needs_reply else 'operator / no immediate reply'}.",
        f"Summary: {triage.summary}",
        f"Recommended action: {triage.recommended_action}",
    ]
    if triage.risk_flags:
        lines.append("Risks: " + ", ".join(triage.risk_flags) + ".")
    else:
        lines.append("Risks: no high-risk flags detected from the sanitized inline text.")
    lines.append(
        "No Gmail draft, label, send, save, post, or external write was performed."
    )
    if triage.triage_limitations:
        lines.extend(["", "Run notes"])
        lines.extend(f"* {item}" for item in triage.triage_limitations[:4])
    return "\n".join(lines).strip()


def _advance_chief_of_staff(
    work_item: WorkItem,
    *,
    request: WorkflowRunRequest,
    store: SQLiteStore | None,
    sdk_session: Any | None = None,
) -> WorkflowRunResult:
    request_text = request.request_text.strip() or work_item.request_text
    sdk_input = {
        "request": request_text,
        "work_item": {
            "id": work_item.id,
            "route": WorkItemRoute.CHIEF_OF_STAFF.value,
            "target": work_item.target.model_dump(mode="json"),
            "sources": [source.model_dump(mode="json") for source in work_item.sources[:8]],
        },
        "slack_context": work_item.target.metadata.get("slack_context", {}),
        "orchestrator_context": _specialist_orchestrator_context_payload(request, work_item),
        "side_effect_policy": (
            "read-only interpretation; no Slack post, Gmail send, calendar write, "
            "Airtable write, or repo write without explicit approval gates"
        ),
    }
    if request.live_sdk:
        typed_result = run_chief_of_staff_sdk(
            sdk_input,
            live=True,
            session=sdk_session,
            force_sdk_interpretation=True,
        )
        output = typed_result.output
        mode_note = "Chief of Staff live SDK interpretation executed for this WorkItem."
    else:
        output = plan_chief_of_staff_request(request_text, database_url=request.database_url)
        mode_note = "Chief of Staff deterministic dry-run planner executed for this WorkItem."

    artifact_id = ""
    output_payload = output.model_dump(mode="json")
    if store is not None:
        artifact_id = str(
            store.save_agent_run(
                agent_name=WorkItemRoute.CHIEF_OF_STAFF.value,
                input_payload=sdk_input,
                input_summary=request_text[:240],
                output=output_payload,
                model="sdk-live" if request.live_sdk else "fixture",
                dry_run=not request.live_sdk,
                status="success",
            )
        )
    artifact = WorkItemArtifactRef(
        artifact_type="chief_of_staff_plan",
        artifact_id=artifact_id or f"unsaved:{work_item.id}:chief_of_staff_plan",
        source_agent=WorkItemRoute.CHIEF_OF_STAFF.value,
        approval_state=ApprovalState.PENDING.value,
        title="Chief of Staff plan",
        summary=output.summary[:240],
        metadata={
            "workflow_type": output.recommended_route.workflow_type,
            "target_channel": output.recommended_route.target_channel,
            "slack_post_allowed": output.slack_post_allowed,
            "send_enabled": output.send_enabled,
        },
    )
    updated = attach_artifact(
        work_item.model_copy(
            update={
                "last_agent": WorkItemRoute.CHIEF_OF_STAFF.value,
                "status": WorkItemStatus.DONE,
                "confidence": max(work_item.confidence, 0.7),
                "audit_notes": [*work_item.audit_notes, mode_note, *output.audit_notes],
                "next_action": WorkItemNextAction(
                    action="review_chief_of_staff_plan",
                    agent=WorkItemRoute.CHIEF_OF_STAFF,
                    description=(
                        "Review the Chief of Staff plan before any live internal write or post."
                    ),
                    requires_approval=bool(output.approval_required),
                ),
            }
        ).touch(),
        artifact,
    )
    _persist_artifact_and_event(
        updated,
        artifact,
        summary="Attached Chief of Staff plan.",
        store=store,
    )
    return WorkflowRunResult(
        work_item=updated,
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=updated.status,
        advanced=True,
        artifact_refs=[artifact],
        next_action=updated.next_action,
        human_summary=output.summary,
        audit_notes=[mode_note, *output.audit_notes],
    )


def _advance_research(
    work_item: WorkItem,
    *,
    request: WorkflowRunRequest,
    store: SQLiteStore | None,
    sdk_session: Any | None = None,
) -> WorkflowRunResult:
    request_text = request.request_text.strip()
    selected_opportunity = selected_artifacts(work_item, "opportunity")
    target = ""
    if selected_opportunity and _should_research_selected_opportunity(request_text):
        target = selected_opportunity[0].title
    target = (
        target
        or _manual_primary_target(work_item)
        or work_item.target.name
        or normalize_target_text(
            request.request_text or work_item.request_text,
            WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        )
    )
    context_target = _slack_context_resolved_target(work_item)
    if context_target and _target_needs_slack_context_resolution(
        target,
        request.request_text or work_item.request_text,
    ):
        target = context_target
    work_item = work_item.model_copy(
        update={
            "target": work_item.target.model_copy(update={"name": target}),
            "last_agent": WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        }
    )
    if _research_requires_source_bundle(request_text) and not _work_item_has_source_bundle_context(
        work_item,
        request,
    ):
        blocker = WorkItemBlocker(
            code="source_bundle_required",
            message=(
                "The request constrained Business Research to a provided source bundle, "
                "but no source bundle, source ids, context file, or WorkItem sources were attached."
            ),
        )
        return _blocked_result(
            work_item,
            (blocker,),
            WorkItemNextAction(
                action="attach_source_bundle",
                agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                description=(
                    "Attach the source bundle or source ids, then rerun Business Research."
                ),
            ),
            store=store,
        )
    if _target_needs_slack_context_resolution(target, ""):
        blocker = WorkItemBlocker(
            code="selected_slack_context_required",
            message=(
                "The request refers to a selected Slack thread, prior post, or company being "
                "discussed, but the Slack context did not resolve a concrete company or target."
            ),
        )
        return _blocked_result(
            work_item,
            (blocker,),
            WorkItemNextAction(
                action="attach_selected_slack_context",
                agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                description=(
                    "Attach the selected Slack thread or restate the company name, then rerun "
                    "Business Research."
                ),
            ),
            store=store,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            audit_notes=["Business Research blocked before live search because Slack context did not resolve the target."],
        )
    ready = research_ready(work_item)
    if not ready.ready:
        return _blocked_result(work_item, ready.blockers, ready.next_action, store=store)

    comparison_names = _comparison_company_names(request.request_text or work_item.request_text)
    if comparison_names is not None:
        return _advance_company_comparison_research(
            work_item,
            request=request,
            company_a=comparison_names[0],
            company_b=comparison_names[1],
            store=store,
        )

    if _should_run_zotero_article_brief(work_item, request.request_text):
        return _advance_zotero_article_research(
            work_item,
            request=request,
            store=store,
            sdk_session=sdk_session,
        )

    if _should_run_zotero_collection_brief(work_item, request.request_text):
        return _advance_zotero_collection_research(work_item, request=request, store=store)

    if request.reuse_existing_research and _has_reusable_company_profile(work_item):
        return _reuse_existing_company_research(
            work_item,
            request=request,
            store=store,
        )

    metadata: dict[str, object] = {}
    if request.live_search:
        max_results = _effective_max_results(request)
        query_builder = _business_research_query_builder_for_request(request)
        profile, metadata = retrieve_company_profile_live(
            company=target,
            max_results=(
                max(max_results, 8)
                if query_builder is not None
                else max_results
            ),
            query_builder=query_builder,
            agents_web_search_max_calls=request.hosted_web_search_max_calls,
            agents_web_search_parallel=not _is_slack_conservative_cost_profile(request),
        )
        audit_notes = ["Live company retrieval executed.", *metadata.get("debug_notes", [])]
        if query_builder is not None:
            audit_notes.append(
                "Current-activity query deepening was included in the initial retrieval pass."
            )
    else:
        profile = research_company_fixture(company_name=target)
        audit_notes = ["Fixture company research executed; no live APIs were called."]
    retrieval_memory_id = _persist_retrieval_tool_memory(
        metadata,
        object_id=f"work_item_company_research:{target}",
        store=store,
    )
    if retrieval_memory_id is not None:
        audit_notes.append(f"Retrieval tool performance memory saved: {retrieval_memory_id}.")
    if store is not None and metadata:
        _record_retrieval_cost_event(
            work_item,
            metadata=metadata,
            cost_profile=request.cost_profile,
            hosted_web_search_max_calls=request.hosted_web_search_max_calls,
            store=store,
        )

    source_refs = [
        _work_item_source_ref_from_company_source(source) for source in profile.sources[:12]
    ]
    include_contact_enrichment = _should_include_contact_enrichment(request)
    contact_enrichment = (
        build_contact_enrichment_artifact(
            company_name=profile.name,
            sources=list(profile.sources),
        )
        if include_contact_enrichment
        else None
    )
    artifact_id = ""
    if store is not None:
        artifact_id = str(store.save_company(profile))
    artifact = WorkItemArtifactRef(
        artifact_type="company_profile",
        artifact_id=artifact_id or f"unsaved:{profile.name}",
        source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        approval_state="approved_for_research",
        title=profile.name,
        summary=(profile.fit_summary or profile.description)[:240],
        metadata={
            "consulting_fit_score": profile.consulting_fit_score,
            "confidence_score": profile.confidence_score,
            "source_refs": [source.model_dump(mode="json") for source in profile.sources[:8]],
            "retrieval_diagnostics": metadata.get("retrieval_diagnostics"),
        },
    )
    contact_artifact: WorkItemArtifactRef | None = None
    if contact_enrichment is not None and store is not None:
        contact_artifact_id = str(
            store.save_agent_run(
                agent_name=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                input_payload={"company_name": profile.name, "artifact": "contact_enrichment"},
                input_summary=f"Contact enrichment candidates for {profile.name}",
                output=contact_enrichment.model_dump(mode="json"),
                dry_run=True,
                status="success",
            )
        )
    else:
        contact_artifact_id = ""
    if contact_enrichment is not None:
        contact_artifact = WorkItemArtifactRef(
            artifact_type="contact_candidates",
            artifact_id=contact_artifact_id or f"unsaved:{profile.name}:contacts",
            source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
            approval_state="needs_confirmation",
            title=f"Contact candidates for {profile.name}",
            summary=(
                f"{len(contact_enrichment.candidates)} source-backed candidate(s); "
                + "; ".join(contact_enrichment.missing[:2])
            )[:240],
            metadata=contact_enrichment.model_dump(mode="json"),
        )
    else:
        audit_notes.append("Contact enrichment skipped by cost profile or request scope.")
    work_item_with_profile = attach_artifact(
        work_item.model_copy(update={"sources": [*work_item.sources, *source_refs]}),
        artifact,
    )
    work_item = (
        attach_artifact(work_item_with_profile, contact_artifact)
        if contact_artifact is not None
        else work_item_with_profile
    )
    work_item = work_item.model_copy(
        update={
            "confidence": float(profile.confidence_score or 0),
            "audit_notes": [*work_item.audit_notes, *audit_notes],
            "next_action": WorkItemNextAction(
                action="review_company_profile",
                agent=WorkItemRoute.OPPORTUNITY_SCOUT,
                description=(
                    "Review the source-backed profile, then scout matching opportunities if useful."
                ),
                command_hint=f"keystone work-items advance {work_item.id}",
            ),
        }
    )
    work_item = work_item.model_copy(update={"status": derive_case_status(work_item)}).touch()
    _persist_artifact_and_event(
        work_item,
        artifact,
        summary=f"Attached company profile for {profile.name}.",
        store=store,
    )
    if contact_artifact is not None:
        _persist_artifact_and_event(
            work_item,
            contact_artifact,
            summary=f"Attached contact enrichment candidates for {profile.name}.",
            store=store,
        )
    result_artifacts = [artifact, *([contact_artifact] if contact_artifact is not None else [])]
    return WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=work_item.status,
        advanced=True,
        artifact_refs=result_artifacts,
        next_action=work_item.next_action,
        human_summary=(
            "Business Research Analyst attached a source-backed company profile "
            f"for {profile.name}."
            + (
                " Contact enrichment found "
                f"{len(contact_enrichment.candidates)} candidate channel(s)."
                if contact_enrichment is not None
                else " Contact enrichment was skipped for this cost-conscious run."
            )
        ),
        audit_notes=audit_notes,
    )


def _business_research_query_builder_for_request(
    request: WorkflowRunRequest,
) -> Callable[[str, str | None], list[str]] | None:
    raw_request_text = str(request.request_text or "")
    request_text = raw_request_text.lower()
    if _is_slack_conservative_cost_profile(request):
        return _slack_conservative_company_research_query_builder(request)
    if not re.search(
        r"\b(?:2026|current|recent|latest|this year|what .* doing|activity|update|roadmap)\b",
        request_text,
    ):
        return None
    focus_terms = _request_focus_terms_for_search(latest_user_request(raw_request_text))

    def build_queries(company: str, company_url: str | None = None) -> list[str]:
        base_queries = build_company_research_queries(company, company_url)
        normalized_company = company.strip()
        focus_phrase = " ".join(
            term for term in focus_terms if term.lower() != normalized_company.lower()
        )
        focus_queries = (
            [
                f"{normalized_company} 2026 {focus_phrase}",
                f"{normalized_company} {focus_phrase} independent coverage 2026",
                f"{normalized_company} {focus_phrase} official announcement 2026",
            ]
            if focus_phrase
            else []
        )
        current_activity_queries = [
            f"{normalized_company} 2026 company update",
            f"{normalized_company} 2026 independent coverage",
            f"{normalized_company} 2026 third party validation",
            f"{normalized_company} 2026 funding valuation revenue growth",
            f"{normalized_company} 2026 partnership customers product roadmap",
            f"{normalized_company} 2026 hiring headcount leadership",
            f"{normalized_company} 2026 medical AI healthcare platform",
            f"{normalized_company} latest news 2026",
            f"{normalized_company} recent funding partnership launch",
            f"{normalized_company} careers hiring 2026",
            f"site:prnewswire.com {normalized_company} 2026",
            f"site:businesswire.com {normalized_company} 2026",
            f"site:fiercehealthcare.com {normalized_company} 2026",
            f"site:mobihealthnews.com {normalized_company} 2026",
            f"site:healthcaredive.com {normalized_company} 2026",
            f"site:medcitynews.com {normalized_company} 2026",
            f"site:statnews.com {normalized_company} 2026",
            f"site:crunchbase.com/organization {normalized_company}",
            f"site:linkedin.com/company {normalized_company} hiring 2026",
        ]
        return list(dict.fromkeys([*focus_queries, *current_activity_queries, *base_queries]))

    return build_queries


def _slack_conservative_company_research_query_builder(
    request: WorkflowRunRequest,
) -> Callable[[str, str | None], list[str]]:
    focus_terms = _request_focus_terms_for_search(latest_user_request(request.request_text))

    def build_queries(company: str, company_url: str | None = None) -> list[str]:
        normalized_company = company.strip()
        focus_phrase = " ".join(
            term for term in focus_terms if term.lower() != normalized_company.lower()
        )
        company_domain = ""
        if company_url:
            parsed = urlparse(company_url if "://" in company_url else f"https://{company_url}")
            company_domain = parsed.netloc.removeprefix("www.")
        queries = [
            f"{normalized_company} official website",
            f"{normalized_company} 2026 recent news partnership funding",
            f"{normalized_company} 2026 independent coverage",
            f"{normalized_company} 2026 product platform customers",
            f"{normalized_company} 2026 leadership hiring headcount",
            f"site:prnewswire.com {normalized_company} 2026 partnership funding",
            f"site:businesswire.com {normalized_company} 2026 partnership funding",
            f"site:fiercehealthcare.com {normalized_company} 2026",
        ]
        if focus_phrase:
            queries.insert(1, f"{normalized_company} 2026 {focus_phrase}")
            queries.insert(2, f"{normalized_company} {focus_phrase} independent coverage 2026")
        if company_domain:
            queries.insert(0, f"site:{company_domain} {normalized_company} about product")
        return list(dict.fromkeys(query for query in queries if query.strip()))[
            : _slack_research_query_limit(request)
        ]

    return build_queries


def _is_slack_conservative_cost_profile(request: WorkflowRunRequest) -> bool:
    return str(request.cost_profile or "").strip().lower() in _SLACK_COST_CONTROLLED_PROFILES


def _slack_research_query_limit(request: WorkflowRunRequest) -> int:
    profile = str(request.cost_profile or "").strip().lower()
    if profile == "slack_research_deep":
        return 16
    if profile == "slack_research_balanced":
        return 12
    return 10


def _should_include_contact_enrichment(request: WorkflowRunRequest) -> bool:
    if request.include_contact_enrichment:
        return True
    text = str(request.request_text or "").lower()
    return bool(
        re.search(
            r"\b(?:contact|email|linkedin|reach out|outreach|business development|partnerships contact)\b",
            text,
        )
    )


def _has_reusable_company_profile(work_item: WorkItem) -> bool:
    return any(ref.artifact_type == "company_profile" for ref in work_item.artifact_refs)


def _reuse_existing_company_research(
    work_item: WorkItem,
    *,
    request: WorkflowRunRequest,
    store: SQLiteStore | None,
) -> WorkflowRunResult:
    company_profiles = [
        ref for ref in work_item.artifact_refs if ref.artifact_type == "company_profile"
    ]
    latest_profile = company_profiles[-1]
    note = (
        "Reused existing source-backed company research for Slack follow-up; "
        "no new live retrieval was run."
    )
    reused_item = work_item.model_copy(
        update={
            "last_agent": WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
            "audit_notes": list(dict.fromkeys([*work_item.audit_notes, note])),
            "next_action": WorkItemNextAction(
                action="answer_from_existing_research_or_request_targeted_deepening",
                agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                description=(
                    "Answer from existing WorkItem artifacts; run targeted deeper research "
                    "only if the current sources do not support the follow-up."
                ),
            ),
        }
    ).touch()
    record_event(
        reused_item,
        event_type="research_reused",
        actor="business_research_analyst",
        summary="Reused existing company research for Slack follow-up.",
        metadata={
            "cost_profile": request.cost_profile,
            "artifact_type": latest_profile.artifact_type,
            "artifact_id": latest_profile.artifact_id,
            "live_search": False,
            "reason": "reuse_existing_research",
        },
        store=store,
    )
    return WorkflowRunResult(
        work_item=reused_item,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=reused_item.status,
        advanced=True,
        artifact_refs=[latest_profile],
        next_action=reused_item.next_action,
        human_summary=(
            "Business Research Analyst reused the latest source-backed company profile "
            "for this Slack follow-up instead of running another broad retrieval pass."
        ),
        audit_notes=[note],
    )


def _request_focus_terms_for_search(text: str, *, max_terms: int = 5) -> list[str]:
    """Extract compact request-specific terms for generic query focusing."""

    stopwords = {
        "about",
        "between",
        "could",
        "details",
        "doing",
        "focus",
        "look",
        "need",
        "other",
        "please",
        "recent",
        "research",
        "specific",
        "still",
        "there",
        "these",
        "this",
        "want",
        "what",
        "with",
    }
    terms: list[str] = []
    for match in re.finditer(r"\b[A-Za-z][A-Za-z0-9-]{3,}\b", str(text or "")):
        term = match.group(0).lower()
        if term in stopwords or re.fullmatch(r"20\d{2}", term):
            continue
        if term.endswith("ies") and len(term) > 6:
            term = f"{term[:-3]}y"
        elif term.endswith("s") and len(term) > 5:
            term = term[:-1]
        terms.append(term)
    return list(dict.fromkeys(terms))[:max_terms]


def _research_requires_source_bundle(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:provided\s+source\s+bundle|attached\s+source\s+bundle|"
            r"source\s+bundle\s+only|attached\s+sources?|"
            r"using\s+these\s+sources\s+only|these\s+sources\s+only|"
            r"provided\s+sources?\s+only|attached\s+sources?\s+only|"
            r"source[- ]id(?:s)?\s+only|cite\s+(?:every|all)\s+"
            r"(?:factual\s+)?claims?\s+to\s+(?:a\s+)?source[- ]id)\b",
            text,
            flags=re.I,
        )
    )


def _work_item_has_source_bundle_context(
    work_item: WorkItem,
    request: WorkflowRunRequest,
) -> bool:
    if request.context_file_path or request.external_context:
        return True
    if work_item.sources:
        return True
    metadata = work_item.target.metadata
    return bool(
        metadata.get("external_context")
        or metadata.get("source_bundle")
        or metadata.get("slack_context")
    )


def _advance_company_comparison_research(
    work_item: WorkItem,
    *,
    request: WorkflowRunRequest,
    company_a: str,
    company_b: str,
    store: SQLiteStore | None,
) -> WorkflowRunResult:
    profile_a = research_company_fixture(company_name=company_a)
    profile_b = research_company_fixture(company_name=company_b)
    comparison = compare_company_profiles_for_decision(
        profile_a,
        profile_b,
        decision_goal=request.request_text or work_item.request_text,
    )
    artifact_id = ""
    if store is not None:
        artifact_id = str(
            store.save_agent_run(
                agent_name=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                input_payload={
                    "company_a": company_a,
                    "company_b": company_b,
                    "artifact": "company_comparison",
                },
                input_summary=f"{company_a} vs {company_b}",
                output=comparison.model_dump(mode="json"),
                dry_run=True,
                status="success",
            )
        )
    source_refs = [
        *[_work_item_source_ref_from_company_source(source) for source in profile_a.sources[:6]],
        *[_work_item_source_ref_from_company_source(source) for source in profile_b.sources[:6]],
    ]
    artifact = WorkItemArtifactRef(
        artifact_type="company_comparison",
        artifact_id=artifact_id or f"unsaved:{company_a}:vs:{company_b}",
        source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        approval_state="approved_for_research",
        title=f"{company_a} vs {company_b}",
        summary=(comparison.recommendation or comparison.next_step)[:240],
        metadata={
            "company_a": company_a,
            "company_b": company_b,
            "decision_criteria": list(comparison.decision_criteria),
            "recommended_company": comparison.recommended_company,
            "side_by_side_entries": [
                entry.model_dump(mode="json") for entry in comparison.side_by_side_entries
            ],
            "evidence_gaps": list(comparison.evidence_gaps),
        },
    )
    work_item = attach_artifact(
        work_item.model_copy(
            update={
                "target": work_item.target.model_copy(update={"name": artifact.title}),
                "sources": [*work_item.sources, *source_refs],
                "last_agent": WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                "audit_notes": [
                    *work_item.audit_notes,
                    "Fixture company comparison executed; no live APIs were called.",
                ],
                "next_action": WorkItemNextAction(
                    action="review_company_comparison",
                    agent=WorkItemRoute.OPPORTUNITY_SCOUT,
                    description=(
                        "Review the side-by-side comparison before using it for "
                        "opportunity scouting or outreach decisions."
                    ),
                    command_hint=f"keystone work-items advance {work_item.id}",
                ),
            }
        ),
        artifact,
    )
    work_item = work_item.model_copy(update={"status": derive_case_status(work_item)}).touch()
    _persist_artifact_and_event(
        work_item,
        artifact,
        summary=f"Attached company comparison for {company_a} vs {company_b}.",
        store=store,
    )
    return WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=work_item.status,
        advanced=True,
        artifact_refs=[artifact],
        next_action=work_item.next_action,
        human_summary=(
            "Business Research Analyst attached a side-by-side company comparison "
            f"for {company_a} vs {company_b}."
        ),
        audit_notes=["Fixture company comparison executed; no live APIs were called."],
    )


def _advance_zotero_collection_research(
    work_item: WorkItem,
    *,
    request: WorkflowRunRequest,
    store: SQLiteStore | None,
) -> WorkflowRunResult:
    hint = (
        extract_zotero_collection_hint(request.request_text)
        or extract_zotero_collection_hint(work_item.target.name)
        or work_item.target.name
    )
    try:
        brief, paragraphs = build_zotero_collection_research_brief(
            hint,
            research_goal=request.request_text or work_item.request_text,
        )
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        blocker = WorkItemBlocker(
            code="zotero_collection_resolution_failed",
            message=(
                "Business Research Analyst could not resolve the requested Zotero "
                f"collection from local cache: {exc}"
            ),
        )
        return _blocked_result(
            work_item,
            (blocker,),
            WorkItemNextAction(
                action="provide_zotero_collection",
                agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                description=(
                    "Provide a unique Zotero collection prefix such as `LH 01`, "
                    "or refresh the Zotero import cache."
                ),
            ),
            store=store,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            audit_notes=["Zotero collection research brief blocked before artifact creation."],
        )
    source_refs = [
        _work_item_source_ref_from_research_source(
            source,
            supported_claim=f"Source included in {brief.target_name}.",
        )
        for source in brief.sources[:12]
    ]
    artifact_id = ""
    if store is not None:
        artifact_id = str(
            store.save_agent_run(
                agent_name=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                input_payload={"request_text": request.request_text, "collection_hint": hint},
                input_summary=f"Zotero collection research brief: {brief.target_name}",
                output=brief.model_dump(mode="json"),
                dry_run=True,
                status="success",
            )
        )
    artifact = WorkItemArtifactRef(
        artifact_type="research_brief",
        artifact_id=artifact_id or f"unsaved:{brief.target_name}",
        source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        approval_state=ApprovalState.APPROVED_FOR_RESEARCH.value,
        title=brief.target_name,
        summary=brief.summary[:240],
        metadata={
            "target_type": brief.target_type,
            "source_count": len(brief.sources),
            "article_summary_count": len(brief.article_summaries),
            "source_refs": [source.model_dump(mode="json") for source in brief.sources[:8]],
        },
    )
    work_item = attach_artifact(
        work_item.model_copy(
            update={
                "sources": [*work_item.sources, *source_refs],
                "target": work_item.target.model_copy(
                    update={"name": brief.target_name, "object_type": brief.target_type}
                ),
            }
        ),
        artifact,
    )
    audit_notes = [
        "Local Zotero collection research brief executed; no live APIs were called.",
        "Business Research Analyst used ResearchBrief output instead of CompanyProfile.",
    ]
    work_item = work_item.model_copy(
        update={
            "confidence": 0.9,
            "audit_notes": [*work_item.audit_notes, *audit_notes],
            "next_action": WorkItemNextAction(
                action="review_research_brief",
                agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                description=(
                    "Review the source-backed Zotero research brief and request live/page "
                    "extraction if it will be used externally."
                ),
                command_hint=f"keystone work-items show {work_item.id}",
            ),
        }
    )
    work_item = work_item.model_copy(update={"status": derive_case_status(work_item)}).touch()
    _persist_artifact_and_event(
        work_item,
        artifact,
        summary=f"Attached Zotero research brief for {brief.target_name}.",
        store=store,
    )
    return WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=work_item.status,
        advanced=True,
        artifact_refs=[artifact],
        next_action=work_item.next_action,
        human_summary=_research_brief_human_summary(brief, paragraphs),
        audit_notes=audit_notes,
    )


def _advance_zotero_article_research(
    work_item: WorkItem,
    *,
    request: WorkflowRunRequest,
    store: SQLiteStore | None,
    sdk_session: Any | None = None,
) -> WorkflowRunResult:
    query = (
        extract_zotero_article_query(request.request_text)
        or extract_zotero_article_query(work_item.target.name)
        or work_item.target.name
    )
    try:
        brief, paragraphs = build_zotero_article_research_brief(
            query,
            research_goal=request.request_text or work_item.request_text,
        )
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        blocker = WorkItemBlocker(
            code="zotero_article_resolution_failed",
            message=(
                "Business Research Analyst could not resolve the requested Zotero "
                f"article or item from local cache: {exc}"
            ),
        )
        return _blocked_result(
            work_item,
            (blocker,),
            WorkItemNextAction(
                action="provide_zotero_article_query",
                agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                description=(
                    "Provide a DOI, PMID, NCT identifier, title fragment, or refresh "
                    "the Zotero import cache."
                ),
            ),
            store=store,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            audit_notes=["Zotero article search blocked before artifact creation."],
        )
    audit_notes = [
        "Local Zotero article search executed.",
        "Business Research Analyst used ResearchBrief output instead of CompanyProfile.",
    ]
    retrieval_metadata: dict[str, object] = {}
    if request.live_sdk:
        try:
            source_context, extraction_notes, retrieval_metadata = build_zotero_live_source_context(
                brief,
                live=request.live_search,
                max_sources=min(max(request.max_results, 1), 4),
            )
            sdk_result = run_business_research_analyst_research_brief_sdk(
                ResearchSDKInput(
                    target_name=brief.target_name,
                    target_type=brief.target_type,
                    research_goal=request.request_text or work_item.request_text,
                    source_context="\n\n".join(
                        part
                        for part in (
                            _specialist_orchestrator_context_text(request, work_item),
                            source_context,
                        )
                        if part
                    ),
                    local_context_source_ids=("zotero_import_cache",),
                ),
                live=True,
                session=sdk_session,
            )
        except Exception as exc:
            blocker = WorkItemBlocker(
                code="zotero_article_live_synthesis_failed",
                message=(
                    "Business Research Analyst resolved the Zotero item, but live SDK "
                    f"synthesis failed: {type(exc).__name__}: {exc}"
                ),
            )
            return _blocked_result(
                work_item,
                (blocker,),
                WorkItemNextAction(
                    action="retry_zotero_article_live_synthesis",
                    agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                    description=(
                        "Retry with --live-search --live-sdk after confirming model and "
                        "page-extraction credentials."
                    ),
                ),
                store=store,
                route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                audit_notes=[
                    *audit_notes,
                    "Zotero article live SDK synthesis failed before artifact creation.",
                ],
            )
        brief = _merge_research_brief_sources(sdk_result.output, fallback=brief)
        paragraphs = _article_summary_lines_from_brief(brief) or paragraphs
        audit_notes.extend(
            [
                "Live SDK synthesis executed for the Zotero article research brief.",
                *extraction_notes,
            ]
        )
    elif request.live_search:
        source_context, extraction_notes, retrieval_metadata = build_zotero_live_source_context(
            brief,
            live=True,
            max_sources=min(max(request.max_results, 1), 4),
        )
        if source_context:
            audit_notes.append("Live source extraction executed without SDK synthesis.")
        audit_notes.extend(extraction_notes)
    source_refs = [
        _work_item_source_ref_from_research_source(
            source,
            supported_claim=f"Source matched for {brief.target_name}.",
        )
        for source in brief.sources[:8]
    ]
    artifact_id = ""
    if store is not None:
        artifact_id = str(
            store.save_agent_run(
                agent_name=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                input_payload={"request_text": request.request_text, "zotero_query": query},
                input_summary=f"Zotero article research brief: {brief.target_name}",
                output=brief.model_dump(mode="json"),
                dry_run=True,
                status="success",
            )
        )
    artifact = WorkItemArtifactRef(
        artifact_type="research_brief",
        artifact_id=artifact_id or f"unsaved:{brief.target_name}",
        source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        approval_state=ApprovalState.APPROVED_FOR_RESEARCH.value,
        title=brief.target_name,
        summary=brief.summary[:240],
        metadata={
            "target_type": brief.target_type,
            "source_count": len(brief.sources),
            "article_summary_count": len(brief.article_summaries),
            "retrieval": retrieval_metadata if request.live_search or request.live_sdk else {},
            "source_refs": [source.model_dump(mode="json") for source in brief.sources[:8]],
        },
    )
    work_item = attach_artifact(
        work_item.model_copy(
            update={
                "sources": [*work_item.sources, *source_refs],
                "target": work_item.target.model_copy(
                    update={"name": brief.target_name, "object_type": brief.target_type}
                ),
            }
        ),
        artifact,
    )
    if not request.live_search and not request.live_sdk:
        audit_notes.append("No live APIs were called.")
    work_item = work_item.model_copy(
        update={
            "confidence": 0.85,
            "audit_notes": [*work_item.audit_notes, *audit_notes],
            "next_action": WorkItemNextAction(
                action="review_research_brief",
                agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                description=(
                    "Review the source-backed Zotero research brief and request live/page "
                    "extraction if detailed methods or eligibility fields are needed."
                ),
                command_hint=f"keystone work-items show {work_item.id}",
            ),
        }
    )
    work_item = work_item.model_copy(update={"status": derive_case_status(work_item)}).touch()
    _persist_artifact_and_event(
        work_item,
        artifact,
        summary=f"Attached Zotero article research brief for {brief.target_name}.",
        store=store,
    )
    return WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=work_item.status,
        advanced=True,
        artifact_refs=[artifact],
        next_action=work_item.next_action,
        human_summary=_research_brief_human_summary(brief, paragraphs),
        audit_notes=audit_notes,
    )


def _should_research_selected_opportunity(request_text: str) -> bool:
    normalized = " ".join(str(request_text or "").strip().lower().split())
    if normalized in {"", "continue", "resume"}:
        return True
    return normalized in {
        "run deeper source-backed business research for this workitem.",
        "find a better source-backed contact or destination for this outreach workitem.",
    }


def _advance_opportunity(
    work_item: WorkItem,
    *,
    request: WorkflowRunRequest,
    store: SQLiteStore | None,
) -> WorkflowRunResult:
    request_text = request.request_text.strip()
    planned_topic = _manual_primary_target(work_item)
    topic_input = (
        work_item.target.name
        if request_text.lower() in {"", "continue", "resume"}
        else planned_topic or request_text or work_item.request_text or work_item.target.name
    )
    topic = normalize_target_text(topic_input, WorkItemRoute.OPPORTUNITY_SCOUT)
    work_item = work_item.model_copy(
        update={
            "target": work_item.target.model_copy(update={"name": topic, "object_type": "topic"}),
            "last_agent": WorkItemRoute.OPPORTUNITY_SCOUT.value,
        }
    )
    ready = opportunity_ready(work_item)
    if not ready.ready:
        return _blocked_result(work_item, ready.blockers, ready.next_action, store=store)

    metadata: dict[str, object] = {}
    if request.live_search:
        def record_planner_cost(sdk_result: Any) -> None:
            if store is None:
                return
            _record_workflow_sdk_cost_event(
                work_item,
                event_type="workflow_sdk_usage",
                summary="Recorded Opportunity Search Planner SDK usage.",
                agent_name="opportunity_search_planner",
                usage=getattr(sdk_result, "usage", None),
                cost=getattr(sdk_result, "cost", None),
                request_cache=getattr(sdk_result, "request_cache", None),
                store=store,
            )

        search_plan = (
            resolve_opportunity_search_plan(
                topic,
                desired_count=_effective_max_results(request),
                live=bool(request.live_sdk),
                planner_context=_specialist_orchestrator_context_text(request, work_item),
                cost_callback=record_planner_cost,
            )
            if request.live_sdk
            else None
        )
        scout_result, metadata = run_opportunity_scout_live(
            topic=topic,
            max_results=_effective_max_results(request),
            search_plan=search_plan,
        )
        audit_notes = ["Live opportunity retrieval executed.", *metadata.get("debug_notes", [])]
        if search_plan is not None:
            audit_notes.append("Opportunity Scout used the named-agent live search planning path.")
        retrieval_memory_id = _persist_retrieval_tool_memory(
            metadata,
            object_id=f"work_item_opportunity_scout:{topic}",
            store=store,
        )
        if retrieval_memory_id is not None:
            audit_notes.append(f"Retrieval tool performance memory saved: {retrieval_memory_id}.")
        if store is not None and metadata:
            _record_retrieval_cost_event(
                work_item,
                metadata=metadata,
                cost_profile=request.cost_profile,
                hosted_web_search_max_calls=request.hosted_web_search_max_calls,
                store=store,
                agent_name=WorkItemRoute.OPPORTUNITY_SCOUT.value,
            )
    else:
        if _is_strict_role_recency_search(topic):
            audit_notes = [
                "Fixture opportunity scout executed; no live APIs were called.",
                (
                    "Strict role-recency filters suppressed generic fixture opportunities "
                    "because no fixture record proves the requested role, recency, remote, "
                    "and part-time/fractional constraints."
                ),
            ]
            blocker = WorkItemBlocker(
                code="no_strong_opportunity_matches",
                message=(
                    "No fixture-backed opportunity matched the strict role, recency, "
                    "remote/location, and part-time/fractional filters."
                ),
            )
            return _blocked_result(
                work_item,
                (blocker,),
                WorkItemNextAction(
                    action="enable_live_search_or_broaden_filters",
                    agent=WorkItemRoute.OPPORTUNITY_SCOUT,
                    description=(
                        "Enable live search for this narrow role scan or broaden one of "
                        "the hard filters before presenting matches."
                    ),
                ),
                store=store,
                route=WorkItemRoute.OPPORTUNITY_SCOUT,
                audit_notes=audit_notes,
            )
        if _is_strict_partnership_signal_search(topic):
            audit_notes = [
                "Fixture opportunity scout executed; no live APIs were called.",
                (
                    "Hard partnership-signal filters suppressed generic fixture opportunities "
                    "because no fixture record proves the requested partnership, recency, "
                    "remote/U.S., and exclusion constraints."
                ),
            ]
            blocker = WorkItemBlocker(
                code="weak_adjacent_matches",
                message=(
                    "No fixture-backed opportunity strongly matched the partnership-signal, "
                    "recency, geography/work-mode, and exclusion filters."
                ),
            )
            return _blocked_result(
                work_item,
                (blocker,),
                WorkItemNextAction(
                    action="enable_live_search_or_broaden_filters",
                    agent=WorkItemRoute.OPPORTUNITY_SCOUT,
                    description=(
                        "Enable live search for this hard-filtered partnership scan or "
                        "broaden one of the filters before presenting matches."
                    ),
                ),
                store=store,
                route=WorkItemRoute.OPPORTUNITY_SCOUT,
                audit_notes=audit_notes,
            )
        scout_result = scout_opportunities_fixture(
            topic=topic,
            max_results=_effective_max_results(request),
        )
        audit_notes = ["Fixture opportunity scout executed; no live APIs were called."]

    if _is_source_summary_opportunity_request(work_item):
        return _opportunity_source_summary_result(
            work_item,
            topic=topic,
            metadata=metadata,
            audit_notes=audit_notes,
            store=store,
        )

    saved_ids: list[str] = []
    if store is not None:
        saved_ids = [str(item_id) for item_id in store.save_opportunity_scout_result(scout_result)]
    artifacts: list[WorkItemArtifactRef] = []
    source_refs: list[WorkItemSourceRef] = []
    for index, record in enumerate(scout_result.records):
        record_source_refs = [
            _work_item_source_ref_from_opportunity_source(source) for source in record.sources[:5]
        ]
        source_refs.extend(record_source_refs)
        artifact = WorkItemArtifactRef(
            artifact_type="opportunity",
            artifact_id=saved_ids[index] if index < len(saved_ids) else f"unsaved:{index + 1}",
            source_agent=WorkItemRoute.OPPORTUNITY_SCOUT.value,
            approval_state="pending",
            title=record.company_name,
            summary=record.why_now_signal[:240],
            metadata={
                "priority_score": record.priority_score,
                "opportunity_type": record.opportunity_type,
                "recommended_next_step": record.recommended_next_step,
                "source_refs": [ref.model_dump(mode="json") for ref in record_source_refs],
                "retrieval_diagnostics": metadata.get("retrieval_diagnostics"),
            },
        )
        artifacts.append(artifact)
        work_item = attach_artifact(work_item, artifact)

    if not artifacts:
        if _is_strict_role_recency_search(topic):
            next_action = WorkItemNextAction(
                action="broaden_opportunity_search",
                agent=WorkItemRoute.OPPORTUNITY_SCOUT,
                description=(
                    "Relax one hard filter, such as role title, fractional/advisory "
                    "status, remote/U.S. verification, or the recency window, then rerun."
                ),
            )
            work_item = work_item.model_copy(
                update={
                    "status": WorkItemStatus.DONE,
                    "audit_notes": [*work_item.audit_notes, *audit_notes],
                    "next_action": next_action,
                }
            ).touch()
            if store is not None:
                record_event(
                    work_item,
                    event_type="advance_limited",
                    summary=(
                        "Opportunity Scout found no exact source-backed matches under "
                        "the requested hard filters."
                    ),
                    metadata={
                        "filtered_candidate_count": len(scout_result.filtered_candidates),
                        "review_candidate_count": len(scout_result.review_candidates),
                        "constraint_relaxation_suggestion": (
                            scout_result.constraint_relaxation_suggestion
                        ),
                    },
                    store=store,
                )
                store.save_work_item(work_item)
            return WorkflowRunResult(
                work_item=work_item,
                route=WorkItemRoute.OPPORTUNITY_SCOUT,
                status=work_item.status,
                advanced=True,
                artifact_refs=[],
                next_action=next_action,
                human_summary=_opportunity_no_exact_match_human_summary(scout_result),
                audit_notes=audit_notes,
            )
        blocker = WorkItemBlocker(
            code="no_opportunities_found",
            message="Opportunity Scout did not produce a source-backed opportunity record.",
        )
        return _blocked_result(
            work_item,
            (blocker,),
            WorkItemNextAction(
                action="broaden_opportunity_search",
                agent=WorkItemRoute.OPPORTUNITY_SCOUT,
                description="Broaden search terms or enable live search for a wider scan.",
            ),
            store=store,
            audit_notes=audit_notes,
        )

    work_item = work_item.model_copy(
        update={
            "confidence": max(record.priority_score for record in scout_result.records) / 100,
            "sources": [*work_item.sources, *source_refs],
            "audit_notes": [*work_item.audit_notes, *audit_notes],
            "next_action": WorkItemNextAction(
                action="review_opportunities",
                agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                description=(
                    "Review the opportunity records and run company research for any "
                    "priority target."
                ),
                command_hint=f"keystone work-items advance {work_item.id}",
            ),
        }
    )
    work_item = work_item.model_copy(update={"status": derive_case_status(work_item)}).touch()
    for artifact in artifacts:
        _persist_artifact_and_event(
            work_item,
            artifact,
            summary=f"Attached opportunity for {artifact.title}.",
            store=store,
        )
    return WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.OPPORTUNITY_SCOUT,
        status=work_item.status,
        advanced=True,
        artifact_refs=artifacts,
        next_action=work_item.next_action,
        human_summary=(
            f"Opportunity Scout attached {len(artifacts)} source-backed opportunity record(s)."
        ),
        audit_notes=audit_notes,
    )


def _opportunity_no_exact_match_human_summary(scout_result: OpportunityScoutResult) -> str:
    lines = [
        "Opportunity Scout found no strong exact matches under the requested hard filters.",
        "",
        "Adjacent but not exact matches:",
    ]
    candidates = [*scout_result.review_candidates, *scout_result.filtered_candidates]
    seen: set[tuple[str, str]] = set()
    rendered = 0
    for candidate in candidates:
        key = (candidate.company_name, candidate.source_url or candidate.source_title)
        if key in seen:
            continue
        seen.add(key)
        rendered += 1
        title = candidate.source_title or candidate.company_name or "Candidate"
        reasons = "; ".join(candidate.reasons[:4]) or "missing exact-match evidence"
        line = f"- {title}: {reasons}"
        if candidate.source_url:
            line += f" ({candidate.source_url})"
        lines.append(line)
        if rendered >= 5:
            break
    if rendered == 0:
        lines.append("- No adjacent source-backed candidates were strong enough to list.")
    if scout_result.constraint_relaxation_suggestion:
        lines.extend(["", "Constraint to relax next:", f"- {scout_result.constraint_relaxation_suggestion}"])
    lines.extend(
        [
            "",
            "No external writes, saves, sends, posts, or drafts were performed.",
        ]
    )
    return "\n".join(lines).strip()


def _is_strict_role_recency_search(topic: str) -> bool:
    lower = str(topic or "").lower()
    has_role = bool(
        re.search(
            r"\b(?:roles?|jobs?|positions?|postings?|openings?|chief medical officer|"
            r"medical director)\b",
            lower,
        )
    )
    has_recency = bool(
        re.search(
            r"\b(?:posted\s+in\s+the\s+last|posted\s+or\s+refreshed\s+(?:within|in)\s+the\s+last|"
            r"last\s+\d+\s+(?:hours?|days?|weeks?)|last\s+one\s+week)\b",
            lower,
        )
    )
    has_workload = bool(re.search(r"\b(?:part[- ]time|fractional|advisory|consulting)\b", lower))
    has_location = bool(
        re.search(r"\b(?:remote|u\.?s\.?|united states|us-based|u\.s\.-based)\b", lower)
    )
    return has_role and has_recency and has_workload and has_location


def _is_strict_partnership_signal_search(topic: str) -> bool:
    lower = str(topic or "").lower()
    has_signal = bool(
        re.search(
            r"\b(?:partnerships?|pilots?|buyer[- ]intent|funding\s+signals?|"
            r"validation\s+activity)\b",
            lower,
        )
    )
    has_recency = bool(re.search(r"\blast\s+\d+\s+days?\b", lower))
    has_location = bool(re.search(r"\b(?:remote|u\.?s\.?|united states|us-based)\b", lower))
    has_exclusion = "exclude" in lower
    return has_signal and has_recency and has_location and has_exclusion


def _is_source_summary_opportunity_request(work_item: WorkItem) -> bool:
    return (
        _manual_expected_artifact_type(work_item) == "source_summary"
        or _manual_task_objective(work_item) == "source_research"
    )


def _opportunity_source_summary_result(
    work_item: WorkItem,
    *,
    topic: str,
    metadata: dict[str, object],
    audit_notes: list[str],
    store: SQLiteStore | None,
) -> WorkflowRunResult:
    candidates = [
        item for item in metadata.get("retrieved_source_candidates", []) if isinstance(item, dict)
    ]
    required_terms = _manual_required_terms(work_item)
    if not candidates:
        candidates = _fixture_source_summary_candidates(
            work_item,
            topic=topic,
            required_terms=required_terms,
        )
        if candidates:
            audit_notes = [
                *audit_notes,
                (
                    "Fixture source-summary candidate generated from the request target; "
                    "enable live search for source-backed citations."
                ),
            ]
    matching = [
        candidate for candidate in candidates if _source_candidate_matches_terms(candidate, required_terms)
    ]
    if not matching:
        rejected_titles = [
            _single_line(str(candidate.get("title") or "")) for candidate in candidates[:5]
        ]
        rejected_titles = [title for title in rejected_titles if title]
        blocker = WorkItemBlocker(
            code="source_summary_target_not_found",
            message=(
                "Retrieved sources did not match the requested source-summary target"
                f" `{topic}` with required terms: {', '.join(required_terms) or 'none'}."
            ),
        )
        if rejected_titles:
            audit_notes = [
                *audit_notes,
                "Rejected retrieved source candidates: " + "; ".join(rejected_titles),
            ]
        return _blocked_result(
            work_item,
            (blocker,),
            WorkItemNextAction(
                action="rerun_source_summary_search",
                agent=WorkItemRoute.OPPORTUNITY_SCOUT,
                description=(
                    "Rerun as a source-summary retrieval task with query terms that must "
                    "match the requested event or topic before presenting results."
                ),
            ),
            store=store,
            route=WorkItemRoute.OPPORTUNITY_SCOUT,
            audit_notes=audit_notes,
        )

    sources = [_work_item_source_ref_from_search_candidate(candidate) for candidate in matching[:6]]
    source_lines = [
        f"- {_single_line(str(candidate.get('title') or 'Source'))}: "
        f"{_single_line(str(candidate.get('snippet') or ''))}"
        for candidate in matching[:4]
    ]
    summary = "\n".join(
        [
            f"Source summary for {topic}",
            "",
            _source_summary_intro(matching),
            "",
            "Matching sources:",
            *source_lines,
        ]
    )
    artifact = WorkItemArtifactRef(
        artifact_type="source_summary",
        artifact_id=f"source-summary:{work_item.id}",
        source_agent=WorkItemRoute.OPPORTUNITY_SCOUT.value,
        approval_state="approved_for_research",
        title=f"Source summary: {topic}",
        summary=summary[:500],
        metadata={
            "required_terms": required_terms,
            "matching_source_count": len(matching),
            "retrieved_source_count": len(candidates),
            "sources": matching[:6],
            "retrieval_diagnostics": metadata.get("retrieval_diagnostics"),
        },
    )
    work_item = work_item.model_copy(
        update={
            "sources": [*work_item.sources, *sources],
            "audit_notes": [*work_item.audit_notes, *audit_notes],
        }
    )
    work_item = attach_artifact(work_item, artifact)
    work_item = work_item.model_copy(
        update={
            "confidence": 0.75,
            "next_action": WorkItemNextAction(
                action="review_source_summary",
                agent=WorkItemRoute.OPPORTUNITY_SCOUT,
                description="Review the matched source-summary evidence before using it downstream.",
            ),
        }
    )
    work_item = work_item.model_copy(update={"status": derive_case_status(work_item)}).touch()
    _persist_artifact_and_event(
        work_item,
        artifact,
        summary=f"Attached source summary for {topic}.",
        store=store,
    )
    return WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.OPPORTUNITY_SCOUT,
        status=work_item.status,
        advanced=True,
        artifact_refs=[artifact],
        next_action=work_item.next_action,
        human_summary=summary,
        audit_notes=audit_notes,
    )


def _fixture_source_summary_candidates(
    work_item: WorkItem,
    *,
    topic: str,
    required_terms: list[str],
) -> list[dict[str, object]]:
    target_text = " ".join(
        str(value or "")
        for value in (
            topic,
            work_item.request_text,
            work_item.target.name,
        )
    ).lower()
    if required_terms and not all(_term_matches_text(term, target_text) for term in required_terms):
        return []
    title = _single_line(topic or work_item.target.name or "Source summary target")
    if not title:
        return []
    return [
        {
            "source_id": f"fixture:source-summary:{work_item.id}",
            "title": title,
            "url": "",
            "snippet": (
                f"Dry-run fixture source-summary candidate for {title}. This is not live "
                "source evidence; enable live search for source-backed citations."
            ),
            "source": "fixture",
            "source_type": "fixture_source_summary",
            "extraction_status": "fixture_fallback",
        }
    ]


def _source_summary_intro(candidates: list[dict[str, object]]) -> str:
    fixture_only = bool(candidates) and all(
        str(candidate.get("source_id") or "").startswith("fixture:")
        for candidate in candidates
    )
    if fixture_only:
        return (
            "I generated dry-run fixture source-summary candidates that match the requested "
            "target terms. These are research source summaries, not opportunity records, and "
            "not live source-backed evidence."
        )
    return (
        "I found source candidates that match the requested target terms. These are research "
        "source summaries, not opportunity records."
    )


def _source_candidate_matches_terms(candidate: dict[str, object], required_terms: list[str]) -> bool:
    if not required_terms:
        return True
    text = _source_candidate_text(candidate)
    return all(_term_matches_text(term, text) for term in required_terms)


def _source_candidate_text(candidate: dict[str, object]) -> str:
    values = [
        candidate.get("title"),
        candidate.get("url"),
        candidate.get("snippet"),
        candidate.get("source"),
        candidate.get("source_type"),
    ]
    return " ".join(str(value or "").lower() for value in values)


def _term_matches_text(term: str, text: str) -> bool:
    normalized = term.strip().lower()
    if not normalized:
        return True
    if normalized in text:
        return True
    synonyms = {
        "apa": ("american psychiatric association", "psychiatric association"),
    }
    return any(value in text for value in synonyms.get(normalized, ()))


def _work_item_source_ref_from_search_candidate(candidate: dict[str, object]) -> WorkItemSourceRef:
    source_id = str(candidate.get("source_id") or "")
    return WorkItemSourceRef(
        title=str(candidate.get("title") or ""),
        url=str(candidate.get("url") or ""),
        source_type=str(candidate.get("source_type") or candidate.get("source") or ""),
        source_id=source_id,
        supported_claim=str(candidate.get("snippet") or "")[:500],
        provider=_provider_from_source_id_or_type(
            source_id,
            str(candidate.get("source_type") or candidate.get("source") or ""),
        ),
        extraction_status=str(candidate.get("extraction_status") or "search_result"),
        retrieved_at=utc_now_iso(),
        key_facts=[str(candidate.get("snippet") or "")[:500]] if candidate.get("snippet") else [],
    )


def _should_run_zotero_collection_brief(work_item: WorkItem, request_text: str) -> bool:
    if work_item.target.object_type == "zotero_collection":
        return True
    text = " ".join(
        part
        for part in (request_text, work_item.request_text, work_item.target.name)
        if str(part or "").strip()
    )
    return looks_like_zotero_collection_request(text)


def _should_run_zotero_article_brief(work_item: WorkItem, request_text: str) -> bool:
    if work_item.target.object_type == "zotero_article":
        return True
    text = " ".join(
        part
        for part in (request_text, work_item.request_text, work_item.target.name)
        if str(part or "").strip()
    )
    return looks_like_zotero_article_request(text)


def _work_item_source_ref_from_company_source(source) -> WorkItemSourceRef:
    quality = getattr(source, "source_quality", None)
    quality_label = ""
    if quality is not None:
        quality_label = str(
            getattr(quality, "quality_label", "")
            or getattr(quality, "tier", "")
            or getattr(quality, "score", "")
        )
    claims = list(getattr(source, "supported_claims", []) or [])
    source_id = str(getattr(source, "source_id", "") or "")
    return WorkItemSourceRef(
        title=str(getattr(source, "title", "") or ""),
        url=str(getattr(source, "url", "") or ""),
        source_type=str(getattr(source, "source_type", "") or ""),
        source_id=source_id,
        supported_claim=claims[0] if claims else "",
        provider=_provider_from_source_id_or_type(
            source_id,
            str(getattr(source, "source_type", "") or ""),
        ),
        extraction_status="extracted" if claims else "",
        source_quality=quality_label,
        retrieved_at=utc_now_iso(),
        key_facts=claims[:5],
        zotero_key=_zotero_item_key(source_id),
    )


def _work_item_source_ref_from_opportunity_source(source) -> WorkItemSourceRef:
    quality = getattr(source, "source_quality", None)
    quality_label = ""
    if quality is not None:
        quality_label = str(
            getattr(quality, "quality_label", "")
            or getattr(quality, "tier", "")
            or getattr(quality, "score", "")
        )
    source_id = str(getattr(source, "source_id", "") or "")
    source_type = str(getattr(source, "source_type", "") or "")
    supported_signal = str(getattr(source, "supported_signal", "") or "")
    return WorkItemSourceRef(
        title=str(getattr(source, "title", "") or ""),
        url=str(getattr(source, "url", "") or ""),
        source_type=source_type,
        source_id=source_id,
        supported_claim=supported_signal,
        provider=_provider_from_source_id_or_type(source_id, source_type),
        extraction_status="opportunity_source",
        source_quality=quality_label,
        retrieved_at=utc_now_iso(),
        key_facts=[supported_signal] if supported_signal else [],
        zotero_key=_zotero_item_key(source_id),
    )


def _work_item_source_ref_from_research_source(
    source,
    *,
    supported_claim: str,
) -> WorkItemSourceRef:
    source_id = str(getattr(source, "source_id", "") or "")
    source_type = str(getattr(source, "source_type", "") or "")
    return WorkItemSourceRef(
        title=str(getattr(source, "title", "") or ""),
        url=str(getattr(source, "url", "") or ""),
        source_type=source_type,
        source_id=source_id,
        supported_claim=supported_claim,
        provider=_provider_from_source_id_or_type(source_id, source_type),
        extraction_status="source_linked",
        retrieved_at=utc_now_iso(),
        zotero_key=_zotero_item_key(source_id),
    )


def _provider_from_source_id_or_type(source_id: str, source_type: str) -> str:
    if source_id.startswith("zotero:item:"):
        return "zotero"
    if ":" in source_id:
        return source_id.split(":", maxsplit=1)[0]
    if source_type.startswith("local_zotero"):
        return "zotero"
    return source_type


def _research_brief_human_summary(brief: ResearchBrief, paragraphs: list[str]) -> str:
    lines = [
        "Business Research Analyst attached a source-backed Zotero research brief.",
        "",
        "*Summary:*",
        brief.summary,
    ]
    if brief.article_summaries:
        article = brief.article_summaries[0]
        lines.extend(
            [
                "",
                "*Requested details:*",
                (
                    "- Methods/design: "
                    f"{article.methods_or_design or 'Not available from local metadata.'}"
                ),
                ("- Inclusion/exclusion: " + _eligibility_summary_from_brief(brief)),
                f"- Relevance: {article.relevance_to_goal or 'Not available from local metadata.'}",
            ]
        )
        if article.limitations:
            lines.append(f"- Limitations: {'; '.join(article.limitations[:3])}")
    if paragraphs:
        lines.extend(["", "*Source summaries:*"])
        for index, paragraph in enumerate(paragraphs[:12]):
            lines.append(_paragraph_with_direct_source_links(paragraph, index, brief))
            lines.append("")
        if lines and lines[-1] == "":
            lines.pop()
    if brief.sources:
        lines.extend(["", "*Sources:*"])
        _append_spaced_lines(lines, _research_source_summary_entries(brief.sources[:6]))
    if brief.unknowns:
        lines.extend(["", "*Missing details:*"])
        lines.extend(f"- {item}" for item in brief.unknowns[:6])
    if brief.next_steps:
        lines.extend(["", "*Next steps:*"])
        lines.extend(f"- {item}" for item in brief.next_steps[:4])
    return "\n".join(lines)


def _append_spaced_lines(lines: list[str], items: list[str]) -> None:
    """Append list entries with blank-line separation for Slack chunk stability."""

    for item in items:
        lines.append(item)
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()


def _research_source_summary_entries(sources) -> list[str]:
    entries: list[str] = []
    for source in sources:
        title = _single_line(source.title)
        url = _single_line(source.url)
        if not title and not url:
            continue
        rows: list[str] = []
        if title:
            rows.append(f"- {title}")
        else:
            rows.append("- Source")
        if url:
            rows.append(f"  Link: {url}")
        source_id = _single_line(source.source_id)
        if source_id:
            rows.append(f"  Source ID: {source_id}")
        zotero_key = _zotero_item_key(source_id)
        if zotero_key:
            rows.append(f"  Zotero key: {zotero_key}")
        entries.append("\n".join(rows))
    return entries


def _single_line(value: str) -> str:
    return " ".join(str(value or "").split())


def _merge_research_brief_sources(
    brief: ResearchBrief,
    *,
    fallback: ResearchBrief,
) -> ResearchBrief:
    source_ids = {source.source_id for source in brief.sources}
    sources = [*brief.sources]
    for source in fallback.sources:
        if source.source_id not in source_ids:
            sources.append(source)
            source_ids.add(source.source_id)
    unknowns = list(dict.fromkeys([*brief.unknowns, *fallback.unknowns]))
    limitations = list(dict.fromkeys([*brief.limitations, *fallback.limitations]))
    return brief.model_copy(
        update={
            "sources": sources,
            "unknowns": unknowns,
            "limitations": limitations,
        }
    )


def _article_summary_lines_from_brief(brief: ResearchBrief) -> list[str]:
    lines: list[str] = []
    source_by_id = {source.source_id: source for source in brief.sources}
    for index, article in enumerate(brief.article_summaries[:8], start=1):
        parts = [article.title.rstrip(".")]
        if article.research_question:
            parts.append(article.research_question.rstrip("."))
        if article.methods_or_design:
            parts.append(f"Methods/design: {article.methods_or_design.rstrip('.')}")
        if article.key_findings:
            parts.append("Key signal: " + article.key_findings[0].rstrip("."))
        if article.limitations:
            parts.append("Limitation: " + article.limitations[0].rstrip("."))
        line = f"{index}. " + ". ".join(part for part in parts if part) + "."
        source_lines = _direct_source_link_lines(
            source_by_id[source_id] for source_id in article.source_ids if source_id in source_by_id
        )
        if source_lines:
            line = "\n".join([line, *source_lines])
        lines.append(line)
    return lines


def _paragraph_with_direct_source_links(
    paragraph: str,
    index: int,
    brief: ResearchBrief,
) -> str:
    source = brief.sources[index] if index < len(brief.sources) else None
    if source is None:
        return paragraph
    source_lines = _direct_source_link_lines((source,))
    missing_lines = [
        line
        for line in source_lines
        if _source_line_value(line) and _source_line_value(line) not in paragraph
    ]
    if not missing_lines:
        return paragraph
    return "\n".join([paragraph, *missing_lines])


def _direct_source_link_lines(sources) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for source in sources:
        if source.url and source.url not in seen:
            lines.append(f"   Source link: {source.url}")
            seen.add(source.url)
        if source.source_id and source.source_id not in seen:
            lines.append(f"   Source ID: {source.source_id}")
            seen.add(source.source_id)
        zotero_key = _zotero_item_key(source.source_id)
        if zotero_key and zotero_key not in seen:
            lines.append(f"   Zotero key: {zotero_key}")
            seen.add(zotero_key)
    return lines


def _source_line_value(line: str) -> str:
    return line.split(":", 1)[1].strip() if ":" in line else line.strip()


def _zotero_item_key(source_id: str) -> str:
    prefix = "zotero:item:"
    if source_id.startswith(prefix):
        return source_id.removeprefix(prefix).strip()
    return ""


def _eligibility_summary_from_brief(brief: ResearchBrief) -> str:
    for unknown in brief.unknowns:
        if any(marker in unknown.lower() for marker in ("inclusion", "exclusion", "eligibility")):
            return unknown
    return "Not available from local metadata."


def _advance_outreach(
    work_item: WorkItem,
    *,
    request: WorkflowRunRequest,
    store: SQLiteStore | None,
) -> WorkflowRunResult:
    if store is None:
        blocker = WorkItemBlocker(
            code="outreach_requires_saved_artifacts",
            message=(
                "Outreach drafting from WorkItems requires saved company/opportunity artifacts."
            ),
        )
        return _blocked_result(
            work_item,
            (blocker,),
            WorkItemNextAction(
                action="save_work_item_context",
                agent=WorkItemRoute.OUTREACH_COMPOSER,
                description="Save the WorkItem and its selected artifacts before drafting.",
            ),
            store=store,
            route=WorkItemRoute.OUTREACH_COMPOSER,
        )
    work_item = _attach_inline_outreach_context_for_drafting(
        work_item,
        request=request,
        store=store,
    )
    ready = drafting_ready(work_item)
    if not ready.ready:
        return _blocked_result(
            work_item,
            ready.blockers,
            ready.next_action,
            store=store,
            route=WorkItemRoute.OUTREACH_COMPOSER,
        )

    company_ref = selected_artifacts(work_item, "company_profile")[0]
    try:
        company_profile = store.load_company_profile(int(company_ref.artifact_id))
    except (KeyError, TypeError, ValueError) as exc:
        blocker = WorkItemBlocker(
            code="selected_company_profile_unavailable",
            message=f"Selected company profile could not be loaded: {company_ref.artifact_id}.",
        )
        return _blocked_result(
            work_item,
            (blocker,),
            WorkItemNextAction(
                action="select_valid_company_profile",
                agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                description="Select a saved company profile artifact before drafting.",
            ),
            store=store,
            route=WorkItemRoute.OUTREACH_COMPOSER,
            audit_notes=[str(exc)],
        )

    opportunity_record: OutreachOpportunityRecord | None = None
    opportunity_refs = selected_artifacts(work_item, "opportunity")
    if opportunity_refs and opportunity_refs[0].approval_state in {
        ApprovalState.APPROVED_FOR_DRAFTING.value,
        ApprovalState.APPROVED_FOR_EXTERNAL_USE.value,
    }:
        try:
            opportunity_record = _to_outreach_opportunity(
                store.load_opportunity_record(int(opportunity_refs[0].artifact_id))
            )
        except (KeyError, TypeError, ValueError):
            opportunity_record = None

    draft, draft_audit_note = _compose_outreach_draft_for_work_item(
        company_profile=company_profile,
        opportunity_record=opportunity_record,
        request=request,
        work_item=work_item,
    )
    draft_id = str(store.save_outreach_draft(draft))
    approval_item = build_approval_queue_item(
        draft,
        context={
            "object_type": "outreach_draft",
            "object_id": draft_id,
            "source_agent": WorkItemRoute.OUTREACH_COMPOSER.value,
            "decision": ApprovalState.PENDING.value,
            "scope": ApprovalScope.EXTERNAL_USE.value,
            "metadata": {
                "work_item_id": work_item.id,
                "company_profile_artifact_id": company_ref.artifact_id,
                "opportunity_artifact_id": opportunity_refs[0].artifact_id
                if opportunity_refs
                else "",
            },
        },
    )
    store.save_approval_item(approval_item)
    artifact = WorkItemArtifactRef(
        artifact_type="outreach_draft",
        artifact_id=draft_id,
        source_agent=WorkItemRoute.OUTREACH_COMPOSER.value,
        approval_state=ApprovalState.PENDING.value,
        title=draft.email_subject,
        summary=draft.personalization_rationale[:240],
        selected=True,
        metadata={
            "approval_queue_id": approval_item.id,
            "selected": True,
            "artifact_subtype": "email_draft",
            "canonical_draft_copy": True,
            "email_subject": draft.email_subject,
            "gmail_draft_created": False,
            "send_enabled": False,
            "external_write_performed": False,
        },
    )
    work_item = attach_artifact(work_item, artifact)
    work_item = work_item.model_copy(
        update={
            "last_agent": WorkItemRoute.OUTREACH_COMPOSER.value,
            "approval_gates": [
                *work_item.approval_gates,
                WorkItemApprovalGate(
                    scope=ApprovalScope.EXTERNAL_USE.value,
                    state=ApprovalState.PENDING.value,
                    required=True,
                    rationale="Outreach draft requires human approval before external use.",
                    approval_id=approval_item.id,
                ),
            ],
            "next_action": WorkItemNextAction(
                action="review_outreach_draft",
                agent=WorkItemRoute.OUTREACH_COMPOSER,
                description="Review the draft approval item before any external use.",
                requires_approval=True,
            ),
            "audit_notes": [
                *work_item.audit_notes,
                draft_audit_note,
            ],
        }
    )
    work_item = work_item.model_copy(update={"status": derive_case_status(work_item)}).touch()
    _persist_artifact_and_event(
        work_item,
        artifact,
        summary=f"Attached outreach draft for {company_profile.name}.",
        store=store,
    )
    return WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.OUTREACH_COMPOSER,
        status=work_item.status,
        advanced=True,
        artifact_refs=[artifact],
        next_action=work_item.next_action,
        human_summary=_format_outreach_draft_work_item_summary(
            draft,
            company_name=company_profile.name,
        ),
        audit_notes=[draft_audit_note],
    )


def _format_outreach_draft_work_item_summary(
    draft: OutreachDraft,
    *,
    company_name: str,
) -> str:
    subject = _compact_outreach_summary_text(draft.email_subject) or "Untitled draft"
    body = str(draft.email_body or "").strip()
    rationale = _compact_outreach_summary_text(draft.personalization_rationale)
    source_ids = [
        _compact_outreach_summary_text(source_id)
        for source_id in draft.source_ids_used
        if _compact_outreach_summary_text(source_id)
    ]
    lines = [
        f"Draft email for {company_name}",
        "",
        f"Subject: {subject}",
        "",
        "Body:",
        body or "No email body was generated.",
    ]
    if rationale:
        lines.extend(["", f"Rationale: {rationale}"])
    if source_ids:
        lines.extend(["", f"Source IDs used: {', '.join(source_ids)}"])
    lines.extend(
        [
            "",
            (
                "Safety: This is a draft-only Outreach Composer artifact pending human "
                "review; no external message was sent, no Gmail draft was created, and "
                "no external save/post/write was performed."
            ),
            "Next safe step: review the draft approval item before any external use.",
        ]
    )
    return "\n".join(lines).strip()


def _compact_outreach_summary_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _attach_inline_outreach_context_for_drafting(
    work_item: WorkItem,
    *,
    request: WorkflowRunRequest,
    store: SQLiteStore,
) -> WorkItem:
    if selected_artifacts(work_item, "company_profile"):
        return work_item
    if looks_like_send_side_effect(request.request_text):
        return work_item
    inline_context = _inline_outreach_context_from_request(request.request_text)
    if inline_context is None:
        return work_item

    profile = _company_profile_from_inline_outreach_context(inline_context)
    company_id = str(store.save_company(profile))
    source_url = str(inline_context.get("source_url") or "user-provided://outreach-context")
    artifact = WorkItemArtifactRef(
        artifact_type="company_profile",
        artifact_id=company_id,
        source_agent=WorkItemRoute.OUTREACH_COMPOSER.value,
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING.value,
        title=profile.name,
        summary=profile.fit_summary or profile.description,
        selected=True,
        metadata={
            "inline_natural_language_context": True,
            "source_count": len(profile.sources),
            "source_url": source_url,
            "source_refs": [source.source_id for source in profile.sources],
            "approved_for": "drafting",
            "draft_only": True,
            "send_enabled": False,
        },
    )
    metadata = {
        **work_item.target.metadata,
        "organization": profile.name,
        "inline_outreach_context": True,
        "inline_outreach_source_url": source_url,
    }
    recipient_name = str(inline_context.get("recipient_name") or "").strip()
    recipient_email = str(inline_context.get("recipient_email") or "").strip()
    if recipient_name:
        metadata["recipient_name"] = recipient_name
    if recipient_email:
        metadata["recipient_email"] = recipient_email
    target = WorkItemTarget(
        name=profile.name,
        url=profile.website or work_item.target.url,
        email=recipient_email or work_item.target.email,
        object_type=work_item.target.object_type or "company",
        external_id=work_item.target.external_id,
        metadata=metadata,
    )
    updated = attach_artifact(work_item.model_copy(update={"target": target}), artifact)
    updated = updated.model_copy(
        update={
            "audit_notes": [
                *updated.audit_notes,
                (
                    "Inline source-backed natural-language context was converted into "
                    "a draft-only company profile approved for drafting; no send action "
                    "was enabled."
                ),
            ],
        }
    ).touch()
    _persist_artifact_and_event(
        updated,
        artifact,
        summary=f"Attached inline outreach context for {profile.name}.",
        store=store,
    )
    record_event(
        updated,
        event_type="inline_outreach_context_attached",
        summary="Natural-language outreach context prepared for draft-only use.",
        metadata={
            "company": profile.name,
            "source_url": source_url,
            "fact_count": len(inline_context.get("facts") or []),
            "draft_only": True,
            "send_enabled": False,
        },
        store=store,
    )
    return updated


def _inline_outreach_context_from_request(request_text: str) -> dict[str, Any] | None:
    text = " ".join(str(request_text or "").split())
    if not re.search(r"\b(?:draft|write|compose|prepare)\b", text, flags=re.I):
        return None
    if not re.search(r"\b(?:outreach|email|linkedin|message|note)\b", text, flags=re.I):
        return None
    if not re.search(
        r"\b(?:approved|source-backed|source backed|sources?|facts?|context)\b",
        text,
        flags=re.I,
    ):
        return None

    company = _extract_inline_outreach_value(
        text,
        (
            "Company",
            "Target company",
            "Target organization",
            "Organization",
            "Recipient organization",
            "To company",
        ),
    )
    if not company:
        company = _extract_outreach_company_from_request(text)
    company = _clean_inline_outreach_company(company)
    if not company:
        return None

    source_url = _extract_inline_source_url(text)
    source_label = _extract_inline_outreach_value(text, ("Source", "Sources", "Citation"))
    facts = _extract_inline_outreach_facts(text)
    if not facts:
        return None
    has_source_basis = bool(source_url or source_label) or bool(
        re.search(r"\b(?:approved|source-backed|source backed)\b", text, flags=re.I)
    )
    if not has_source_basis:
        return None
    recipient_name = _extract_inline_outreach_value(text, ("Recipient", "Contact"))
    recipient_email = ""
    if recipient_name and "@" in recipient_name:
        _, recipient_email = parseaddr(recipient_name)
    return {
        "company": company,
        "source_url": source_url or "user-provided://outreach-context",
        "source_label": source_label or "User-provided approved outreach context",
        "facts": facts,
        "recipient_name": recipient_name,
        "recipient_email": recipient_email,
    }


def _extract_inline_outreach_value(text: str, labels: tuple[str, ...]) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    stop_labels = (
        "Company|Target company|Target organization|Organization|Recipient organization|"
        "To company|Recipient|Contact|Source|Sources|Citation|Facts|Approved facts|"
        "Source-backed facts|Source backed facts|Context|Goal|Ask|Body"
    )
    pattern = re.compile(
        rf"\b(?:{label_pattern})\s*:\s*(?P<value>.*?)(?=\s+\b(?:{stop_labels})\s*:|$)",
        flags=re.I,
    )
    match = pattern.search(text)
    return match.group("value").strip(" .;") if match else ""


def _extract_outreach_company_from_request(text: str) -> str:
    patterns = (
        r"\b(?:to|for)\s+(?P<company>[A-Z][A-Za-z0-9&.,' -]{1,80}?)(?=\s+(?:using|based|with|from|about|that|who|and|\.|,|;|$))",
        r"\b(?:company|organization)\s+(?P<company>[A-Z][A-Za-z0-9&.,' -]{1,80}?)(?=\s+(?:using|based|with|from|about|that|who|and|\.|,|;|$))",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group("company")
    return ""


def _clean_inline_outreach_company(company: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(company or "")).strip(" .;,:")
    cleaned = re.sub(
        r"\b(?:using|based on|with|from|about|do not|draft-only|draft only)\b[\s\S]*$",
        "",
        cleaned,
        flags=re.I,
    ).strip(" .;,:")
    return cleaned[:120]


def _extract_inline_source_url(text: str) -> str:
    match = re.search(r"\bhttps?://[^\s,;)]+", text)
    if match:
        return match.group(0).rstrip(".,;)>")
    return ""


def _extract_inline_outreach_facts(text: str) -> list[str]:
    facts_block = _extract_inline_outreach_value(
        text,
        (
            "Approved facts",
            "Source-backed facts",
            "Source backed facts",
            "Facts",
            "Context",
        ),
    )
    if not facts_block:
        return []
    facts_block = re.sub(
        r"\b(?:Do not|Don't|Dont|Never)\s+[\s\S]*$",
        "",
        facts_block,
        flags=re.I,
    ).strip()
    raw_facts = re.split(r"\s*(?:;|\n| \d+[\).] | - )\s*", facts_block)
    facts = []
    for raw_fact in raw_facts:
        fact = raw_fact.strip(" .;")
        if len(fact) < 12:
            continue
        facts.append(fact[:360])
    return list(dict.fromkeys(facts))[:6]


def _company_profile_from_inline_outreach_context(context: dict[str, Any]) -> CompanyProfile:
    company = str(context["company"])
    facts = [str(fact) for fact in context.get("facts") or []]
    source_url = str(context.get("source_url") or "user-provided://outreach-context")
    source = SourceRecord(
        source_id="user_provided_outreach_context",
        title=str(context.get("source_label") or "User-provided approved outreach context")[:160],
        url=source_url,
        source_type="user_provided",
        supported_claims=facts,
        confidence=0.7,
    )
    description = facts[0] if facts else f"User-provided outreach context for {company}."
    fit_summary = next(
        (
            fact
            for fact in facts
            if re.search(
                r"\b(?:keystone|advisory|evaluation|evidence|clinical|behavioral|workflow)\b",
                fact,
                flags=re.I,
            )
        ),
        description,
    )
    return CompanyProfile(
        name=company,
        website=source_url if source_url.startswith("http") else None,
        description=description,
        fit_summary=fit_summary,
        behavioral_health_relevance=70,
        clinical_ai_relevance=60,
        evidence_generation_need=70,
        outside_consulting_likelihood=60,
        consulting_fit_score=65,
        confidence_score=0.7,
        confidence_explanation=(
            "Built from operator-provided source-backed context for draft-only outreach."
        ),
        sources=[source],
        risks=[
            "Context was provided in the operator request; verify source details before external use."
        ],
        missing_information=[
            "No live contact channel was selected.",
            "No external-use approval has been granted.",
        ],
    )


def _compose_outreach_draft_for_work_item(
    *,
    company_profile: Any,
    opportunity_record: OutreachOpportunityRecord | None,
    request: WorkflowRunRequest,
    work_item: WorkItem,
) -> tuple[Any, str]:
    objective = _outreach_goal(request.request_text)
    if not request.live_sdk:
        return (
            compose_outreach_draft_fixture(
                company_profile=company_profile,
                opportunity_record=opportunity_record,
                outreach_goal=objective,
            ),
            "Draft-only outreach artifact created; no send or live side effects occurred.",
        )

    try:
        style_profile = load_style_profile("sample_email_style_profile_anup_approved")
        approved_context = build_approved_outreach_drafting_context(
            company_profile=company_profile,
            opportunity_record=opportunity_record,
            email_style_profile=style_profile,
            objective=objective,
            revision_request=request.request_text,
        )

        def retrieve() -> dict[str, Any]:
            return {
                "company_profile": company_profile,
                "opportunity_record": opportunity_record,
                "email_style_profile": style_profile,
                "approved_context": approved_context,
                "orchestrator_context": _specialist_orchestrator_context_payload(
                    request,
                    work_item,
                ),
                "context_policy": "Approved source-backed WorkItem context only.",
            }

        def normalize(context: dict[str, Any]) -> OutreachComposerSDKInput:
            return OutreachComposerSDKInput(
                company_name=company_profile.name,
                recent_signal=opportunity_record.rationale if opportunity_record else "",
                outreach_goal=objective,
                approved_context=(
                    _specialist_orchestrator_context_text(request, work_item)
                    + "\n\n"
                    "Approved source-backed WorkItem outreach context:\n"
                    f"{json.dumps(jsonable(context), ensure_ascii=True, sort_keys=True)}"
                ),
                email_style_profile=json.dumps(
                    jsonable(style_profile),
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            )

        outcome = run_retrieved_sdk_synthesis(
            agent=build_outreach_composer_compact_synthesis_agent(),
            output_type=OutreachLLMDraftPayload,
            retrieve=retrieve,
            normalize=normalize,
            input_summary=f"WorkItem outreach SDK synthesis for {company_profile.name}",
            input_audit_payload={
                "company": company_profile.name,
                "sdk_synthesis": True,
                "workflow": "work_item_outreach_composer",
            },
            live=True,
            save=False,
            model_label="sdk-live",
        )
        compact_payload = jsonable(outcome.final_output)
        if not isinstance(compact_payload, dict):
            raise RuntimeError("Outreach SDK synthesis did not return a JSON object.")
        source_ids_used = compact_payload.get("source_ids_used")
        if isinstance(source_ids_used, list) and "keystone_profile" not in source_ids_used:
            compact_payload["source_ids_used"] = [*source_ids_used, "keystone_profile"]
        draft = compose_outreach_draft_llm_constrained(
            approved_context=approved_context,
            llm_draft_payload=compact_payload,
            fallback_to_fixture=False,
        )
        return (
            draft.model_copy(update={"drafting_mode": "llm_constrained"}),
            "Outreach Composer live SDK draft created; no send or live Gmail side effect occurred.",
        )
    except Exception as exc:
        return (
            compose_outreach_draft_fixture(
                company_profile=company_profile,
                opportunity_record=opportunity_record,
                outreach_goal=objective,
            ),
            (
                "Outreach Composer live SDK drafting failed with "
                f"{type(exc).__name__}; deterministic draft-only fallback created."
            ),
        )


def _block_unsupported_route(
    work_item: WorkItem,
    *,
    route: WorkItemRoute,
    store: SQLiteStore | None,
) -> WorkflowRunResult:
    if route == WorkItemRoute.OUTREACH_COMPOSER:
        ready = drafting_ready(work_item)
        return _blocked_result(
            work_item,
            ready.blockers,
            ready.next_action,
            store=store,
            route=route,
        )
    blocker = WorkItemBlocker(
        code="route_not_supported_in_workitem_phase",
        message=(
            "WorkItem Phase II supports Business Research Analyst and Opportunity "
            f"Scout only; {route.value} is not enabled here."
        ),
    )
    return _blocked_result(
        work_item,
        (blocker,),
        WorkItemNextAction(
            action="use_existing_agent_path",
            agent=route,
            description=(
                "Use the existing specialist path until this route is enabled for WorkItems."
            ),
        ),
        store=store,
        route=route,
    )


def _blocked_result(
    work_item: WorkItem,
    blockers: tuple[WorkItemBlocker, ...],
    next_action: WorkItemNextAction | None,
    *,
    store: SQLiteStore | None,
    route: WorkItemRoute | None = None,
    audit_notes: list[str] | None = None,
) -> WorkflowRunResult:
    updated = work_item
    for blocker in blockers:
        updated = add_blocker(updated, blocker)
    updated = set_next_action(updated, next_action)
    updated = updated.model_copy(
        update={
            "status": derive_case_status(updated),
            "audit_notes": [*updated.audit_notes, *(audit_notes or [])],
        }
    ).touch()
    record_event(
        updated,
        event_type="advance_blocked",
        summary=blockers[0].message if blockers else "WorkItem advance blocked.",
        metadata={"blockers": [blocker.model_dump(mode="json") for blocker in blockers]},
        store=store,
    )
    return WorkflowRunResult(
        work_item=updated,
        route=route or updated.current_route,
        status=updated.status,
        advanced=False,
        blockers=list(blockers),
        next_action=next_action,
        human_summary=_blocked_human_summary(route or updated.current_route, blockers),
        audit_notes=audit_notes or [],
    )


def _blocked_human_summary(
    route: WorkItemRoute,
    blockers: tuple[WorkItemBlocker, ...],
) -> str:
    if not blockers:
        return "WorkItem advance blocked."
    requirements = [blocker.message.strip() for blocker in blockers if blocker.message.strip()]
    if not requirements:
        return "WorkItem advance blocked."
    agent_name = {
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST: "Business Research Analyst",
        WorkItemRoute.OPPORTUNITY_SCOUT: "Opportunity Scout",
        WorkItemRoute.OUTREACH_COMPOSER: "Outreach Composer",
        WorkItemRoute.GMAIL_TRIAGE: "Gmail Triage",
        WorkItemRoute.CHIEF_OF_STAFF: "Chief of Staff",
        WorkItemRoute.ORCHESTRATOR: "Orchestrator",
        WorkItemRoute.CLARIFICATION: "Orchestrator",
    }.get(route, route.value)
    return f"{agent_name} did not have the required context to complete synthesis: " + "; ".join(
        dict.fromkeys(requirements)
    )


def _persist_artifact_and_event(
    work_item: WorkItem,
    artifact: WorkItemArtifactRef,
    *,
    summary: str,
    store: SQLiteStore | None,
) -> None:
    if store is None:
        return
    store.save_work_item_artifact(work_item.id, artifact)
    record_event(
        work_item,
        event_type="artifact_attached",
        summary=summary,
        metadata={"artifact": artifact.model_dump(mode="json")},
        store=store,
    )


def _persist_retrieval_tool_memory(
    metadata: dict[str, object],
    *,
    object_id: str,
    store: SQLiteStore | None,
) -> int | None:
    if store is None or not metadata:
        return None
    item = retrieval_tool_performance_memory_item(dict(metadata), object_id=object_id)
    if item is None:
        return None
    return int(store.save_memory_item(item))


def _persist_manager_loop_efficiency_memory(
    metrics: dict[str, object],
    *,
    object_id: str,
    store: SQLiteStore | None,
) -> int | None:
    if store is None or not metrics:
        return None
    item = manager_loop_efficiency_memory_item(dict(metrics), object_id=object_id)
    if item is None:
        return None
    return int(store.save_memory_item(item))


def _looks_like_outreach_request(text: str) -> bool:
    cleaned = str(text or "")
    if re.search(r"\boutreach\s+composer\b", cleaned, flags=re.I):
        return True
    return bool(
        re.search(
            r"\b(?:draft|write|compose|prepare)\b[\s\S]{0,120}"
            r"\b(?:draft[- ]only\s+)?(?:outreach|email|linkedin|message|note)\b"
            r"|\b(?:outreach|email|linkedin|message|note)\b[\s\S]{0,80}"
            r"\b(?:draft|version)\b",
            cleaned,
            flags=re.I,
        )
    )


def _to_outreach_opportunity(record: ScoutOpportunityRecord) -> OutreachOpportunityRecord:
    source = record.sources[0] if record.sources else None
    return OutreachOpportunityRecord(
        company_name=record.company_name,
        title=f"{record.opportunity_type} opportunity",
        source=source.url if source else "fixture",
        source_id=source.source_id if source else "fixture:work-item-opportunity",
        notes=record.why_now_signal,
        score=record.priority_score / 100,
        rationale=record.keystone_fit_reason,
        next_step=record.recommended_next_step,
        claims=record.claims,
        unsupported_claims_flagged=record.unsupported_claims_flagged,
        approved_for_outreach=True,
    )


def _outreach_goal(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    if not cleaned or cleaned.lower() in {"continue", "resume"}:
        return "compare notes on clinical AI evaluation and research operations"
    return cleaned[:180]
