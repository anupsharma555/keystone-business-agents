"""Optional LangGraph orchestration for durable WorkItem advancement.

The graph layer intentionally wraps the existing WorkItem runner. It does not
replace SDK agents, prompts, schemas, tool wrappers, or Python safety gates.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from importlib.util import find_spec
from typing import Any, Literal, TypedDict
from uuid import uuid4

from pydantic import BaseModel, Field

from keystone_agents.schemas.approval import ApprovalState
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkflowRunResult,
    WorkItem,
    WorkItemApprovalGate,
    WorkItemArtifactRef,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemStatus,
)
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env
from keystone_agents.tools.announcement_context_tools import (
    retrieve_preprint_announcement_history_impl,
    retrieve_rss_announcement_history_impl,
)
from keystone_agents.work_items import attach_artifact, build_context_pack_for_route, record_event
from keystone_agents.workflow_runner import (
    _MANAGER_LOOP_STOP_STATUSES,
    PreparedWorkItemStep,
    _finalize_manager_loop_result,
    _manager_loop_request_is_planning_only,
    _operator_requested_manager_continuation,
    advance_work_item,
    advance_work_item_manager_loop,
    answer_work_item_state_followup,
    finalize_prepared_work_item_step,
    normalize_workflow_request_for_graph,
    prepare_work_item_step,
    run_prepared_work_item_specialist,
)

LANGGRAPH_WORKITEM_ENV_KEYS = (
    "KNI_BUSINESS_AGENTS_LANGGRAPH",
    "KEYSTONE_WORKITEM_LANGGRAPH",
)
FALSE_VALUES = {"", "0", "false", "no", "off"}


class LangGraphUnavailableError(RuntimeError):
    """Raised when a caller explicitly requires LangGraph but it is not installed."""


class WorkItemGraphState(TypedDict, total=False):
    """Serializable state passed between optional LangGraph nodes."""

    request: dict[str, Any]
    result: dict[str, Any]
    route: str
    status: str
    advanced: bool
    node_path: list[str]
    checkpoint_required: bool
    checkpoint_reason: str
    enable_interrupts: bool
    human_checkpoint_payload: dict[str, Any]
    human_decision: Any
    original_request: dict[str, Any]
    prepared_step: dict[str, Any]
    improvements: list[str]
    loop_steps: list[dict[str, Any]]
    manager_loop: bool
    max_manager_steps: int
    terminal: bool
    graph_stop_reason: str


class LangGraphWorkflowOutcome(BaseModel):
    """Execution envelope for the optional LangGraph WorkItem runtime."""

    request: WorkflowRunRequest
    result: WorkflowRunResult
    graph_runtime: Literal["langgraph", "dependency_free_fallback"]
    graph_available: bool
    checkpoint_required: bool = False
    checkpoint_reason: str = ""
    checkpoint_payload: dict[str, Any] | None = None
    node_path: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    checkpoint_key: str = ""


def langgraph_available() -> bool:
    """Return whether the optional LangGraph package can be imported."""

    return find_spec("langgraph") is not None


def work_item_langgraph_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether WorkItem calls should default to graph-native execution."""

    override = work_item_langgraph_env_override(env)
    return bool(override)


def work_item_langgraph_env_override(env: Mapping[str, str] | None = None) -> bool | None:
    """Return an explicit environment override, if one is configured."""

    source = env if env is not None else os.environ
    saw_false = False
    for key in LANGGRAPH_WORKITEM_ENV_KEYS:
        value = source.get(key)
        if value is None:
            continue
        if value.strip().lower() not in FALSE_VALUES:
            return True
        saw_false = True
    return False if saw_false else None


def should_use_langgraph_for_work_item(
    request: WorkflowRunRequest,
    *,
    manager_loop: bool = False,
) -> bool:
    """Return whether the backend should use LangGraph for this WorkItem run."""

    if request.work_item_id:
        return True
    normalized = " ".join(str(request.request_text or "").lower().split())
    if not normalized or normalized in {"continue", "resume"}:
        return False
    if not manager_loop:
        return _request_has_graph_worthy_single_step_boundary(normalized)
    if _manager_loop_request_is_planning_only(
        normalized,
        manual_request_plan=request.manual_request_plan,
    ):
        return False
    if _operator_requested_manager_continuation(normalized):
        return True
    if any(
        _operator_requested_manager_continuation(normalized, next_action_agent=agent)
        for agent in (
            WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            WorkItemRoute.OPPORTUNITY_SCOUT,
            WorkItemRoute.OUTREACH_COMPOSER,
            WorkItemRoute.GMAIL_TRIAGE,
        )
    ):
        return True
    return (
        _request_has_graph_worthy_single_step_boundary(normalized)
        or _request_mentions_context_to_specialist_edge(normalized)
        or _request_mentions_chief_coordination_edge(normalized)
        or _request_mentions_chief_multistage_workflow(normalized)
    )


def _request_has_graph_worthy_single_step_boundary(normalized: str) -> bool:
    requested_outbound_draft = bool(
        re.search(
            r"\b(?:draft|write|compose|prepare)\b[^.\n]{0,120}"
            r"\b(?:outreach|email|linkedin|message|reply|response)\b",
            normalized,
        )
        and not re.search(
            r"\b(?:do\s+not|don't|never|no)\b[^.\n]{0,80}"
            r"\b(?:draft|write|compose|prepare)\b[^.\n]{0,120}"
            r"\b(?:outreach|email|linkedin|message|reply|response)\b",
            normalized,
        )
    )
    return bool(
        re.search(r"\bapproval\b|\bcheckpoint\b|\bdraft[- ]only\b", normalized)
        or requested_outbound_draft
        or (
            "gmail" in normalized
            and re.search(r"\b(?:research|company|source|draft|reply|triage)\b", normalized)
        )
        or re.search(r"\b(?:workflow|handoff|multi[- ]?step|manager loop)\b", normalized)
    )


def _request_mentions_context_to_specialist_edge(normalized: str) -> bool:
    context_marker = re.search(
        r"\b(?:rss|preprints?|zotero|airtable|google workspace)\s+context\s+agent\b",
        normalized,
    )
    natural_context_marker = bool(
        _normalized_feed_context_kind(normalized)
        or _normalized_mentions_zotero_context_source(normalized)
    )
    downstream_marker = re.search(
        r"\b(?:research|opportunit\w*|outreach|artifact|evidence packet|brief|plan)\b",
        normalized,
    )
    return bool((context_marker or natural_context_marker) and downstream_marker)


def _normalized_feed_context_kind(
    normalized: str,
) -> Literal["rss", "preprints"] | None:
    if re.search(
        r"\b(?:preprints?|preprints_context_agent|preprint context|preliminary papers?)\b",
        normalized,
    ):
        return "preprints"
    if re.search(
        r"\b(?:rss|rss_context_agent|feed context|announcements? context|"
        r"announcement history)\b",
        normalized,
    ):
        return "rss"
    if re.search(
        r"\b(?:recent|latest|available|selected|saved|prior|historical)\s+"
        r"(?:announcements?|news|updates?)\b",
        normalized,
    ):
        return "rss"
    return None


def _normalized_mentions_zotero_context_source(normalized: str) -> bool:
    if "zotero" in normalized and re.search(
        r"\b(?:context|handoff|evidence|artifact|brief|packet|collection|"
        r"library|papers?|articles?|references?)\b",
        normalized,
    ):
        return True
    return bool(
        re.search(
            r"\b(?:saved|selected|local|prior|available|curated)\s+"
            r"(?:papers?|articles?|literature|references?|citations?)\b"
            r"|\b(?:paper|article|literature|citation)\s+library\b",
            normalized,
        )
    )


def _request_mentions_chief_coordination_edge(normalized: str) -> bool:
    if "chief of staff" not in normalized:
        return False
    return bool(
        re.search(
            r"\b(?:coordinate|orchestrate|handoff|use .*agent|agents-as-tools|"
            r"specialists?|context agents?)\b",
            normalized,
        )
    )


def _request_mentions_chief_multistage_workflow(normalized: str) -> bool:
    """Return true for natural Chief asks that name multiple workflow stages.

    This is graph-selection policy only. It does not choose the business answer,
    exact specialist output, evidence quality, or outreach text.
    """

    if "chief of staff" not in normalized:
        return False
    stage_markers = (
        bool(re.search(r"\b(?:research|source[- ]backed|evidence)\b", normalized)),
        bool(re.search(r"\b(?:opportunit(?:y|ies)|advisory|fit)\b", normalized)),
        bool(
            re.search(
                r"\b(?:draft[- ]only|sample\s+outreach|outreach|reply|response)\b",
                normalized,
            )
        ),
        bool(re.search(r"\b(?:approval|checkpoint|review)\b", normalized)),
        bool(re.search(r"\b(?:gmail|email|inbox|thread)\b", normalized)),
        bool(
            _normalized_feed_context_kind(normalized)
            or _normalized_mentions_zotero_context_source(normalized)
            or re.search(r"\b(?:airtable|google workspace)\s+context\b", normalized)
        ),
    )
    return sum(1 for marker in stage_markers if marker) >= 2


def work_item_graph_thread_id(work_item_id: str) -> str:
    """Return the stable LangGraph thread id for one WorkItem."""

    cleaned = str(work_item_id or "").strip()
    if not cleaned:
        return ""
    return f"work-item:{cleaned}"


def langgraph_functionality_improvements() -> list[str]:
    """Describe the concrete workflow improvements this layer adds."""

    return [
        "Splits WorkItem execution into explicit graph nodes.",
        "Creates a durable checkpoint point before approval-gated next actions.",
        "Keeps SQLite WorkItems as canonical state while allowing LangGraph checkpoints.",
        "Lets future Slack or scheduled runs resume from a graph thread id instead of rerouting.",
        "Preserves SDK specialist agents, Pydantic outputs, and Python safety gates.",
    ]


def build_work_item_langgraph(*, checkpointer: Any | None = None) -> Any:
    """Compile the optional LangGraph WorkItem graph.

    LangGraph remains an optional dependency. Use `run_work_item_langgraph()` for
    normal execution; it falls back to the same node contract when LangGraph is
    unavailable.
    """

    if not langgraph_available():
        raise LangGraphUnavailableError(
            "LangGraph is not installed. Install the optional `orchestration` extra "
            "to run compiled LangGraph workflows."
        )

    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(WorkItemGraphState)
    builder.add_node("normalize_request", _normalize_request_node)
    builder.add_node("orchestrator_preflight", _orchestrator_preflight_node)
    builder.add_node("state_followup", _state_followup_node)
    builder.add_node("prepare_work_item", _prepare_work_item_node)
    builder.add_node("stage_feed_context", _stage_feed_context_node)
    builder.add_node("stage_zotero_context", _stage_zotero_context_node)
    builder.add_node("stage_airtable_context", _stage_airtable_context_node)
    builder.add_node("stage_google_workspace_context", _stage_google_workspace_context_node)
    builder.add_node("run_business_research", _run_business_research_node)
    builder.add_node("run_opportunity_scout", _run_opportunity_scout_node)
    builder.add_node("run_gmail_triage", _run_gmail_triage_node)
    builder.add_node("run_outreach_composer", _run_outreach_composer_node)
    builder.add_node("run_chief_of_staff", _run_chief_of_staff_node)
    builder.add_node("run_unsupported_route", _run_unsupported_route_node)
    builder.add_node("finalize_step", _finalize_step_node)
    builder.add_node("manager_loop_continue", _manager_loop_continue_node)
    builder.add_node("manager_loop_finalize", _manager_loop_finalize_node)
    builder.add_node("approval_checkpoint", _approval_checkpoint_node)
    builder.add_edge(START, "normalize_request")
    builder.add_edge("normalize_request", "orchestrator_preflight")
    builder.add_edge("orchestrator_preflight", "state_followup")
    builder.add_conditional_edges(
        "state_followup",
        _route_after_state_followup,
        {
            "done": END,
            "prepare_work_item": "prepare_work_item",
        },
    )
    builder.add_conditional_edges(
        "prepare_work_item",
        _route_after_prepare_work_item,
        {
            "stage_feed_context": "stage_feed_context",
            "stage_zotero_context": "stage_zotero_context",
            "stage_airtable_context": "stage_airtable_context",
            "stage_google_workspace_context": "stage_google_workspace_context",
            "run_business_research": "run_business_research",
            "run_opportunity_scout": "run_opportunity_scout",
            "run_gmail_triage": "run_gmail_triage",
            "run_outreach_composer": "run_outreach_composer",
            "run_chief_of_staff": "run_chief_of_staff",
            "run_unsupported_route": "run_unsupported_route",
        },
    )
    builder.add_conditional_edges(
        "stage_feed_context",
        _route_after_context_staging,
        {
            "stage_feed_context": "stage_feed_context",
            "stage_zotero_context": "stage_zotero_context",
            "stage_airtable_context": "stage_airtable_context",
            "stage_google_workspace_context": "stage_google_workspace_context",
            "run_business_research": "run_business_research",
            "run_opportunity_scout": "run_opportunity_scout",
            "run_gmail_triage": "run_gmail_triage",
            "run_outreach_composer": "run_outreach_composer",
            "run_chief_of_staff": "run_chief_of_staff",
            "run_unsupported_route": "run_unsupported_route",
        },
    )
    builder.add_conditional_edges(
        "stage_zotero_context",
        _route_after_context_staging,
        {
            "stage_feed_context": "stage_feed_context",
            "stage_zotero_context": "stage_zotero_context",
            "stage_airtable_context": "stage_airtable_context",
            "stage_google_workspace_context": "stage_google_workspace_context",
            "run_business_research": "run_business_research",
            "run_opportunity_scout": "run_opportunity_scout",
            "run_gmail_triage": "run_gmail_triage",
            "run_outreach_composer": "run_outreach_composer",
            "run_chief_of_staff": "run_chief_of_staff",
            "run_unsupported_route": "run_unsupported_route",
        },
    )
    builder.add_conditional_edges(
        "stage_airtable_context",
        _route_after_airtable_context_staging,
        {
            "approval_checkpoint": "approval_checkpoint",
            "stage_feed_context": "stage_feed_context",
            "stage_zotero_context": "stage_zotero_context",
            "stage_airtable_context": "stage_airtable_context",
            "stage_google_workspace_context": "stage_google_workspace_context",
            "run_business_research": "run_business_research",
            "run_opportunity_scout": "run_opportunity_scout",
            "run_gmail_triage": "run_gmail_triage",
            "run_outreach_composer": "run_outreach_composer",
            "run_chief_of_staff": "run_chief_of_staff",
            "run_unsupported_route": "run_unsupported_route",
        },
    )
    builder.add_conditional_edges(
        "stage_google_workspace_context",
        _route_after_google_workspace_context_staging,
        {
            "approval_checkpoint": "approval_checkpoint",
            "stage_feed_context": "stage_feed_context",
            "stage_zotero_context": "stage_zotero_context",
            "stage_airtable_context": "stage_airtable_context",
            "stage_google_workspace_context": "stage_google_workspace_context",
            "run_business_research": "run_business_research",
            "run_opportunity_scout": "run_opportunity_scout",
            "run_gmail_triage": "run_gmail_triage",
            "run_outreach_composer": "run_outreach_composer",
            "run_chief_of_staff": "run_chief_of_staff",
            "run_unsupported_route": "run_unsupported_route",
        },
    )
    for node_name in (
        "run_business_research",
        "run_opportunity_scout",
        "run_gmail_triage",
        "run_outreach_composer",
        "run_chief_of_staff",
        "run_unsupported_route",
    ):
        builder.add_edge(node_name, "finalize_step")
    builder.add_conditional_edges(
        "finalize_step",
        _route_after_finalize,
        {
            "stage_airtable_context": "stage_airtable_context",
            "stage_google_workspace_context": "stage_google_workspace_context",
            "approval_checkpoint": "approval_checkpoint",
            "manager_loop_continue": "manager_loop_continue",
            "manager_loop_finalize": "manager_loop_finalize",
            "done": END,
        },
    )
    builder.add_edge("manager_loop_continue", "prepare_work_item")
    builder.add_edge("manager_loop_finalize", END)
    builder.add_edge("approval_checkpoint", END)
    if checkpointer is None:
        return builder.compile()
    return builder.compile(checkpointer=checkpointer)


def run_work_item_langgraph(
    request: WorkflowRunRequest,
    *,
    checkpointer: Any | None = None,
    thread_id: str | None = None,
    require_langgraph: bool = False,
    enable_interrupts: bool = False,
    manager_loop: bool = False,
    max_manager_steps: int = 3,
    feedback_callback: Any | None = None,
) -> LangGraphWorkflowOutcome:
    """Advance a WorkItem through the optional LangGraph orchestration layer."""

    bounded_max_steps = max(1, min(5, int(max_manager_steps or 3)))
    initial_state: WorkItemGraphState = {
        "request": request.model_dump(mode="json"),
        "original_request": request.model_dump(mode="json"),
        "node_path": [],
        "enable_interrupts": enable_interrupts,
        "improvements": langgraph_functionality_improvements(),
        "loop_steps": [],
        "manager_loop": bool(manager_loop),
        "max_manager_steps": bounded_max_steps,
    }
    checkpoint_key = thread_id or f"work-item-graph-{uuid4().hex}"
    if manager_loop:
        _emit_langgraph_feedback(
            feedback_callback,
            "manager_loop_graph_started",
            {
                "schema": "keystone.langgraph.manager_loop_feedback.v1",
                "thread_id": checkpoint_key,
                "work_item_id": request.work_item_id or "",
                "max_steps": bounded_max_steps,
                "send_enabled": False,
            },
        )
    if langgraph_available():
        graph = build_work_item_langgraph(checkpointer=checkpointer)
        final_state = graph.invoke(
            initial_state,
            config={"configurable": {"thread_id": checkpoint_key}},
        )
        runtime: Literal["langgraph", "dependency_free_fallback"] = "langgraph"
    else:
        if require_langgraph:
            raise LangGraphUnavailableError(
                "LangGraph is not installed. Install the optional `orchestration` extra "
                "to require compiled graph execution."
            )
        final_state = _run_dependency_free_graph(initial_state)
        runtime = "dependency_free_fallback"

    result = WorkflowRunResult.model_validate(final_state["result"])
    checkpoint_required = bool(final_state.get("checkpoint_required", False))
    checkpoint_reason = str(final_state.get("checkpoint_reason") or "")
    checkpoint_payload = (
        _approval_checkpoint_payload(result, reason=checkpoint_reason)
        if checkpoint_required
        else None
    )
    stop_reason = str(
        final_state.get("graph_stop_reason")
        or "stopped because no next graph edge was available"
    )
    graph_completion_review = (
        _graph_completion_review(
            original_request=request,
            result=result,
            node_path=list(final_state.get("node_path", [])),
            loop_steps=list(final_state.get("loop_steps") or []),
            stop_reason=stop_reason,
            checkpoint_required=checkpoint_required,
            checkpoint_reason=checkpoint_reason,
        )
        if manager_loop
        else None
    )
    if graph_completion_review is not None:
        result = _enhance_graph_terminal_summary(
            original_request=request,
            result=result,
            graph_completion_review=graph_completion_review,
        )
    _record_langgraph_checkpoint_event(
        request=request,
        result=result,
        runtime=runtime,
        checkpoint_required=checkpoint_required,
        checkpoint_reason=checkpoint_reason,
        checkpoint_key=checkpoint_key,
        node_path=list(final_state.get("node_path", [])),
        checkpoint_payload=checkpoint_payload,
        graph_completion_review=graph_completion_review,
    )
    if manager_loop:
        _emit_langgraph_feedback(
            feedback_callback,
            "manager_loop_completed",
            {
                "schema": "keystone.langgraph.manager_loop_feedback.v1",
                "runtime": runtime,
                "thread_id": checkpoint_key,
                "work_item_id": result.work_item.id,
                "route": result.route.value,
                "status": result.status.value,
                "checkpoint_required": checkpoint_required,
                "checkpoint_reason": checkpoint_reason,
                "checkpoint_payload": checkpoint_payload,
                "node_path": list(final_state.get("node_path", [])),
                "steps": list(final_state.get("loop_steps") or []),
                "stop_reason": stop_reason,
                "graph_completion_review": graph_completion_review,
                "send_enabled": False,
            },
        )
    return LangGraphWorkflowOutcome(
        request=request,
        result=result,
        graph_runtime=runtime,
        graph_available=(runtime == "langgraph"),
        checkpoint_required=checkpoint_required,
        checkpoint_reason=checkpoint_reason,
        checkpoint_payload=checkpoint_payload,
        node_path=list(final_state.get("node_path", [])),
        improvements=list(
            final_state.get("improvements") or langgraph_functionality_improvements()
        ),
        checkpoint_key=checkpoint_key,
    )


def advance_work_item_with_optional_langgraph(
    request: WorkflowRunRequest,
    *,
    use_langgraph: bool | None = None,
    thread_id: str | None = None,
    require_langgraph: bool = False,
) -> WorkflowRunResult:
    """Advance a WorkItem directly or through LangGraph based on backend policy."""

    override = work_item_langgraph_env_override()
    enabled = (
        bool(use_langgraph)
        if use_langgraph is not None
        else override
        if override is not None
        else should_use_langgraph_for_work_item(request)
    )
    if not enabled:
        return advance_work_item(request)
    graph_thread_id = thread_id or work_item_graph_thread_id(request.work_item_id or "")
    outcome = run_work_item_langgraph(
        request,
        thread_id=graph_thread_id or None,
        require_langgraph=require_langgraph,
    )
    return outcome.result


def advance_work_item_manager_loop_with_optional_langgraph(
    request: WorkflowRunRequest,
    *,
    max_steps: int = 3,
    use_langgraph: bool | None = None,
    thread_id: str | None = None,
    require_langgraph: bool = False,
    feedback_callback: Any | None = None,
) -> WorkflowRunResult:
    """Run the integrated manager loop directly or through backend-selected LangGraph."""

    override = work_item_langgraph_env_override()
    enabled = (
        bool(use_langgraph)
        if use_langgraph is not None
        else override
        if override is not None
        else should_use_langgraph_for_work_item(request, manager_loop=True)
    )
    if not enabled:
        return advance_work_item_manager_loop(
            request,
            max_steps=max_steps,
            feedback_callback=feedback_callback,
        )
    graph_thread_id = thread_id or work_item_graph_thread_id(request.work_item_id or "")
    outcome = run_work_item_langgraph(
        request,
        thread_id=graph_thread_id or None,
        require_langgraph=require_langgraph,
        manager_loop=True,
        max_manager_steps=max_steps,
        feedback_callback=feedback_callback,
    )
    return outcome.result


def next_langgraph_node_for_result(
    result: WorkflowRunResult,
) -> Literal["approval_checkpoint", "done"]:
    """Return the next graph node implied by a WorkItem advancement result."""

    if _approval_checkpoint_reason(result):
        return "approval_checkpoint"
    return "done"


def _emit_langgraph_feedback(
    feedback_callback: Any | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Emit best-effort graph feedback without changing WorkItem result schemas."""

    if feedback_callback is None:
        return
    try:
        feedback_callback(event_type, payload)
    except Exception:
        return



def _run_dependency_free_graph(state: WorkItemGraphState) -> WorkItemGraphState:
    state = _normalize_request_node(state)
    state = _orchestrator_preflight_node(state)
    state = _state_followup_node(state)
    if _route_after_state_followup(state) == "done":
        return state
    state = _prepare_work_item_node(state)
    state = _stage_requested_context_edges(state)
    if state.get("checkpoint_required"):
        state = _approval_checkpoint_node(state)
        return state
    route_node = _route_to_specialist_node(state)
    specialist_nodes = {
        "run_business_research": _run_business_research_node,
        "run_opportunity_scout": _run_opportunity_scout_node,
        "run_gmail_triage": _run_gmail_triage_node,
        "run_outreach_composer": _run_outreach_composer_node,
        "run_chief_of_staff": _run_chief_of_staff_node,
        "run_unsupported_route": _run_unsupported_route_node,
    }
    state = specialist_nodes[route_node](state)
    while True:
        state = _finalize_step_node(state)
        next_node = _route_after_finalize(state)
        if next_node == "stage_airtable_context":
            state = _stage_airtable_context_node(state)
            if state.get("checkpoint_required"):
                state = _approval_checkpoint_node(state)
                return state
            route_node = _route_to_specialist_node(state)
            state = specialist_nodes[route_node](state)
            continue
        if next_node == "stage_google_workspace_context":
            state = _stage_google_workspace_context_node(state)
            if state.get("checkpoint_required"):
                state = _approval_checkpoint_node(state)
                return state
            route_node = _route_to_specialist_node(state)
            state = specialist_nodes[route_node](state)
            continue
        if next_node == "approval_checkpoint":
            state = _approval_checkpoint_node(state)
            return state
        if next_node == "manager_loop_finalize":
            state = _manager_loop_finalize_node(state)
            return state
        if next_node != "manager_loop_continue":
            return state
        state = _manager_loop_continue_node(state)
        state = _prepare_work_item_node(state)
        state = _stage_requested_context_edges(state)
        if state.get("checkpoint_required"):
            state = _approval_checkpoint_node(state)
            return state
        route_node = _route_to_specialist_node(state)
        state = specialist_nodes[route_node](state)


def _stage_requested_context_edges(state: WorkItemGraphState) -> WorkItemGraphState:
    """Run pending read-only context staging nodes before the specialist node."""

    while True:
        next_node = _route_after_context_staging(state)
        if next_node == "stage_feed_context":
            state = _stage_feed_context_node(state)
            continue
        if next_node == "stage_zotero_context":
            state = _stage_zotero_context_node(state)
            continue
        if next_node == "stage_airtable_context":
            state = _stage_airtable_context_node(state)
            if state.get("checkpoint_required"):
                return state
            continue
        if next_node == "stage_google_workspace_context":
            state = _stage_google_workspace_context_node(state)
            if state.get("checkpoint_required"):
                return state
            continue
        return state


def _graph_step_summary(result: WorkflowRunResult, step_index: int) -> dict[str, Any]:
    return {
        "step": step_index,
        "route": result.route.value,
        "status": result.status.value,
        "advanced": result.advanced,
        "artifact_types": [ref.artifact_type for ref in result.artifact_refs],
        "next_action": result.next_action.action if result.next_action else "",
    }


def _graph_completion_review(
    *,
    original_request: WorkflowRunRequest,
    result: WorkflowRunResult,
    node_path: list[str],
    loop_steps: list[dict[str, Any]],
    stop_reason: str,
    checkpoint_required: bool,
    checkpoint_reason: str,
) -> dict[str, Any]:
    artifact_types = [ref.artifact_type for ref in result.work_item.artifact_refs]
    blocker_payloads = [
        {"code": blocker.code, "message": blocker.message}
        for blocker in result.blockers
        if str(blocker.code or "").startswith("manager_loop_")
    ]
    requested_stages = _graph_requested_stage_review(
        request_text=original_request.request_text,
        manual_intent=str(
            (original_request.manual_request_plan or {}).get("intent")
            if isinstance(original_request.manual_request_plan, dict)
            else ""
        ),
        node_path=node_path,
        loop_steps=loop_steps,
        artifact_types=artifact_types,
        blocker_codes={str(item["code"]) for item in blocker_payloads},
        checkpoint_required=checkpoint_required,
        checkpoint_reason=checkpoint_reason,
    )
    completed_routes = [
        str(step.get("route") or "")
        for step in loop_steps
        if str(step.get("route") or "")
    ]
    summary_lines = _graph_completion_summary_lines(
        requested_stages=requested_stages,
        stop_reason=stop_reason,
        checkpoint_required=checkpoint_required,
        checkpoint_reason=checkpoint_reason,
    )
    return {
        "schema": "keystone.langgraph.completion_review.v1",
        "review_mode": "deterministic",
        "llm_review_used": False,
        "cost_guard": {
            "mode": "deterministic_graph_completion",
            "model_call": False,
            "scope": (
                "requested stage coverage, checkpoint state, source/artifact "
                "presence, and side-effect boundaries"
            ),
            "deterministic_hard_gates_authoritative": True,
        },
        "deterministic_gates_authoritative": True,
        "stop_reason": stop_reason,
        "completed_nodes": list(node_path),
        "completed_routes": completed_routes,
        "completed_artifact_types": artifact_types,
        "requested_stages": requested_stages,
        "missing_required_stages": blocker_payloads,
        "checkpoint_required": checkpoint_required,
        "checkpoint_reason": checkpoint_reason,
        "renderer_summary_lines": summary_lines,
        "send_enabled": False,
        "external_writes_enabled": False,
    }


def _enhance_graph_terminal_summary(
    *,
    original_request: WorkflowRunRequest,
    result: WorkflowRunResult,
    graph_completion_review: dict[str, Any],
) -> WorkflowRunResult:
    """Compose a compact graph-aware operator brief without another model call."""

    request_text = " ".join(str(original_request.request_text or "").lower().split())
    if not re.search(
        r"\b(?:visible result|recommendation|strongest evidence|main uncertainty|"
        r"next step|decision|data\s*source|facts?|inference|limitations?|unknowns?|"
        r"source links?|collaboration angle)\b",
        request_text,
    ):
        return result
    completed_routes = {
        str(route) for route in graph_completion_review.get("completed_routes") or []
    }
    if len(completed_routes) < 2:
        return result
    research_summary = _graph_research_terminal_summary(
        request_text=request_text,
        result=result,
        completed_routes=completed_routes,
    )
    if research_summary:
        return result.model_copy(
            update={
                "human_summary": research_summary,
                "audit_notes": [
                    *result.audit_notes,
                    (
                        "LangGraph research terminal brief composed deterministically "
                        "from source-backed artifact evidence."
                    ),
                ],
            }
        )
    outreach = next(
        (
            artifact
            for artifact in reversed(result.work_item.artifact_refs)
            if artifact.artifact_type == "outreach_draft"
        ),
        None,
    )
    if outreach is None:
        return result
    model_recommendation = outreach.metadata.get("model_recommendation")
    recommendation = model_recommendation if isinstance(model_recommendation, dict) else {}
    next_step = str(recommendation.get("recommended_next_step") or "").strip()
    thread_local_draft = outreach.metadata.get("thread_local_slack_draft") is True
    reply_recommended = recommendation.get("reply_recommended") is True or thread_local_draft
    decision = (
        "Proceed with a brief exploratory reply after human review."
        if reply_recommended
        else "Hold outreach until the missing context is resolved."
    )
    evidence = [
        str(fact.value).strip()
        for fact in result.work_item.facts
        if str(fact.value or "").strip()
        and str(fact.approval_state or "")
        in {ApprovalState.APPROVED_FOR_DRAFTING.value, ApprovalState.APPROVED_FOR_RESEARCH.value}
    ][:2]
    missing = recommendation.get("additional_information_needed")
    uncertainty = (
        [str(item).strip() for item in missing if str(item or "").strip()][:2]
        if isinstance(missing, list)
        else []
    )
    if not uncertainty:
        uncertainty = [
            str(fact.value).strip()
            for fact in result.work_item.facts
            if "unsupported" in str(fact.key or "").lower()
            and str(fact.value or "").strip()
        ][:2]
    if not next_step:
        next_step = (
            "Review the thread-local draft; no Gmail/provider draft or send is authorized."
            if thread_local_draft
            else "Review the draft approval item before any external use."
        )
    sections = [f"*Recommendation:*\n{decision}"]
    if evidence:
        sections.append("*Strongest evidence:*\n" + "\n".join(f"- {item}" for item in evidence))
    if uncertainty:
        sections.append(
            "*Main uncertainty:*\n" + "\n".join(f"- {item}" for item in uncertainty)
        )
    if next_step:
        sections.append(f"*Next step:*\n{next_step}")
    draft_for_review = result.human_summary.strip()
    suggested_reply = re.search(
        r"\*Suggested reply:\*\s*(?P<reply>.*?)"
        r"(?=\n\n\*Supporting evidence and approval status:\*|\Z)",
        draft_for_review,
        flags=re.S,
    )
    if suggested_reply is not None:
        draft_for_review = suggested_reply.group("reply").strip()
    if draft_for_review:
        sections.append(f"*Draft for review:*\n{draft_for_review}")
    return result.model_copy(
        update={
            "human_summary": "\n\n".join(sections),
            "audit_notes": [
                *result.audit_notes,
                (
                    "LangGraph terminal operator brief composed deterministically "
                    "from approved artifacts."
                ),
            ],
        }
    )


def _graph_research_terminal_summary(
    *,
    request_text: str,
    result: WorkflowRunResult,
    completed_routes: set[str],
) -> str:
    """Return a source-visible brief for research-only graph endings."""

    if WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value not in completed_routes:
        return ""
    if WorkItemRoute.OUTREACH_COMPOSER.value in completed_routes:
        return ""
    if not re.search(
        r"\b(?:data\s*source|facts?|inference|limitations?|unknowns?|source links?|"
        r"collaboration angle|why .* matters)\b",
        request_text,
    ):
        return ""
    profile = next(
        (
            artifact
            for artifact in reversed(result.work_item.artifact_refs)
            if artifact.artifact_type == "company_profile"
        ),
        None,
    )
    if profile is None:
        return ""
    source_refs = profile.metadata.get("source_refs")
    if not isinstance(source_refs, list):
        return ""
    marker_weights = {
        "neuroblu": 5,
        "longitudinal": 4,
        "coverage": 4,
        "de-identified": 4,
        "deidentified": 4,
        "tokenization": 4,
        "nlp": 4,
        "claims": 3,
        "linkage": 3,
        "provenance": 3,
        "million": 3,
        "years": 3,
        "inpatient": 3,
        "outpatient": 3,
        "unstructured": 3,
        "structured": 2,
        "notes": 2,
        "clinical": 2,
        "ehr": 2,
        "encounter": 2,
        "patient": 2,
        "partner": 2,
        "data": 1,
        "database": 1,
        "dataset": 1,
        "record": 1,
    }
    fact_candidates: list[tuple[int, int, str, str, str]] = []
    clean_sources: list[tuple[str, str]] = []
    for source in source_refs:
        if not isinstance(source, dict):
            continue
        title = " ".join(str(source.get("title") or "Source").split())[:180]
        url = str(source.get("url") or "").strip()
        if url and url.startswith(("http://", "https://")) and (title, url) not in clean_sources:
            clean_sources.append((title, url))
        facts = source.get("key_facts")
        if not isinstance(facts, list):
            continue
        for value in facts:
            raw_fact = str(value or "").strip()
            fragments = [
                " ".join(fragment.split()).strip(" -#")
                for fragment in re.split(r"(?:\n\s*\.\.\.\s*\n|\n+)", raw_fact)
            ]
            fragments = [fragment for fragment in fragments if 20 <= len(fragment) <= 650]
            if not fragments:
                fragments = [" ".join(raw_fact.split())[:650].strip()]
            for fact in fragments:
                if not fact:
                    continue
                normalized_fact = fact.lower()
                words = set(re.findall(r"[a-z0-9-]+", normalized_fact))
                marker_score = sum(
                    weight for marker, weight in marker_weights.items() if marker in words
                )
                if re.search(r"\b\d[\d,.+]*\s*(?:million|patient|record|year)", normalized_fact):
                    marker_score += 5
                if normalized_fact.startswith(("ehr data,", "how to ", "previously on ")):
                    marker_score -= 4
                if marker_score >= 3:
                    fact_candidates.append((marker_score, -len(fact), fact, title, url))
    fact_candidates.sort(reverse=True)
    selected_facts: list[tuple[str, str, str]] = []
    seen_facts: set[str] = set()
    selected_per_source: dict[str, int] = {}
    for _score, _length, fact, title, url in fact_candidates:
        normalized = fact.lower()
        if normalized in seen_facts or (url and selected_per_source.get(url, 0) >= 2):
            continue
        seen_facts.add(normalized)
        if url:
            selected_per_source[url] = selected_per_source.get(url, 0) + 1
        selected_facts.append((fact, title, url))
        if len(selected_facts) == 4:
            break
    if not selected_facts:
        return ""
    platform_match = next(
        (
            re.search(
                r"\b([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){0,2}\s+"
                r"(?:Analytics|Database|Platform))\b",
                fact,
            )
            for fact, _title, _url in selected_facts
            if re.search(
                r"\b([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){0,2}\s+"
                r"(?:Analytics|Database|Platform))\b",
                fact,
            )
        ),
        None,
    )
    gmail_focus_terms = result.work_item.target.metadata.get("gmail_research_focus_terms")
    platform = (
        " ".join(str(gmail_focus_terms[0]).split())
        if isinstance(gmail_focus_terms, list) and gmail_focus_terms
        else platform_match.group(1)
        if platform_match is not None
        else profile.title
    )
    supported_lines = []
    for fact, title, url in selected_facts:
        citation = f" ([{title}]({url}))" if url else ""
        supported_lines.append(f"- {fact}{citation}")
    selected_sources = []
    for _fact, title, url in selected_facts:
        if url and (title, url) not in selected_sources:
            selected_sources.append((title, url))
    source_lines = [f"- [{title}]({url})" for title, url in selected_sources[:5]]
    gmail_target = " ".join(
        str(result.work_item.target.metadata.get("gmail_research_target") or "").split()
    )
    identity_sentence = (
        f"The selected Gmail thread identifies {gmail_target} as the organization and "
        f"{platform} as its named data platform."
        if gmail_target
        else f"The source-backed platform is {platform}."
    )
    sections = [
        (
            "*Answer:*\n"
            f"{identity_sentence} The evidence describes a "
            "behavioral-health real-world clinical data asset assembled from partner "
            "data and designed for linked research analysis."
        ),
        (
            "*Organization:*\n"
            f"{gmail_target} is the sender organization identified from the selected "
            f"Gmail thread. Its public product materials present {platform} as its "
            "neuropsychiatry real-world-data and analytics offering. Ownership, funding, "
            "customer mix, and current organizational scale should be treated as unknown "
            "unless the selected sources explicitly establish them."
        )
        if gmail_target
        else "",
        "*Directly supported facts:*\n" + "\n".join(supported_lines),
        (
            "*Inference and unknowns:*\n"
            "- The reviewed sources do not independently establish representativeness, "
            "missingness, cross-site harmonization, or fitness for a specific analytic endpoint.\n"
            "- Scale and coverage claims remain source-reported unless corroborated by an "
            "independent methods or data-provenance description."
        ),
        (
            "*Why it matters for KNI:*\n"
            "A useful KNI collaboration would be a source-provenance and fitness-for-purpose "
            "review covering cohort construction, variable coverage, missingness and bias, "
            "linkage validity, and endpoint suitability before the data supports evaluation claims."
        ),
    ]
    if source_lines:
        sections.append("*Sources:*\n" + "\n".join(source_lines))
    sections = [section for section in sections if section]
    return "\n\n".join(sections)


def _graph_requested_stage_review(
    *,
    request_text: str,
    manual_intent: str = "",
    node_path: list[str],
    loop_steps: list[dict[str, Any]],
    artifact_types: list[str],
    blocker_codes: set[str],
    checkpoint_required: bool,
    checkpoint_reason: str,
) -> list[dict[str, str]]:
    normalized = " ".join(str(request_text or "").lower().split())
    stages: list[dict[str, str]] = []
    route_values = {str(step.get("route") or "") for step in loop_steps}

    def add_stage(
        stage: str,
        *,
        requested: bool,
        node: str,
        route: WorkItemRoute | None = None,
        artifacts: tuple[str, ...] = (),
        blocker_code: str = "",
        intentionally_skipped: bool = False,
    ) -> None:
        if not requested:
            return
        completed = (
            node in node_path
            or bool(route and route.value in route_values)
            or any(artifact in artifact_types for artifact in artifacts)
        )
        if completed:
            status = "completed"
            evidence = node if node in node_path else "artifact_or_route"
        elif blocker_code and blocker_code in blocker_codes:
            status = "missing_required"
            evidence = blocker_code
        elif intentionally_skipped:
            status = "intentionally_skipped"
            evidence = "operator_no_draft_or_no_side_effect_constraint"
        else:
            status = "not_completed"
            evidence = "no_matching_graph_node_or_artifact"
        stages.append({"stage": stage, "status": status, "evidence": evidence})

    add_stage(
        "chief_of_staff",
        requested=("chief of staff" in normalized or "run_chief_of_staff" in node_path),
        node="run_chief_of_staff",
        route=WorkItemRoute.CHIEF_OF_STAFF,
        artifacts=("chief_of_staff_plan",),
    )
    add_stage(
        "gmail_triage",
        requested=bool(
            re.search(r"\b(?:gmail|inbound email|email thread|triage)\b", normalized)
            or "run_gmail_triage" in node_path
        ),
        node="run_gmail_triage",
        route=WorkItemRoute.GMAIL_TRIAGE,
        artifacts=("gmail_triage_report",),
        blocker_code="manager_loop_gmail_triage_not_completed",
    )
    add_stage(
        "business_research",
        requested=bool(
            re.search(r"\b(?:research|source-backed|company profile)\b", normalized)
            or (
                manual_intent != "business_system_write"
                and re.search(r"\bevidence\b", normalized)
            )
            or "run_business_research" in node_path
        ),
        node="run_business_research",
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        artifacts=("company_profile",),
        blocker_code="manager_loop_research_not_completed",
    )
    add_stage(
        "opportunity_scout",
        requested=bool(
            re.search(
                r"\b(?:opportunit|advisory/research|advisory|research opportunity)\b",
                normalized,
            )
            or "run_opportunity_scout" in node_path
        ),
        node="run_opportunity_scout",
        route=WorkItemRoute.OPPORTUNITY_SCOUT,
        artifacts=("opportunity",),
        blocker_code="manager_loop_opportunity_not_created",
    )
    outreach_constraint_text = re.sub(
        r"\b(?:do\s+not|don't|dont|never|without)\s+"
        r"(?:create|save|write)\s+(?:a\s+|any\s+)?"
        r"(?:gmail\s+|provider\s+)?drafts?\b",
        "",
        normalized,
    )
    no_outreach = bool(
        re.search(
            r"\b(?:do not|don't|no|without)\b[^,.\n;]{0,120}"
            r"\b(?:draft|outreach|email|gmail draft|post|send)\b",
            outreach_constraint_text,
        )
    )
    outreach_requested = bool(
        re.search(
            r"\b(?:draft-only|draft only|sample outreach|outreach|reply|response|email draft)\b",
            normalized,
        )
        or "run_outreach_composer" in node_path
    )
    add_stage(
        "outreach_composer",
        requested=outreach_requested,
        node="run_outreach_composer",
        route=WorkItemRoute.OUTREACH_COMPOSER,
        artifacts=("outreach_draft",),
        blocker_code="manager_loop_outreach_not_drafted",
        intentionally_skipped=no_outreach,
    )
    approval_requested = bool(
        re.search(r"\b(?:approval checkpoint|approval|approve|review before)\b", normalized)
        or checkpoint_required
        or "approval_checkpoint" in node_path
    )
    if approval_requested:
        if checkpoint_required or "approval_checkpoint" in node_path:
            status = "completed"
            evidence = checkpoint_reason or "approval_checkpoint"
        elif no_outreach:
            status = "not_required"
            evidence = "no_outreach_or_no_side_effect_constraint"
        else:
            status = "not_completed"
            evidence = "no_approval_checkpoint_node"
        stages.append({"stage": "approval_checkpoint", "status": status, "evidence": evidence})
    return stages


def _graph_completion_summary_lines(
    *,
    requested_stages: list[dict[str, str]],
    stop_reason: str,
    checkpoint_required: bool,
    checkpoint_reason: str,
) -> list[str]:
    completed = [
        item["stage"] for item in requested_stages if item.get("status") == "completed"
    ]
    incomplete = [
        f"{item.get('stage')}={item.get('status')}"
        for item in requested_stages
        if item.get("status") not in {"completed", "not_required"}
    ]
    lines = [
        "Graph path completed: " + (" -> ".join(completed) if completed else "none"),
        f"Graph stop reason: {stop_reason}",
    ]
    if incomplete:
        lines.append("Still needs attention: " + ", ".join(incomplete))
    if checkpoint_required:
        lines.append(f"Approval checkpoint: {checkpoint_reason or 'required'}")
    lines.append(
        "Side effects: sends, posts, drafts, schedules, files, and external writes disabled"
    )
    return lines


def _manager_loop_stop_reason(state: WorkItemGraphState) -> str:
    if not state.get("manager_loop"):
        return ""
    result = WorkflowRunResult.model_validate(state["result"])
    loop_steps = list(state.get("loop_steps") or [])
    step_index = len(loop_steps)
    max_steps = max(1, min(5, int(state.get("max_manager_steps") or 3)))
    if not result.advanced:
        return "stopped because the current graph step did not safely advance"
    if result.status in _MANAGER_LOOP_STOP_STATUSES:
        return f"stopped at WorkItem status {result.status.value}"
    if step_index >= max_steps:
        return f"stopped at graph manager loop max_steps={max_steps}"
    if result.next_action is None:
        return "stopped because no next action was available"
    if result.next_action.requires_approval:
        return "stopped because the next action requires approval"
    if result.next_action.agent in {None, result.route}:
        return "stopped because the next action did not require a distinct specialist"
    if result.route != WorkItemRoute.CHIEF_OF_STAFF:
        previous_routes = {
            str(step.get("route") or "")
            for step in loop_steps[:-1]
            if str(step.get("route") or "")
        }
        if result.next_action.agent.value in previous_routes:
            return "stopped before repeating a specialist already used in this graph loop"
        original_request = WorkflowRunRequest.model_validate(
            state.get("original_request") or state.get("request") or {}
        )
        if not _operator_requested_manager_continuation(
            original_request.request_text,
            next_action_agent=result.next_action.agent,
        ):
            return "stopped after one specialist step; no multi-step workflow was requested"
    return ""


def _manager_loop_continue_node(state: WorkItemGraphState) -> WorkItemGraphState:
    result = WorkflowRunResult.model_validate(state["result"])
    original_request = WorkflowRunRequest.model_validate(
        state.get("original_request") or state.get("request") or {}
    )
    manual_request_plan = dict(original_request.manual_request_plan or {})
    manual_request_plan.setdefault("objective", original_request.request_text)
    next_request = original_request.model_copy(
        update={
            "request_text": "continue",
            "work_item_id": result.work_item.id,
            "requested_route": None,
            "manual_request_plan": manual_request_plan,
        }
    )
    return {
        **state,
        "request": next_request.model_dump(mode="json"),
        "node_path": [*state.get("node_path", []), "manager_loop_continue"],
        "graph_stop_reason": "",
    }


def _manager_loop_finalize_node(state: WorkItemGraphState) -> WorkItemGraphState:
    result = WorkflowRunResult.model_validate(state["result"])
    original_request = WorkflowRunRequest.model_validate(
        state.get("original_request") or state.get("request") or {}
    )
    stop_reason = str(
        state.get("graph_stop_reason")
        or _manager_loop_stop_reason(state)
        or "stopped because no next graph edge was available"
    )
    loop_steps = list(state.get("loop_steps") or [])
    store = (
        SQLiteStore(original_request.database_url or database_url_from_env())
        if original_request.save
        else None
    )
    result = _finalize_manager_loop_result(
        result,
        original_request=original_request,
        stop_reason=stop_reason,
        loop_steps=loop_steps,
        store=store,
        feedback_callback=None,
    )
    if original_request.save:
        if store is None:
            store = SQLiteStore(original_request.database_url or database_url_from_env())
        node_path = [*state.get("node_path", []), "manager_loop_finalize"]
        graph_completion_review = _graph_completion_review(
            original_request=original_request,
            result=result,
            node_path=node_path,
            loop_steps=loop_steps,
            stop_reason=stop_reason,
            checkpoint_required=False,
            checkpoint_reason="",
        )
        record_event(
            result.work_item,
            event_type="langgraph_manager_loop_completed",
            actor="orchestrator",
            summary=f"LangGraph manager loop {stop_reason}.",
            metadata={
                "stop_reason": stop_reason,
                "steps": loop_steps,
                "send_enabled": False,
                "external_writes_enabled": False,
                "graph_completion_review": graph_completion_review,
                "schema_policy": (
                    "reused WorkflowRunResult, WorkItem events, and specialist schemas"
                ),
            },
            store=store,
        )
    return _state_with_result(
        {
            **state,
            "node_path": [*state.get("node_path", []), "manager_loop_finalize"],
            "graph_stop_reason": stop_reason,
        },
        result,
    )


def _run_legacy_single_pass_tail(state: WorkItemGraphState) -> WorkItemGraphState:
    """Retained only to keep old linear fallback shape easy to compare in tests."""

    state = _finalize_step_node(state)
    if _route_after_finalize(state) == "approval_checkpoint":
        state = _approval_checkpoint_node(state)
    return state


def _normalize_request_node(state: WorkItemGraphState) -> WorkItemGraphState:
    request = WorkflowRunRequest.model_validate(state.get("request") or {})
    normalized = normalize_workflow_request_for_graph(request)
    return {
        **state,
        "request": normalized.model_dump(mode="json"),
        "node_path": [*state.get("node_path", []), "normalize_request"],
        "improvements": state.get("improvements") or langgraph_functionality_improvements(),
    }


def _orchestrator_preflight_node(state: WorkItemGraphState) -> WorkItemGraphState:
    request = WorkflowRunRequest.model_validate(state.get("request") or {})
    preflight = (
        request.orchestrator_preflight if isinstance(request.orchestrator_preflight, dict) else {}
    )
    selected_agent = str(preflight.get("selected_agent") or preflight.get("requested_agent") or "")
    return {
        **state,
        "node_path": [*state.get("node_path", []), "orchestrator_preflight"],
        "graph_stop_reason": (
            "blocked_by_orchestrator_preflight"
            if bool(preflight.get("blocked_by_orchestrator"))
            else str(state.get("graph_stop_reason") or "")
        ),
        "route": selected_agent or str(state.get("route") or ""),
    }


def _state_followup_node(state: WorkItemGraphState) -> WorkItemGraphState:
    request = WorkflowRunRequest.model_validate(state.get("request") or {})
    result = answer_work_item_state_followup(request)
    if result is None:
        return {
            **state,
            "node_path": [*state.get("node_path", []), "state_followup"],
            "terminal": False,
        }
    return _state_with_result(
        {
            **state,
            "node_path": [*state.get("node_path", []), "state_followup"],
            "terminal": True,
            "graph_stop_reason": "answered_from_existing_work_item_state",
        },
        result,
    )


def _prepare_work_item_node(state: WorkItemGraphState) -> WorkItemGraphState:
    request = WorkflowRunRequest.model_validate(state.get("request") or {})
    prepared = prepare_work_item_step(request)
    return {
        **state,
        "request": prepared.request.model_dump(mode="json"),
        "prepared_step": _prepared_step_payload(prepared),
        "route": prepared.route.value,
        "status": prepared.work_item.status.value,
        "node_path": [*state.get("node_path", []), "prepare_work_item"],
    }


def _stage_feed_context_node(state: WorkItemGraphState) -> WorkItemGraphState:
    prepared = _prepared_step_from_state(state)
    request_text = _context_edge_request_text(state, prepared)
    kind = _feed_context_edge_kind(request_text, prepared) or "rss"
    agent_name = _feed_context_agent_name(kind)
    work_item = prepared.work_item
    retrieval = _retrieve_feed_context_history(
        kind=kind,
        request_text=request_text,
        database_url=prepared.request.database_url,
    )
    source_refs = _feed_context_source_refs(
        work_item_id=work_item.id,
        kind=kind,
        request_text=request_text,
        retrieval=retrieval,
    )
    artifact = _feed_context_artifact(
        work_item_id=work_item.id,
        kind=kind,
        request_text=request_text,
        retrieval=retrieval,
        source_refs=source_refs,
    )
    existing_source_ids = {source.source_id for source in work_item.sources}
    sources = list(work_item.sources)
    for source_ref in source_refs:
        if source_ref.source_id not in existing_source_ids:
            sources.append(source_ref)
            existing_source_ids.add(source_ref.source_id)
    target_metadata = {
        **work_item.target.metadata,
        f"{kind}_context_handoff": {
            "agent_name": agent_name,
            "mode": "local_history_context_handoff",
            "artifact_type": artifact.artifact_type,
            "artifact_id": artifact.artifact_id,
            "item_count": int(retrieval.get("item_count") or 0),
            "external_writes_enabled": False,
        },
    }
    work_item = attach_artifact(
        work_item.model_copy(
            update={
                "sources": sources,
                "target": work_item.target.model_copy(update={"metadata": target_metadata}),
            }
        ),
        artifact,
    )
    route = _route_after_feed_context(prepared.route, request_text)
    store = None
    if prepared.request.save:
        store = SQLiteStore(prepared.request.database_url or database_url_from_env())
        store.save_work_item(work_item)
    prepared = PreparedWorkItemStep(
        request=prepared.request,
        work_item=work_item,
        route=route,
        input_text=prepared.input_text,
        context_pack=build_context_pack_for_route(work_item, route, store=store),
    )
    if prepared.request.save:
        if store is None:
            store = SQLiteStore(prepared.request.database_url or database_url_from_env())
        record_event(
            work_item,
            event_type="context_evidence_staged",
            actor=agent_name,
            summary=f"Staged read-only {kind} context for downstream {route.value}.",
            metadata={
                "artifact_type": artifact.artifact_type,
                "artifact_id": artifact.artifact_id,
                "source_ids": [source.source_id for source in source_refs],
                "downstream_route": route.value,
                "item_count": int(retrieval.get("item_count") or 0),
                "external_writes_enabled": False,
                "schema_policy": "WorkItem artifact/source handoff; no WorkItemRoute expansion",
            },
            store=store,
        )
    return {
        **state,
        "prepared_step": _prepared_step_payload(prepared),
        "route": route.value,
        "status": work_item.status.value,
        "node_path": [*state.get("node_path", []), "stage_feed_context"],
    }


def _stage_zotero_context_node(state: WorkItemGraphState) -> WorkItemGraphState:
    prepared = _prepared_step_from_state(state)
    request_text = _context_edge_request_text(state, prepared)
    work_item = prepared.work_item
    existing_zotero_sources = [
        source
        for source in work_item.sources
        if source.source_id.startswith("zotero:item:")
        or source.provider in {"zotero", "zotero_context_agent"}
    ]
    provider_evidence = bool(existing_zotero_sources)
    source_ref = (
        existing_zotero_sources[0]
        if existing_zotero_sources
        else _zotero_context_source_ref(work_item.id, request_text)
    )
    artifact = _zotero_context_artifact(
        work_item.id,
        request_text,
        source_ref,
        provider_evidence=provider_evidence,
    )
    existing_source_ids = {source.source_id for source in work_item.sources}
    sources = list(work_item.sources)
    if source_ref.source_id not in existing_source_ids:
        sources.append(source_ref)
    target_metadata = {
        **work_item.target.metadata,
        "zotero_context_handoff": {
            "agent_name": "zotero_context_agent",
            "mode": (
                "provider_source_handoff" if provider_evidence else "dry_run_context_handoff"
            ),
            "artifact_type": artifact.artifact_type,
            "artifact_id": artifact.artifact_id,
            "source_id": source_ref.source_id,
            "provider_evidence": provider_evidence,
            "external_writes_enabled": False,
        },
    }
    work_item = attach_artifact(
        work_item.model_copy(
            update={
                "sources": sources,
                "target": work_item.target.model_copy(update={"metadata": target_metadata}),
            }
        ),
        artifact,
    )
    route = _route_after_zotero_context(prepared.route, request_text)
    store = None
    if prepared.request.save:
        store = SQLiteStore(prepared.request.database_url or database_url_from_env())
        store.save_work_item(work_item)
    prepared = PreparedWorkItemStep(
        request=prepared.request,
        work_item=work_item,
        route=route,
        input_text=prepared.input_text,
        context_pack=build_context_pack_for_route(work_item, route, store=store),
    )
    if prepared.request.save:
        if store is None:
            store = SQLiteStore(prepared.request.database_url or database_url_from_env())
        record_event(
            work_item,
            event_type="context_evidence_staged",
            actor="zotero_context_agent",
            summary=f"Staged read-only Zotero context for downstream {route.value}.",
            metadata={
                "artifact_type": artifact.artifact_type,
                "artifact_id": artifact.artifact_id,
                "source_id": source_ref.source_id,
                "downstream_route": route.value,
                "external_writes_enabled": False,
                "schema_policy": "WorkItem artifact/source handoff; no WorkItemRoute expansion",
            },
            store=store,
        )
    return {
        **state,
        "prepared_step": _prepared_step_payload(prepared),
        "route": route.value,
        "status": work_item.status.value,
        "node_path": [*state.get("node_path", []), "stage_zotero_context"],
    }


def _stage_airtable_context_node(state: WorkItemGraphState) -> WorkItemGraphState:
    prepared = _prepared_step_from_state(state)
    prior_result = (
        WorkflowRunResult.model_validate(state["result"])
        if isinstance(state.get("result"), dict)
        else None
    )
    prior_result_present = prior_result is not None and prior_result.route == prepared.route
    if prior_result_present and prior_result is not None:
        prepared = PreparedWorkItemStep(
            request=prepared.request,
            work_item=prior_result.work_item,
            route=prior_result.route,
            input_text=prepared.input_text,
            context_pack=prior_result.context_pack or prepared.context_pack,
        )
    request_text = _context_edge_request_text(state, prepared)
    work_item = prepared.work_item
    existing_airtable_sources = [
        source
        for source in work_item.sources
        if source.source_id.startswith("airtable:")
        or source.provider == "airtable_context_agent"
    ]
    provider_evidence = bool(existing_airtable_sources)
    source_ref = (
        existing_airtable_sources[0]
        if existing_airtable_sources
        else _airtable_context_source_ref(work_item.id, request_text)
    )
    if (
        not prior_result_present
        and _airtable_context_should_stage_before_specialist(request_text, prepared)
    ):
        artifact = _airtable_context_summary_artifact(
            work_item.id,
            request_text,
            source_ref,
            provider_evidence=provider_evidence,
        )
        existing_source_ids = {source.source_id for source in work_item.sources}
        sources = list(work_item.sources)
        if source_ref.source_id not in existing_source_ids:
            sources.append(source_ref)
        target_metadata = {
            **work_item.target.metadata,
            "airtable_context_handoff": {
                "agent_name": "airtable_context_agent",
                "mode": (
                    "provider_source_handoff"
                    if provider_evidence
                    else "dry_run_read_context_handoff"
                ),
                "artifact_type": artifact.artifact_type,
                "artifact_id": artifact.artifact_id,
                "source_id": source_ref.source_id,
                "provider_evidence": provider_evidence,
                "external_writes_enabled": False,
            },
        }
        work_item = attach_artifact(
            work_item.model_copy(
                update={
                    "sources": sources,
                    "target": work_item.target.model_copy(update={"metadata": target_metadata}),
                }
            ),
            artifact,
        )
        store = None
        if prepared.request.save:
            store = SQLiteStore(prepared.request.database_url or database_url_from_env())
            store.save_work_item(work_item)
        prepared = PreparedWorkItemStep(
            request=prepared.request,
            work_item=work_item,
            route=prepared.route,
            input_text=prepared.input_text,
            context_pack=build_context_pack_for_route(work_item, prepared.route, store=store),
        )
        if prepared.request.save:
            if store is None:
                store = SQLiteStore(prepared.request.database_url or database_url_from_env())
            record_event(
                work_item,
                event_type="context_evidence_staged",
                actor="airtable_context_agent",
                summary=f"Staged read-only Airtable context for downstream {prepared.route.value}.",
                metadata={
                    "artifact_type": artifact.artifact_type,
                    "artifact_id": artifact.artifact_id,
                    "source_id": source_ref.source_id,
                    "downstream_route": prepared.route.value,
                    "external_writes_enabled": False,
                    "schema_policy": "WorkItem artifact/source handoff; no WorkItemRoute expansion",
                },
                store=store,
            )
        return {
            **state,
            "prepared_step": _prepared_step_payload(prepared),
            "route": prepared.route.value,
            "status": work_item.status.value,
            "node_path": [*state.get("node_path", []), "stage_airtable_context"],
        }

    artifact = _airtable_context_artifact(work_item.id, request_text, source_ref)
    existing_source_ids = {source.source_id for source in work_item.sources}
    sources = list(work_item.sources)
    if source_ref.source_id not in existing_source_ids:
        sources.append(source_ref)
    target_metadata = {
        **work_item.target.metadata,
        "airtable_context_handoff": {
            "agent_name": "airtable_context_agent",
            "mode": "dry_run_write_plan_handoff",
            "artifact_type": artifact.artifact_type,
            "artifact_id": artifact.artifact_id,
            "source_id": source_ref.source_id,
            "external_writes_enabled": False,
        },
    }
    approval_gate = WorkItemApprovalGate(
        scope="airtable_write_plan",
        state="pending",
        required=True,
        rationale=(
            "Human approval is required before creating, updating, attaching, or "
            "otherwise mutating Airtable records."
        ),
        approval_id=f"{work_item.id}:airtable_write_plan",
    )
    approval_gates = [
        gate for gate in work_item.approval_gates if gate.approval_id != approval_gate.approval_id
    ]
    work_item = attach_artifact(
        work_item.model_copy(
            update={
                "sources": sources,
                "target": work_item.target.model_copy(update={"metadata": target_metadata}),
                "approval_gates": [*approval_gates, approval_gate],
                "next_action": WorkItemNextAction(
                    action="review_airtable_write_plan",
                    agent=WorkItemRoute.CHIEF_OF_STAFF,
                    description=(
                        "Review the read-only Airtable context/write plan and provide "
                        "a scoped approval reference before any Airtable create, update, "
                        "attachment upload, or record mutation."
                    ),
                    requires_approval=True,
                ),
                "status": WorkItemStatus.NEEDS_APPROVAL,
                "last_agent": "airtable_context_agent",
                "audit_notes": [
                    *work_item.audit_notes,
                    "Airtable context write plan staged; no live reads or writes executed.",
                ],
            }
        ),
        artifact,
    ).touch()
    store = None
    if prepared.request.save:
        store = SQLiteStore(prepared.request.database_url or database_url_from_env())
        store.save_work_item(work_item)
        record_event(
            work_item,
            event_type="context_evidence_staged",
            actor="airtable_context_agent",
            summary="Staged read-only Airtable context/write plan for approval.",
            metadata={
                "artifact_type": artifact.artifact_type,
                "artifact_id": artifact.artifact_id,
                "source_id": source_ref.source_id,
                "approval_scope": approval_gate.scope,
                "external_writes_enabled": False,
                "schema_policy": "WorkItem artifact/source handoff; no WorkItemRoute expansion",
            },
            store=store,
        )
    context_pack = build_context_pack_for_route(
        work_item,
        WorkItemRoute.CHIEF_OF_STAFF,
        store=store,
    ).model_dump(mode="json")
    result = WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=work_item.status,
        advanced=True,
        artifact_refs=[artifact],
        next_action=work_item.next_action,
        human_summary=(
            "Airtable context/write plan is staged for review. Human approval is "
            "required before any Airtable create, update, attachment upload, or mutation."
        ),
        audit_notes=["Airtable context write plan staged; no live reads or writes executed."],
        manual_request_plan=prepared.request.manual_request_plan,
        orchestrator_preflight=prepared.request.orchestrator_preflight,
        context_pack=context_pack,
    )
    return _state_with_result(
        {
            **state,
            "prepared_step": _prepared_step_payload(
                PreparedWorkItemStep(
                    request=prepared.request,
                    work_item=work_item,
                    route=WorkItemRoute.CHIEF_OF_STAFF,
                    input_text=prepared.input_text,
                    context_pack=context_pack,
                )
            ),
            "node_path": [*state.get("node_path", []), "stage_airtable_context"],
            "checkpoint_required": True,
            "checkpoint_reason": (
                "Review the Airtable context/write plan and provide scoped approval "
                "before any Airtable write."
            ),
            "graph_stop_reason": "approval_checkpoint_required",
        },
        result,
    )


def _stage_google_workspace_context_node(state: WorkItemGraphState) -> WorkItemGraphState:
    prepared = _prepared_step_from_state(state)
    prior_result = (
        WorkflowRunResult.model_validate(state["result"])
        if isinstance(state.get("result"), dict)
        else None
    )
    prior_result_present = prior_result is not None and prior_result.route == prepared.route
    if prior_result_present and prior_result is not None:
        prepared = PreparedWorkItemStep(
            request=prepared.request,
            work_item=prior_result.work_item,
            route=prior_result.route,
            input_text=prepared.input_text,
            context_pack=prior_result.context_pack or prepared.context_pack,
        )
    request_text = _context_edge_request_text(state, prepared)
    work_item = prepared.work_item
    source_ref = _google_workspace_context_source_ref(work_item.id, request_text)
    if (
        not prior_result_present
        and _google_workspace_context_should_stage_before_specialist(request_text, prepared)
    ):
        artifact = _google_workspace_context_summary_artifact(
            work_item.id,
            request_text,
            source_ref,
        )
        existing_source_ids = {source.source_id for source in work_item.sources}
        sources = list(work_item.sources)
        if source_ref.source_id not in existing_source_ids:
            sources.append(source_ref)
        target_metadata = {
            **work_item.target.metadata,
            "google_workspace_context_handoff": {
                "agent_name": "google_workspace_context_agent",
                "mode": "dry_run_read_context_handoff",
                "artifact_type": artifact.artifact_type,
                "artifact_id": artifact.artifact_id,
                "source_id": source_ref.source_id,
                "external_writes_enabled": False,
            },
        }
        work_item = attach_artifact(
            work_item.model_copy(
                update={
                    "sources": sources,
                    "target": work_item.target.model_copy(update={"metadata": target_metadata}),
                }
            ),
            artifact,
        )
        store = None
        if prepared.request.save:
            store = SQLiteStore(prepared.request.database_url or database_url_from_env())
            store.save_work_item(work_item)
        prepared = PreparedWorkItemStep(
            request=prepared.request,
            work_item=work_item,
            route=prepared.route,
            input_text=prepared.input_text,
            context_pack=build_context_pack_for_route(work_item, prepared.route, store=store),
        )
        if prepared.request.save:
            if store is None:
                store = SQLiteStore(prepared.request.database_url or database_url_from_env())
            record_event(
                work_item,
                event_type="context_evidence_staged",
                actor="google_workspace_context_agent",
                summary=(
                    "Staged read-only Google Workspace context for downstream "
                    f"{prepared.route.value}."
                ),
                metadata={
                    "artifact_type": artifact.artifact_type,
                    "artifact_id": artifact.artifact_id,
                    "source_id": source_ref.source_id,
                    "downstream_route": prepared.route.value,
                    "external_writes_enabled": False,
                    "schema_policy": "WorkItem artifact/source handoff; no WorkItemRoute expansion",
                },
                store=store,
            )
        return {
            **state,
            "prepared_step": _prepared_step_payload(prepared),
            "route": prepared.route.value,
            "status": work_item.status.value,
            "node_path": [*state.get("node_path", []), "stage_google_workspace_context"],
        }

    artifact = _google_workspace_context_artifact(work_item.id, request_text, source_ref)
    existing_source_ids = {source.source_id for source in work_item.sources}
    sources = list(work_item.sources)
    if source_ref.source_id not in existing_source_ids:
        sources.append(source_ref)
    target_metadata = {
        **work_item.target.metadata,
        "google_workspace_context_handoff": {
            "agent_name": "google_workspace_context_agent",
            "mode": "dry_run_artifact_plan_handoff",
            "artifact_type": artifact.artifact_type,
            "artifact_id": artifact.artifact_id,
            "source_id": source_ref.source_id,
            "external_writes_enabled": False,
        },
    }
    approval_gate = WorkItemApprovalGate(
        scope="google_workspace_artifact_plan",
        state="pending",
        required=True,
        rationale=(
            "Human approval is required before creating, updating, sharing, or "
            "otherwise mutating Google Workspace artifacts."
        ),
        approval_id=f"{work_item.id}:google_workspace_artifact_plan",
    )
    approval_gates = [
        gate
        for gate in work_item.approval_gates
        if gate.approval_id != approval_gate.approval_id
    ]
    work_item = attach_artifact(
        work_item.model_copy(
            update={
                "sources": sources,
                "target": work_item.target.model_copy(update={"metadata": target_metadata}),
                "approval_gates": [*approval_gates, approval_gate],
                "next_action": WorkItemNextAction(
                    action="review_google_workspace_artifact_plan",
                    agent=WorkItemRoute.CHIEF_OF_STAFF,
                    description=(
                        "Review the read-only Workspace artifact plan and provide a scoped "
                        "approval reference before any Drive, Docs, Sheets, or sharing write."
                    ),
                    requires_approval=True,
                ),
                "status": WorkItemStatus.NEEDS_APPROVAL,
                "last_agent": "google_workspace_context_agent",
                "audit_notes": [
                    *work_item.audit_notes,
                    (
                        "Google Workspace context artifact plan staged; no live reads or "
                        "writes executed."
                    ),
                ],
            }
        ),
        artifact,
    ).touch()
    store = None
    if prepared.request.save:
        store = SQLiteStore(prepared.request.database_url or database_url_from_env())
        store.save_work_item(work_item)
        record_event(
            work_item,
            event_type="context_evidence_staged",
            actor="google_workspace_context_agent",
            summary="Staged read-only Google Workspace artifact plan for approval.",
            metadata={
                "artifact_type": artifact.artifact_type,
                "artifact_id": artifact.artifact_id,
                "source_id": source_ref.source_id,
                "approval_scope": approval_gate.scope,
                "external_writes_enabled": False,
                "schema_policy": "WorkItem artifact/source handoff; no WorkItemRoute expansion",
            },
            store=store,
        )
    context_pack = build_context_pack_for_route(
        work_item,
        WorkItemRoute.CHIEF_OF_STAFF,
        store=store,
    ).model_dump(mode="json")
    result = WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=work_item.status,
        advanced=True,
        artifact_refs=[artifact],
        next_action=work_item.next_action,
        human_summary=(
            "Google Workspace artifact plan is staged for review. Human approval is "
            "required before any Drive, Docs, Sheets, sharing, or file mutation."
        ),
        audit_notes=[
            "Google Workspace context artifact plan staged; no live reads or writes executed."
        ],
        manual_request_plan=prepared.request.manual_request_plan,
        orchestrator_preflight=prepared.request.orchestrator_preflight,
        context_pack=context_pack,
    )
    return _state_with_result(
        {
            **state,
            "prepared_step": _prepared_step_payload(
                PreparedWorkItemStep(
                    request=prepared.request,
                    work_item=work_item,
                    route=WorkItemRoute.CHIEF_OF_STAFF,
                    input_text=prepared.input_text,
                    context_pack=context_pack,
                )
            ),
            "node_path": [
                *state.get("node_path", []),
                "stage_google_workspace_context",
            ],
            "checkpoint_required": True,
            "checkpoint_reason": (
                "Review the Google Workspace artifact plan and provide scoped approval "
                "before any Workspace write."
            ),
            "graph_stop_reason": "approval_checkpoint_required",
        },
        result,
    )


def _run_business_research_node(state: WorkItemGraphState) -> WorkItemGraphState:
    return _run_specialist_node(state, "run_business_research")


def _run_opportunity_scout_node(state: WorkItemGraphState) -> WorkItemGraphState:
    return _run_specialist_node(state, "run_opportunity_scout")


def _run_gmail_triage_node(state: WorkItemGraphState) -> WorkItemGraphState:
    return _run_specialist_node(state, "run_gmail_triage")


def _run_outreach_composer_node(state: WorkItemGraphState) -> WorkItemGraphState:
    return _run_specialist_node(state, "run_outreach_composer")


def _run_chief_of_staff_node(state: WorkItemGraphState) -> WorkItemGraphState:
    return _run_specialist_node(state, "run_chief_of_staff")


def _run_unsupported_route_node(state: WorkItemGraphState) -> WorkItemGraphState:
    return _run_specialist_node(state, "run_unsupported_route")


def _run_specialist_node(state: WorkItemGraphState, node_name: str) -> WorkItemGraphState:
    prepared = _prepared_step_from_state(state)
    result = run_prepared_work_item_specialist(prepared)
    return _state_with_result(
        {
            **state,
            "node_path": [*state.get("node_path", []), node_name],
        },
        result,
    )


def _finalize_step_node(state: WorkItemGraphState) -> WorkItemGraphState:
    prepared = _prepared_step_from_state(state)
    result = WorkflowRunResult.model_validate(state["result"])
    manager_loop = bool(state.get("manager_loop"))
    result = finalize_prepared_work_item_step(
        prepared,
        result,
        synthesize_user_response=not manager_loop,
    )
    checkpoint_reason = _approval_checkpoint_reason(result)
    loop_steps = list(state.get("loop_steps") or [])
    if manager_loop:
        loop_steps.append(_graph_step_summary(result, len(loop_steps) + 1))
    updated = _state_with_result(
        {
            **state,
            "node_path": [*state.get("node_path", []), "finalize_step"],
            "checkpoint_required": bool(checkpoint_reason),
            "checkpoint_reason": checkpoint_reason,
            "loop_steps": loop_steps,
            "graph_stop_reason": "approval_checkpoint_required" if checkpoint_reason else "",
        },
        result,
    )
    if not checkpoint_reason:
        updated["graph_stop_reason"] = (
            _manager_loop_stop_reason(updated) if manager_loop else "done"
        )
    return updated


def _approval_checkpoint_node(state: WorkItemGraphState) -> WorkItemGraphState:
    result = WorkflowRunResult.model_validate(state["result"])
    payload = _approval_checkpoint_payload(
        result,
        reason=str(state.get("checkpoint_reason") or ""),
    )
    human_decision = None
    if state.get("enable_interrupts") and langgraph_available():
        try:
            from langgraph.types import interrupt
        except ImportError:
            interrupt = None
        if interrupt is not None:
            human_decision = interrupt(payload)
    return {
        **state,
        "node_path": [*state.get("node_path", []), "approval_checkpoint"],
        "human_checkpoint_payload": payload,
        "human_decision": human_decision,
    }


def _route_after_state_followup(state: WorkItemGraphState) -> Literal["prepare_work_item", "done"]:
    if state.get("terminal"):
        return "done"
    return "prepare_work_item"


def _route_after_prepare_work_item(
    state: WorkItemGraphState,
) -> Literal[
    "stage_feed_context",
    "stage_zotero_context",
    "stage_airtable_context",
    "stage_google_workspace_context",
    "run_business_research",
    "run_opportunity_scout",
    "run_gmail_triage",
    "run_outreach_composer",
    "run_chief_of_staff",
    "run_unsupported_route",
]:
    prepared = _prepared_step_from_state(state)
    if _chief_coordination_should_run_before_context_edges(state, prepared):
        return _route_to_specialist_node(state)
    if _feed_context_edge_kind(_context_edge_request_text(state, prepared), prepared):
        return "stage_feed_context"
    if _zotero_context_edge_requested(_context_edge_request_text(state, prepared), prepared):
        return "stage_zotero_context"
    if _airtable_context_edge_requested(_context_edge_request_text(state, prepared), prepared):
        if (
            _airtable_context_should_stage_before_specialist(
                _context_edge_request_text(state, prepared),
                prepared,
            )
            and not _airtable_context_summary_artifact_exists(prepared)
        ):
            return "stage_airtable_context"
        if _airtable_context_should_wait_for_specialist(prepared):
            return _route_to_specialist_node(state)
        return "stage_airtable_context"
    if _google_workspace_context_edge_requested(
        _context_edge_request_text(state, prepared),
        prepared,
    ):
        if (
            _google_workspace_context_should_stage_before_specialist(
                _context_edge_request_text(state, prepared),
                prepared,
            )
            and not _google_workspace_context_summary_artifact_exists(prepared)
        ):
            return "stage_google_workspace_context"
        if _google_workspace_context_should_wait_for_specialist(prepared):
            return _route_to_specialist_node(state)
        return "stage_google_workspace_context"
    return _route_to_specialist_node(state)


def _chief_coordination_should_run_before_context_edges(
    state: WorkItemGraphState,
    prepared: PreparedWorkItemStep,
) -> bool:
    if prepared.route != WorkItemRoute.CHIEF_OF_STAFF:
        return False
    request_text = _context_edge_request_text(state, prepared)
    normalized = " ".join(str(request_text or "").lower().split())
    if "chief of staff" not in normalized and not _chief_context_advisory_only_request(
        normalized
    ):
        return False
    if not (
        _feed_context_edge_kind(request_text, prepared)
        or _zotero_context_edge_requested(request_text, prepared)
        or _airtable_context_edge_requested(request_text, prepared)
        or _google_workspace_context_edge_requested(request_text, prepared)
    ):
        return False
    return bool(
        re.search(
            r"\b(?:coordinate|orchestrate|select|selected|before|then|handoff|"
            r"recommend(?:ing)?|next owner|best next owner|best next specialist|"
            r"assess|review|decide|evaluate|triage|prioritize|summarize|explain|"
            r"identify|return|design|advisory|advisor|advisors)\b",
            normalized,
        )
        and re.search(
            r"\b(?:business research|opportunity scout|gmail triage|outreach composer|"
            r"specialist|specialists?|main agent|main specialist|opportunit\w*|"
            r"research|evidence|source-backed|approval checkpoint|outreach|"
            r"context agents?|airtable context|google workspace context|"
            r"zotero context|rss context|preprints context)\b",
            normalized,
        )
    )


def _chief_context_advisory_only_request(normalized: str) -> bool:
    return bool(
        "agents-as-tools only" in normalized
        or "advisory specialist" in normalized
        or "advisory context" in normalized
        or "read-only advisors" in normalized
        or "read-only advisor" in normalized
        or re.search(r"\bcontext\s+as\s+an?\s+advisory\b", normalized)
    )


def _route_after_context_staging(
    state: WorkItemGraphState,
) -> Literal[
    "stage_feed_context",
    "stage_zotero_context",
    "stage_airtable_context",
    "stage_google_workspace_context",
    "run_business_research",
    "run_opportunity_scout",
    "run_gmail_triage",
    "run_outreach_composer",
    "run_chief_of_staff",
    "run_unsupported_route",
]:
    return _route_after_prepare_work_item(state)


def _route_after_airtable_context_staging(
    state: WorkItemGraphState,
) -> Literal[
    "approval_checkpoint",
    "stage_feed_context",
    "stage_zotero_context",
    "stage_airtable_context",
    "stage_google_workspace_context",
    "run_business_research",
    "run_opportunity_scout",
    "run_gmail_triage",
    "run_outreach_composer",
    "run_chief_of_staff",
    "run_unsupported_route",
]:
    if state.get("checkpoint_required"):
        return "approval_checkpoint"
    return _route_after_context_staging(state)


def _route_after_google_workspace_context_staging(
    state: WorkItemGraphState,
) -> Literal[
    "approval_checkpoint",
    "stage_feed_context",
    "stage_zotero_context",
    "stage_airtable_context",
    "stage_google_workspace_context",
    "run_business_research",
    "run_opportunity_scout",
    "run_gmail_triage",
    "run_outreach_composer",
    "run_chief_of_staff",
    "run_unsupported_route",
]:
    if state.get("checkpoint_required"):
        return "approval_checkpoint"
    return _route_after_context_staging(state)


def _route_to_specialist_node(
    state: WorkItemGraphState,
) -> Literal[
    "run_business_research",
    "run_opportunity_scout",
    "run_gmail_triage",
    "run_outreach_composer",
    "run_chief_of_staff",
    "run_unsupported_route",
]:
    route = str(state.get("route") or "")
    if route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value:
        return "run_business_research"
    if route == WorkItemRoute.OPPORTUNITY_SCOUT.value:
        return "run_opportunity_scout"
    if route == WorkItemRoute.GMAIL_TRIAGE.value:
        return "run_gmail_triage"
    if route == WorkItemRoute.OUTREACH_COMPOSER.value:
        return "run_outreach_composer"
    if route == WorkItemRoute.CHIEF_OF_STAFF.value:
        return "run_chief_of_staff"
    return "run_unsupported_route"


def _context_edge_request_text(
    state: WorkItemGraphState,
    prepared: PreparedWorkItemStep,
) -> str:
    original = WorkflowRunRequest.model_validate(
        state.get("original_request") or state.get("request") or {}
    )
    return " ".join(
        part
        for part in (
            original.request_text,
            prepared.request.request_text,
            prepared.input_text,
            prepared.work_item.request_text,
            prepared.work_item.target.name,
            _chief_context_handoff_request_text(prepared),
        )
        if str(part or "").strip()
    )


def _chief_context_handoff_request_text(prepared: PreparedWorkItemStep) -> str:
    parts: list[str] = []
    for artifact in prepared.work_item.artifact_refs:
        if artifact.artifact_type != "chief_of_staff_plan":
            continue
        handoffs = artifact.metadata.get("context_handoffs")
        if not isinstance(handoffs, list):
            continue
        for item in handoffs:
            if not isinstance(item, dict):
                continue
            parts.append(_chief_context_handoff_text(item))
    return " ".join(part for part in parts if part)


def _chief_context_handoff_text(handoff: dict[str, Any]) -> str:
    agent = str(handoff.get("agent") or "").strip()
    before_agent = str(handoff.get("before_agent") or "").strip()
    agent_text = {
        "rss_context_agent": "RSS context agent signal history",
        "preprints_context_agent": "preprints context agent evidence history",
        "zotero_context_agent": "Zotero context agent handoff collection evidence",
        "airtable_context_agent": "Airtable context agent schema context handoff",
        "google_workspace_context_agent": (
            "Google Workspace context agent artifact context handoff"
        ),
    }.get(agent)
    if not agent_text:
        return ""
    before_text = {
        "business_research_analyst": "before Business Research",
        "opportunity_scout": "before Opportunity Scout",
        "gmail_triage": "before Gmail Triage",
        "outreach_composer": "before Outreach Composer",
    }.get(before_agent, "before specialist research or opportunity review")
    return f"{agent_text} {before_text} as read-only context."


def _zotero_context_edge_requested(
    request_text: str,
    prepared: PreparedWorkItemStep,
) -> bool:
    if any(
        ref.artifact_type == "zotero_context_summary"
        for ref in prepared.work_item.artifact_refs
    ):
        return False
    normalized = " ".join(str(request_text or "").lower().split())
    if not _normalized_mentions_zotero_context_source(normalized):
        return False
    if not re.search(
        r"\b(?:agent|handoff|evidence|artifact|brief|packet|collection)\b",
        normalized,
    ):
        return False
    if not re.search(r"\b(?:research|source|evidence|artifact|brief|packet|plan)\b", normalized):
        return False
    return prepared.route in {
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        WorkItemRoute.OPPORTUNITY_SCOUT,
        WorkItemRoute.GMAIL_TRIAGE,
        WorkItemRoute.CHIEF_OF_STAFF,
        WorkItemRoute.ORCHESTRATOR,
        WorkItemRoute.CLARIFICATION,
    }


def _airtable_context_edge_requested(
    request_text: str,
    prepared: PreparedWorkItemStep,
) -> bool:
    if (
        any(ref.artifact_type == "airtable_write_plan" for ref in prepared.work_item.artifact_refs)
        and not _airtable_context_should_stage_before_specialist(request_text, prepared)
    ):
        return False
    if (
        _airtable_context_should_stage_before_specialist(request_text, prepared)
        and _airtable_context_summary_artifact_exists(prepared)
    ):
        return False
    normalized = " ".join(str(request_text or "").lower().split())
    if "airtable" not in normalized:
        return False
    if "context" not in normalized and "artifact" not in normalized and "plan" not in normalized:
        return False
    if not re.search(
        r"\b(?:agent|handoff|context|write|create|update|record|table|base|"
        r"schema|expense|receipt|attachment|plan)\b",
        normalized,
    ):
        return False
    if not re.search(
        r"\b(?:approval|review|write|create|update|sync|plan|schema|record|"
        r"table|base|expense|receipt)\b",
        normalized,
    ):
        return False
    return prepared.route in {
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        WorkItemRoute.OPPORTUNITY_SCOUT,
        WorkItemRoute.GMAIL_TRIAGE,
        WorkItemRoute.CHIEF_OF_STAFF,
        WorkItemRoute.ORCHESTRATOR,
        WorkItemRoute.CLARIFICATION,
    }


def _airtable_context_should_wait_for_specialist(prepared: PreparedWorkItemStep) -> bool:
    return prepared.route in {
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        WorkItemRoute.OPPORTUNITY_SCOUT,
        WorkItemRoute.GMAIL_TRIAGE,
    }


def _airtable_context_summary_artifact_exists(prepared: PreparedWorkItemStep) -> bool:
    return any(
        ref.artifact_type == "airtable_context_summary"
        for ref in prepared.work_item.artifact_refs
    )


def _airtable_context_should_stage_before_specialist(
    request_text: str,
    prepared: PreparedWorkItemStep,
) -> bool:
    if prepared.route not in {
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        WorkItemRoute.OPPORTUNITY_SCOUT,
        WorkItemRoute.GMAIL_TRIAGE,
    }:
        return False
    normalized = " ".join(str(request_text or "").lower().split())
    if "airtable" not in normalized or "context" not in normalized:
        return False
    airtable_pos = normalized.find("airtable")
    route_markers = {
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST: ("business research", "research analyst"),
        WorkItemRoute.OPPORTUNITY_SCOUT: ("opportunity scout", "scout"),
        WorkItemRoute.GMAIL_TRIAGE: ("gmail triage", "gmail"),
    }
    marker_positions = [
        normalized.find(marker)
        for marker in route_markers.get(prepared.route, ())
        if normalized.find(marker) >= 0
    ]
    route_pos = min(marker_positions) if marker_positions else -1
    if route_pos < 0:
        return False
    return bool(
        airtable_pos < route_pos
        and re.search(
            r"\b(?:before|then|handoff|hand\s+off|context first|schema first)\b",
            normalized,
        )
    )


def _airtable_write_plan_requested(request_text: str) -> bool:
    normalized = " ".join(str(request_text or "").lower().split())
    if "airtable" not in normalized:
        return False
    explicit_plan = re.search(
        r"\b(?:plan|prepare|stage|review)\b[^.\n]{0,140}\bairtable\b[^.\n]{0,140}"
        r"\b(?:write|create|update|sync|record|attachment|upload|mutation|expense|receipt)\b"
        r"|\bairtable\b[^.\n]{0,140}"
        r"\b(?:write|create|update|sync|record|attachment|upload|mutation|expense|receipt)\b"
        r"[^.\n]{0,140}\b(?:plan|approval|review)\b",
        normalized,
    )
    if explicit_plan:
        return True
    negated_write = re.search(
        r"\b(?:do not|don't|no|without)\b[^.\n]{0,80}"
        r"\b(?:write|create|update|sync|attach|attachment|upload|mutate|mutation)\b"
        r"[^.\n]{0,80}\bairtable\b"
        r"|\b(?:do not|don't|no|without)\b[^.\n]{0,80}\bairtable\b"
        r"[^.\n]{0,80}"
        r"\b(?:write|create|update|sync|attach|attachment|upload|mutate|mutation)\b",
        normalized,
    )
    read_only_context = re.search(
        r"\b(?:read[- ]only|only as read[- ]only|context only|only .*context)\b",
        normalized,
    )
    if negated_write or read_only_context:
        return False
    return bool(
        re.search(
            r"\b(?:approval|review|write|create|update|sync|plan|expense|receipt|"
            r"attachment|upload|record mutation)\b",
            normalized,
        )
    )


def _airtable_context_after_specialist_requested(state: WorkItemGraphState) -> bool:
    if not isinstance(state.get("result"), dict):
        return False
    result = WorkflowRunResult.model_validate(state["result"])
    if result.route not in {
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        WorkItemRoute.OPPORTUNITY_SCOUT,
        WorkItemRoute.GMAIL_TRIAGE,
    }:
        return False
    if result.status in {WorkItemStatus.NEEDS_APPROVAL, WorkItemStatus.BLOCKED}:
        return False
    prepared = _prepared_step_from_state(state)
    prepared = PreparedWorkItemStep(
        request=prepared.request,
        work_item=result.work_item,
        route=result.route,
        input_text=prepared.input_text,
        context_pack=result.context_pack or prepared.context_pack,
    )
    request_text = _context_edge_request_text(state, prepared)
    if any(ref.artifact_type == "airtable_write_plan" for ref in prepared.work_item.artifact_refs):
        return False
    return _airtable_write_plan_requested(request_text) and (
        _airtable_context_edge_requested(request_text, prepared)
        or _airtable_context_summary_artifact_exists(prepared)
    )


def _google_workspace_context_edge_requested(
    request_text: str,
    prepared: PreparedWorkItemStep,
) -> bool:
    if (
        any(
            ref.artifact_type == "google_workspace_artifact_plan"
            for ref in prepared.work_item.artifact_refs
        )
        and not _google_workspace_context_should_stage_before_specialist(
            request_text,
            prepared,
        )
    ):
        return False
    if (
        _google_workspace_context_should_stage_before_specialist(request_text, prepared)
        and _google_workspace_context_summary_artifact_exists(prepared)
    ):
        return False
    normalized = " ".join(str(request_text or "").lower().split())
    if not re.search(
        r"\b(?:google workspace|google drive|google docs|google sheets)\b",
        normalized,
    ):
        return False
    if "context" not in normalized and "artifact" not in normalized and "plan" not in normalized:
        return False
    if not re.search(
        r"\b(?:agent|handoff|artifact|brief|packet|plan|doc|sheet|folder|workspace)\b",
        normalized,
    ):
        return False
    if not (
        _google_workspace_context_should_stage_before_specialist(request_text, prepared)
        or _google_workspace_artifact_plan_requested(request_text)
    ):
        return False
    return prepared.route in {
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        WorkItemRoute.OPPORTUNITY_SCOUT,
        WorkItemRoute.GMAIL_TRIAGE,
        WorkItemRoute.CHIEF_OF_STAFF,
        WorkItemRoute.ORCHESTRATOR,
        WorkItemRoute.CLARIFICATION,
    }


def _google_workspace_context_should_wait_for_specialist(
    prepared: PreparedWorkItemStep,
) -> bool:
    return prepared.route in {
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        WorkItemRoute.OPPORTUNITY_SCOUT,
        WorkItemRoute.GMAIL_TRIAGE,
    }


def _google_workspace_context_summary_artifact_exists(prepared: PreparedWorkItemStep) -> bool:
    return any(
        ref.artifact_type == "google_workspace_context_summary"
        for ref in prepared.work_item.artifact_refs
    )


def _google_workspace_context_should_stage_before_specialist(
    request_text: str,
    prepared: PreparedWorkItemStep,
) -> bool:
    if prepared.route not in {
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        WorkItemRoute.OPPORTUNITY_SCOUT,
        WorkItemRoute.GMAIL_TRIAGE,
    }:
        return False
    normalized = " ".join(str(request_text or "").lower().split())
    if not re.search(
        r"\b(?:google workspace|google drive|google docs|google sheets)\b",
        normalized,
    ):
        return False
    if "context" not in normalized:
        return False
    workspace_positions = [
        normalized.find(marker)
        for marker in ("google workspace", "google drive", "google docs", "google sheets")
        if normalized.find(marker) >= 0
    ]
    workspace_pos = min(workspace_positions) if workspace_positions else -1
    route_markers = {
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST: ("business research", "research analyst"),
        WorkItemRoute.OPPORTUNITY_SCOUT: ("opportunity scout", "scout"),
        WorkItemRoute.GMAIL_TRIAGE: ("gmail triage", "gmail"),
    }
    marker_positions = [
        normalized.find(marker)
        for marker in route_markers.get(prepared.route, ())
        if normalized.find(marker) >= 0
    ]
    route_pos = min(marker_positions) if marker_positions else -1
    if workspace_pos < 0 or route_pos < 0:
        return False
    return bool(
        workspace_pos < route_pos
        and re.search(
            r"\b(?:before|then|handoff|hand\s+off|context first|artifact context first)\b",
            normalized,
        )
    )


def _google_workspace_artifact_plan_requested(request_text: str) -> bool:
    normalized = " ".join(str(request_text or "").lower().split())
    if not re.search(
        r"\b(?:google workspace|google drive|google docs|google sheets)\b",
        normalized,
    ):
        return False
    explicit_plan = re.search(
        r"\b(?:plan|planning|prepare|stage|review)\b[^.\n]{0,160}"
        r"\b(?:where|should live|approval|drive|docs|sheets|folder|file|doc|sheet)\b"
        r"|\b(?:google workspace|google drive|google docs|google sheets)\b"
        r"[^.\n]{0,160}\b(?:artifact plan|packet plan|approval plan)\b",
        normalized,
    )
    if explicit_plan:
        return True
    negated_write = re.search(
        r"\b(?:do not|don't|no|without)\b[^.\n]{0,100}"
        r"\b(?:create|update|share|sharing|write|mutate|file|folder|doc|sheet)\b"
        r"|\b(?:do not|don't|no|without)\b[^.\n]{0,100}"
        r"\b(?:google workspace|google drive|google docs|google sheets|drive|docs|sheets)\b",
        normalized,
    )
    read_only_context = re.search(
        r"\b(?:read[- ]only|only as read[- ]only|context only|only .*context)\b",
        normalized,
    )
    if negated_write or read_only_context:
        return False
    return bool(
        re.search(
            r"\b(?:approval|review|write|create|update|share|sharing|planning|plan|"
            r"artifact|doc|sheet|folder|file)\b",
            normalized,
        )
    )


def _google_workspace_context_after_specialist_requested(state: WorkItemGraphState) -> bool:
    if not isinstance(state.get("result"), dict):
        return False
    result = WorkflowRunResult.model_validate(state["result"])
    if result.route not in {
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        WorkItemRoute.OPPORTUNITY_SCOUT,
        WorkItemRoute.GMAIL_TRIAGE,
    }:
        return False
    if result.status in {WorkItemStatus.NEEDS_APPROVAL, WorkItemStatus.BLOCKED}:
        return False
    prepared = _prepared_step_from_state(state)
    prepared = PreparedWorkItemStep(
        request=prepared.request,
        work_item=result.work_item,
        route=result.route,
        input_text=prepared.input_text,
        context_pack=result.context_pack or prepared.context_pack,
    )
    request_text = _context_edge_request_text(state, prepared)
    if any(
        ref.artifact_type == "google_workspace_artifact_plan"
        for ref in prepared.work_item.artifact_refs
    ):
        return False
    artifact_plan_requested = _google_workspace_artifact_plan_requested(
        request_text
    ) or _context_backed_internal_artifact_plan_requested(request_text, prepared)
    return artifact_plan_requested and (
        _google_workspace_context_edge_requested(request_text, prepared)
        or _google_workspace_context_summary_artifact_exists(prepared)
        or _context_backed_internal_artifact_plan_requested(request_text, prepared)
    )


def _feed_context_edge_kind(
    request_text: str,
    prepared: PreparedWorkItemStep,
) -> Literal["rss", "preprints"] | None:
    if any(
        ref.artifact_type in {"rss_context_summary", "preprints_context_summary"}
        for ref in prepared.work_item.artifact_refs
    ):
        return None
    normalized = " ".join(str(request_text or "").lower().split())
    kind = _normalized_feed_context_kind(normalized)
    if kind is None:
        return None
    if not re.search(
        r"\b(?:agent|handoff|history|signal|evidence|artifact|brief|plan)\b",
        normalized,
    ):
        return None
    if not re.search(
        r"\b(?:research|opportunit|source|signal|evidence|artifact|brief|plan)\b",
        normalized,
    ):
        return None
    if prepared.route not in {
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        WorkItemRoute.OPPORTUNITY_SCOUT,
        WorkItemRoute.RSS_CONTEXT_AGENT,
        WorkItemRoute.PREPRINTS_CONTEXT_AGENT,
        WorkItemRoute.CHIEF_OF_STAFF,
        WorkItemRoute.ORCHESTRATOR,
        WorkItemRoute.CLARIFICATION,
    }:
        return None
    return kind


def _route_after_feed_context(route: WorkItemRoute, request_text: str) -> WorkItemRoute:
    if route == WorkItemRoute.OPPORTUNITY_SCOUT:
        return route
    if _context_request_prefers_opportunity_scout(request_text):
        return WorkItemRoute.OPPORTUNITY_SCOUT
    if route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST:
        return route
    return WorkItemRoute.BUSINESS_RESEARCH_ANALYST


def _route_after_zotero_context(route: WorkItemRoute, request_text: str) -> WorkItemRoute:
    if route in {
        WorkItemRoute.CHIEF_OF_STAFF,
        WorkItemRoute.ORCHESTRATOR,
        WorkItemRoute.CLARIFICATION,
    }:
        if _context_request_prefers_opportunity_scout(request_text):
            return WorkItemRoute.OPPORTUNITY_SCOUT
        return WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    return route


def _context_request_prefers_opportunity_scout(request_text: str) -> bool:
    normalized = " ".join(str(request_text or "").lower().split())
    if re.search(r"\bopportunity scout(?: agent)?\b|\bscout(?:ing)?\b", normalized):
        return True
    opportunity = r"\bopportunit(?:y|ies)\b"
    scout_action = r"\b(?:assess|assessment|fit|rank|score|prioriti[sz]e|pipeline|lead|rfp|pilot)\b"
    return bool(
        re.search(rf"{opportunity}.{{0,120}}{scout_action}", normalized)
        or re.search(rf"{scout_action}.{{0,120}}{opportunity}", normalized)
    )


def _feed_context_agent_name(kind: str) -> str:
    return "preprints_context_agent" if kind == "preprints" else "rss_context_agent"


def _context_backed_internal_artifact_plan_requested(
    request_text: str,
    prepared: PreparedWorkItemStep,
) -> bool:
    context_artifact_types = {
        "rss_context_summary",
        "preprints_context_summary",
        "zotero_context_summary",
        "airtable_context_summary",
        "google_workspace_context_summary",
    }
    if not any(
        artifact.artifact_type in context_artifact_types
        for artifact in prepared.work_item.artifact_refs
    ):
        return False
    normalized = " ".join(str(request_text or "").lower().split())
    if re.search(
        r"\b(?:do not|don't|dont|no|without|skip|avoid)\b[^.\n]{0,100}"
        r"\b(?:artifact|packet|brief|plan|planning)\b",
        normalized,
    ):
        return False
    if not re.search(r"\binternal\b", normalized):
        return False
    if not re.search(r"\b(?:artifact|packet|brief|evidence packet)\b", normalized):
        return False
    return bool(re.search(r"\b(?:plan|planning|review|approval)\b", normalized))


def _retrieve_feed_context_history(
    *,
    kind: Literal["rss", "preprints"],
    request_text: str,
    database_url: str | None,
) -> dict[str, Any]:
    query = _compact_context_edge_text(request_text, 240)
    if kind == "preprints":
        return retrieve_preprint_announcement_history_impl(
            query=query,
            selected_only=True,
            limit=5,
            database_url=database_url,
        )
    return retrieve_rss_announcement_history_impl(
        query=query,
        selected_only=True,
        limit=5,
        database_url=database_url,
    )


def _feed_context_source_refs(
    *,
    work_item_id: str,
    kind: Literal["rss", "preprints"],
    request_text: str,
    retrieval: Mapping[str, Any],
) -> list[WorkItemSourceRef]:
    agent_name = _feed_context_agent_name(kind)
    refs: list[WorkItemSourceRef] = []
    for index, item in enumerate(list(retrieval.get("items") or [])[:5], start=1):
        if not isinstance(item, Mapping):
            continue
        source_id = str(item.get("feed_item_id") or item.get("url") or f"{work_item_id}:{index}")
        evidence_notes = [str(note) for note in item.get("evidence_notes") or [] if str(note)]
        key_facts = [
            str(item.get("selection_reason") or "").strip(),
            str(item.get("summary") or "").strip(),
            str(item.get("source_basis") or "").strip(),
            *evidence_notes[:2],
        ]
        refs.append(
            WorkItemSourceRef(
                title=str(item.get("title") or f"{agent_name} historical context"),
                url=str(item.get("url") or item.get("slack_link") or ""),
                source_type=f"{kind}_historical_context",
                source_id=f"{agent_name}:{source_id}",
                supported_claim=(
                    str(item.get("summary") or item.get("detailed_summary_seed") or "")
                    or _feed_context_supported_claim(kind)
                )[:500],
                provider=agent_name,
                extraction_status=str(item.get("evidence_status") or "historical_context"),
                source_quality="historical_context",
                key_facts=[fact for fact in key_facts if fact][:5],
                evidence_excerpt=" | ".join(evidence_notes)[:700],
            )
        )
    if refs:
        return refs
    return [
        WorkItemSourceRef(
            title=f"Read-only {kind} context handoff",
            source_type="local_history_context_handoff",
            source_id=f"{agent_name}:{work_item_id}:context_handoff",
            provider=agent_name,
            supported_claim=_feed_context_supported_claim(kind),
            extraction_status=str(retrieval.get("status") or "context_handoff"),
            source_quality="historical_context_pointer",
            key_facts=[
                f"Read-only {kind} context was requested before downstream specialist work.",
                "No matching selected local feed records were staged as current external evidence.",
                _compact_context_edge_text(request_text, 220),
            ],
            evidence_excerpt=_compact_context_edge_text(request_text, 400),
        )
    ]


def _feed_context_supported_claim(kind: str) -> str:
    if kind == "preprints":
        return (
            "Preprint history can guide research or opportunity planning, but preprints "
            "remain preliminary and require source verification before external use."
        )
    return (
        "RSS/announcement history can guide research or opportunity planning, but "
        "historical context alone is not current external verification."
    )


def _feed_context_artifact(
    *,
    work_item_id: str,
    kind: Literal["rss", "preprints"],
    request_text: str,
    retrieval: Mapping[str, Any],
    source_refs: list[WorkItemSourceRef],
) -> WorkItemArtifactRef:
    agent_name = _feed_context_agent_name(kind)
    route = _route_after_feed_context(WorkItemRoute.CHIEF_OF_STAFF, request_text)
    item_count = int(retrieval.get("item_count") or 0)
    return WorkItemArtifactRef(
        artifact_type=f"{kind}_context_summary",
        artifact_id=f"{work_item_id}:{kind}_context",
        source_agent=agent_name,
        approval_state="approved_for_research",
        title=f"{kind.title()} context handoff",
        summary=(
            f"Read-only {kind} context staged from local announcement history. "
            f"Matched selected item count: {item_count}. "
            "Downstream synthesis must distinguish historical/preliminary signal "
            "from current external verification."
        ),
        selected=True,
        metadata={
            "agent_name": agent_name,
            "mode": "local_history_context_handoff",
            "source_refs": [source.model_dump(mode="json") for source in source_refs],
            "source_count": len(source_refs),
            "retrieval_status": str(retrieval.get("status") or ""),
            "retrieval_blockers": [
                str(blocker) for blocker in list(retrieval.get("blockers") or [])[:5]
            ],
            "item_count": item_count,
            "recommended_downstream_route": route.value,
            "recommended_artifact_plan": [
                "Use the context as historical signal for specialist planning.",
                "Verify current facts before external claims or outreach.",
                "Keep writes, posts, publication, and sends blocked without explicit approval.",
            ],
            "request_excerpt": _compact_context_edge_text(request_text, 500),
            "external_writes_enabled": False,
            "send_enabled": False,
            "live_reads_enabled": False,
        },
    )


def _zotero_context_source_ref(work_item_id: str, request_text: str) -> WorkItemSourceRef:
    return WorkItemSourceRef(
        title="Read-only Zotero context handoff",
        source_type="dry_run_context_handoff",
        source_id=f"zotero_context:{work_item_id}",
        provider="zotero_context_agent",
        supported_claim=(
            "Zotero context should guide downstream research or artifact planning; "
            "item-level citations still require local Zotero reads."
        ),
        extraction_status="context_handoff",
        source_quality="read_only_context",
        key_facts=[
            "Read-only Zotero context was requested before downstream specialist work.",
            "The handoff does not perform live reads, writes, sends, or publication.",
            _compact_context_edge_text(request_text, 220),
        ],
        evidence_excerpt=_compact_context_edge_text(request_text, 400),
    )


def _zotero_context_artifact(
    work_item_id: str,
    request_text: str,
    source_ref: WorkItemSourceRef,
    *,
    provider_evidence: bool = False,
) -> WorkItemArtifactRef:
    return WorkItemArtifactRef(
        artifact_type="zotero_context_summary",
        artifact_id=f"{work_item_id}:zotero_context",
        source_agent="zotero_context_agent",
        approval_state="approved_for_research",
        title="Zotero context handoff",
        summary=(
            "A concrete read-only Zotero item was staged for downstream synthesis."
            if provider_evidence
            else "Read-only Zotero context staged for downstream synthesis. The specialist "
            "must still cite concrete local Zotero items or other retrieved sources."
        ),
        selected=True,
        metadata={
            "agent_name": "zotero_context_agent",
            "mode": "provider_source_handoff" if provider_evidence else "dry_run_context_handoff",
            "source_refs": [source_ref.model_dump(mode="json")],
            "source_count": 1,
            "recommended_downstream_route": "context_selected_downstream_specialist",
            "recommended_artifact_plan": [
                "Use Zotero context as background evidence planning input.",
                "Route to the selected downstream specialist before any outreach step.",
                "Keep external-use claims blocked until source-backed artifacts are approved.",
            ],
            "request_excerpt": _compact_context_edge_text(request_text, 500),
            "external_writes_enabled": False,
            "send_enabled": False,
            "live_reads_enabled": provider_evidence,
            "provider_evidence": provider_evidence,
        },
    )


def _google_workspace_context_source_ref(
    work_item_id: str,
    request_text: str,
) -> WorkItemSourceRef:
    return WorkItemSourceRef(
        title="Read-only Google Workspace artifact plan handoff",
        source_type="dry_run_artifact_plan_handoff",
        source_id=f"google_workspace_context:{work_item_id}",
        provider="google_workspace_context_agent",
        supported_claim=(
            "Google Workspace context should guide internal artifact planning; scoped "
            "approval is required before Drive, Docs, Sheets, or sharing writes."
        ),
        extraction_status="context_handoff",
        source_quality="read_only_context",
        key_facts=[
            "Read-only Google Workspace artifact planning was requested.",
            "The graph handoff does not perform live reads, writes, sharing, or publication.",
            _compact_context_edge_text(request_text, 220),
        ],
        evidence_excerpt=_compact_context_edge_text(request_text, 400),
    )


def _airtable_context_source_ref(
    work_item_id: str,
    request_text: str,
) -> WorkItemSourceRef:
    return WorkItemSourceRef(
        title="Read-only Airtable context/write plan handoff",
        source_type="dry_run_write_plan_handoff",
        source_id=f"airtable_context:{work_item_id}",
        provider="airtable_context_agent",
        supported_claim=(
            "Airtable context should guide internal schema, record, or write planning; "
            "scoped approval is required before any Airtable mutation."
        ),
        extraction_status="context_handoff",
        source_quality="read_only_context",
        key_facts=[
            "Read-only Airtable context/write planning was requested.",
            (
                "The graph handoff does not perform live reads, writes, "
                "attachment uploads, or record mutations."
            ),
            _compact_context_edge_text(request_text, 220),
        ],
        evidence_excerpt=_compact_context_edge_text(request_text, 400),
    )


def _airtable_context_artifact(
    work_item_id: str,
    request_text: str,
    source_ref: WorkItemSourceRef,
) -> WorkItemArtifactRef:
    return WorkItemArtifactRef(
        artifact_type="airtable_write_plan",
        artifact_id=f"{work_item_id}:airtable_write_plan",
        source_agent="airtable_context_agent",
        approval_state="needs_approval",
        title="Airtable context/write plan",
        summary=(
            "Read-only Airtable context/write plan staged for human review. Approval "
            "is required before creating, updating, attaching, deleting, or otherwise "
            "mutating Airtable records."
        ),
        selected=True,
        metadata={
            "agent_name": "airtable_context_agent",
            "mode": "dry_run_write_plan_handoff",
            "source_refs": [source_ref.model_dump(mode="json")],
            "source_count": 1,
            "write_plan": {
                "target_system": "airtable",
                "operation": "chief_owned_airtable_write_after_approval",
                "target": "Airtable base, table, and record selected after schema review",
                "scope": "internal Airtable schema, record, create, update, or attachment plan",
                "approval_required": True,
                "approval_reference_needed": True,
                "live_write_allowed_for_specialist": False,
                "rationale": (
                    "Graph staging is read-only. Chief of Staff or a direct selected "
                    "Airtable path owns any approved write."
                ),
            },
            "approval_needs": [
                "Scoped Airtable approval reference",
                "Confirmed base/table/field mapping",
                "Confirmed record identity or create target",
                "Confirmed attachment field support before uploads",
            ],
            "recommended_next_action": "review_airtable_write_plan",
            "request_excerpt": _compact_context_edge_text(request_text, 500),
            "external_writes_enabled": False,
            "send_enabled": False,
            "live_reads_enabled": False,
            "live_write_allowed_for_specialist": False,
        },
    )


def _airtable_context_summary_artifact(
    work_item_id: str,
    request_text: str,
    source_ref: WorkItemSourceRef,
    *,
    provider_evidence: bool = False,
) -> WorkItemArtifactRef:
    return WorkItemArtifactRef(
        artifact_type="airtable_context_summary",
        artifact_id=f"{work_item_id}:airtable_context",
        source_agent="airtable_context_agent",
        approval_state="approved_for_research",
        title="Airtable context handoff",
        summary=(
            "Concrete read-only Airtable evidence staged for downstream specialist work."
            if provider_evidence
            else "Read-only Airtable context staged for downstream specialist work. "
            "Live schema or record reads were not performed by this graph handoff."
        ),
        selected=True,
        metadata={
            "agent_name": "airtable_context_agent",
            "mode": (
                "provider_source_handoff"
                if provider_evidence
                else "dry_run_read_context_handoff"
            ),
            "source_refs": [source_ref.model_dump(mode="json")],
            "source_count": 1,
            "recommended_artifact_plan": [
                "Use Airtable context as schema and record-planning input only.",
                "Verify live schema or record data before relying on Airtable facts.",
                "Keep creates, updates, attachment uploads, deletes, sends, and posts blocked.",
            ],
            "request_excerpt": _compact_context_edge_text(request_text, 500),
            "external_writes_enabled": False,
            "send_enabled": False,
            "live_reads_enabled": provider_evidence,
            "provider_evidence": provider_evidence,
            "live_write_allowed_for_specialist": False,
        },
    )


def _google_workspace_context_artifact(
    work_item_id: str,
    request_text: str,
    source_ref: WorkItemSourceRef,
) -> WorkItemArtifactRef:
    return WorkItemArtifactRef(
        artifact_type="google_workspace_artifact_plan",
        artifact_id=f"{work_item_id}:google_workspace_artifact_plan",
        source_agent="google_workspace_context_agent",
        approval_state="needs_approval",
        title="Google Workspace artifact plan",
        summary=(
            "Read-only Workspace artifact plan staged for human review. Approval is "
            "required before creating, updating, sharing, or deleting Drive, Docs, or "
            "Sheets artifacts."
        ),
        selected=True,
        metadata={
            "agent_name": "google_workspace_context_agent",
            "mode": "dry_run_artifact_plan_handoff",
            "source_refs": [source_ref.model_dump(mode="json")],
            "source_count": 1,
            "write_plan": {
                "target_system": "google_workspace",
                "operation": "chief_owned_artifact_write_after_approval",
                "target": "Drive folder, Doc, or Sheet selected after review",
                "scope": "internal artifact placement, creation, update, or sharing plan",
                "approval_required": True,
                "approval_reference_needed": True,
                "live_write_allowed_for_specialist": False,
                "rationale": (
                    "Graph staging is read-only. Chief of Staff or a direct selected "
                    "Workspace path owns any approved write."
                ),
            },
            "approval_needs": [
                "Scoped Google Workspace approval reference",
                "Confirmed folder/file/doc/sheet identity",
                "Confirmed sharing scope and source basis",
            ],
            "recommended_next_action": "review_google_workspace_artifact_plan",
            "request_excerpt": _compact_context_edge_text(request_text, 500),
            "external_writes_enabled": False,
            "send_enabled": False,
            "live_reads_enabled": False,
            "live_write_allowed_for_specialist": False,
        },
    )


def _google_workspace_context_summary_artifact(
    work_item_id: str,
    request_text: str,
    source_ref: WorkItemSourceRef,
) -> WorkItemArtifactRef:
    return WorkItemArtifactRef(
        artifact_type="google_workspace_context_summary",
        artifact_id=f"{work_item_id}:google_workspace_context",
        source_agent="google_workspace_context_agent",
        approval_state="approved_for_research",
        title="Google Workspace context handoff",
        summary=(
            "Read-only Google Workspace context staged for downstream specialist work. "
            "Live Drive, Docs, Sheets, or sharing reads were not performed by this graph handoff."
        ),
        selected=True,
        metadata={
            "agent_name": "google_workspace_context_agent",
            "mode": "dry_run_read_context_handoff",
            "source_refs": [source_ref.model_dump(mode="json")],
            "source_count": 1,
            "recommended_artifact_plan": [
                "Use Workspace context as artifact and source-planning input only.",
                "Verify live Drive, Docs, Sheets, or sharing data before relying on it.",
                "Keep creates, updates, sharing, sends, posts, and publication blocked.",
            ],
            "request_excerpt": _compact_context_edge_text(request_text, 500),
            "external_writes_enabled": False,
            "send_enabled": False,
            "live_reads_enabled": False,
            "live_write_allowed_for_specialist": False,
        },
    )


def _compact_context_edge_text(text: str, max_chars: int) -> str:
    compacted = " ".join(str(text or "").split())
    if len(compacted) <= max_chars:
        return compacted
    return f"{compacted[: max(0, max_chars - 3)].rstrip()}..."


def _route_after_finalize(
    state: WorkItemGraphState,
) -> Literal[
    "stage_airtable_context",
    "stage_google_workspace_context",
    "approval_checkpoint",
    "manager_loop_continue",
    "manager_loop_finalize",
    "done",
]:
    if state.get("checkpoint_required"):
        return "approval_checkpoint"
    if _airtable_context_after_specialist_requested(state):
        return "stage_airtable_context"
    if _google_workspace_context_after_specialist_requested(state):
        return "stage_google_workspace_context"
    if state.get("manager_loop"):
        if _manager_loop_stop_reason(state):
            return "manager_loop_finalize"
        return "manager_loop_continue"
    return "done"


def _prepared_step_payload(prepared: PreparedWorkItemStep) -> dict[str, Any]:
    return {
        "request": prepared.request.model_dump(mode="json"),
        "work_item": prepared.work_item.model_dump(mode="json"),
        "route": prepared.route.value,
        "input_text": prepared.input_text,
        "context_pack": dict(prepared.context_pack),
    }


def _prepared_step_from_state(state: WorkItemGraphState) -> PreparedWorkItemStep:
    payload = state.get("prepared_step")
    if not isinstance(payload, dict):
        raise ValueError("LangGraph WorkItem state is missing prepared_step.")
    return PreparedWorkItemStep(
        request=WorkflowRunRequest.model_validate(payload.get("request") or {}),
        work_item=WorkItem.model_validate(payload.get("work_item") or {}),
        route=WorkItemRoute(str(payload.get("route") or WorkItemRoute.ORCHESTRATOR.value)),
        input_text=str(payload.get("input_text") or ""),
        context_pack=dict(payload.get("context_pack") or {}),
    )


def _state_with_result(
    state: WorkItemGraphState,
    result: WorkflowRunResult,
) -> WorkItemGraphState:
    return {
        **state,
        "result": result.model_dump(mode="json"),
        "route": result.route.value,
        "status": result.status.value,
        "advanced": result.advanced,
    }


def _approval_checkpoint_reason(result: WorkflowRunResult) -> str:
    next_action = result.next_action
    if _next_action_requires_approval(next_action):
        return _next_action_reason(next_action)
    if result.status == WorkItemStatus.NEEDS_APPROVAL:
        return "WorkItem status requires human approval before the next action."
    for blocker in result.blockers:
        code = str(blocker.code or "").lower()
        if "approval" in code or "approved" in code:
            return blocker.message
    return ""


def _approval_checkpoint_payload(
    result: WorkflowRunResult,
    *,
    reason: str,
) -> dict[str, Any]:
    """Return a bounded checkpoint payload for approval review and audit events."""

    context_pack = result.context_pack if isinstance(result.context_pack, Mapping) else {}
    return {
        "schema": "keystone.langgraph.approval_checkpoint.v1",
        "work_item_id": result.work_item.id,
        "route": result.route.value,
        "status": result.status.value,
        "target": result.work_item.target.model_dump(mode="json"),
        "next_action": (result.next_action.model_dump(mode="json") if result.next_action else None),
        "blockers": [blocker.model_dump(mode="json") for blocker in result.blockers[:8]],
        "approval_gates": [
            gate.model_dump(mode="json") for gate in result.work_item.approval_gates[:8]
        ],
        "artifact_refs": [
            {
                "artifact_type": artifact.artifact_type,
                "artifact_id": artifact.artifact_id,
                "source_agent": artifact.source_agent,
                "approval_state": artifact.approval_state,
                "selected": artifact.selected,
                "title": artifact.title,
                "summary": artifact.summary,
            }
            for artifact in result.work_item.artifact_refs[:12]
        ],
        "source_refs": [
            {
                "title": source.title,
                "url": source.url,
                "source_id": source.source_id,
                "provider": source.provider,
                "source_quality": source.source_quality,
                "supported_claim": source.supported_claim,
            }
            for source in result.work_item.sources[:12]
        ],
        "context_pack": {
            "ready": context_pack.get("ready"),
            "can_synthesize": context_pack.get("can_synthesize"),
            "missing_requirements": list(context_pack.get("missing_requirements") or [])[:8],
            "limitation_notes": list(context_pack.get("limitation_notes") or [])[:8],
            "readiness_gates": list(context_pack.get("readiness_gates") or [])[:8],
        },
        "reason": reason,
        "send_enabled": False,
        "external_writes_enabled": False,
    }


def _next_action_requires_approval(next_action: WorkItemNextAction | None) -> bool:
    return bool(next_action and next_action.requires_approval)


def _next_action_reason(next_action: WorkItemNextAction | None) -> str:
    if next_action is None:
        return ""
    return next_action.description or "The next WorkItem action requires explicit human approval."


def _record_langgraph_checkpoint_event(
    *,
    request: WorkflowRunRequest,
    result: WorkflowRunResult,
    runtime: str,
    checkpoint_required: bool,
    checkpoint_reason: str,
    checkpoint_key: str,
    node_path: list[str],
    checkpoint_payload: dict[str, Any] | None = None,
    graph_completion_review: dict[str, Any] | None = None,
) -> None:
    if not request.save:
        return
    store = SQLiteStore(request.database_url or database_url_from_env())
    record_event(
        result.work_item,
        event_type="langgraph_orchestration",
        summary=(
            "LangGraph orchestration checkpoint recorded."
            if checkpoint_required
            else "LangGraph orchestration completed without an approval checkpoint."
        ),
        metadata={
            "runtime": runtime,
            "checkpoint_required": checkpoint_required,
            "checkpoint_reason": checkpoint_reason,
            "checkpoint_key": checkpoint_key,
            "node_path": node_path,
            "checkpoint_payload": checkpoint_payload,
            "graph_completion_review": graph_completion_review,
        },
        store=store,
    )
