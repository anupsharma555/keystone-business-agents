"""Optional LangGraph orchestration for durable WorkItem advancement.

The graph layer intentionally wraps the existing WorkItem runner. It does not
replace SDK agents, prompts, schemas, tool wrappers, or Python safety gates.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from importlib.util import find_spec
from typing import Any, Literal, TypedDict
from uuid import uuid4

from pydantic import BaseModel, Field

from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkflowRunResult,
    WorkItemNextAction,
    WorkItemStatus,
)
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env
from keystone_agents.work_items import record_event
from keystone_agents.workflow_runner import advance_work_item

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
    improvements: list[str]


class LangGraphWorkflowOutcome(BaseModel):
    """Execution envelope for the optional LangGraph WorkItem runtime."""

    request: WorkflowRunRequest
    result: WorkflowRunResult
    graph_runtime: Literal["langgraph", "dependency_free_fallback"]
    graph_available: bool
    checkpoint_required: bool = False
    checkpoint_reason: str = ""
    node_path: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    checkpoint_key: str = ""


def langgraph_available() -> bool:
    """Return whether the optional LangGraph package can be imported."""

    return find_spec("langgraph") is not None


def work_item_langgraph_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether WorkItem calls should default to the LangGraph wrapper."""

    source = env if env is not None else os.environ
    for key in LANGGRAPH_WORKITEM_ENV_KEYS:
        value = source.get(key)
        if value is None:
            continue
        if value.strip().lower() not in FALSE_VALUES:
            return True
    return False


def work_item_graph_thread_id(work_item_id: str) -> str:
    """Return the stable LangGraph thread id for one WorkItem."""

    cleaned = str(work_item_id or "").strip()
    if not cleaned:
        return ""
    return f"work-item:{cleaned}"


def langgraph_functionality_improvements() -> list[str]:
    """Describe the concrete workflow improvements this layer adds."""

    return [
        "Adds an explicit node boundary around WorkItem advancement.",
        "Creates a durable checkpoint seam before approval-gated next actions.",
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
    builder.add_node("advance_work_item", _advance_work_item_node)
    builder.add_node("approval_checkpoint", _approval_checkpoint_node)
    builder.add_edge(START, "advance_work_item")
    builder.add_conditional_edges(
        "advance_work_item",
        _route_after_advance,
        {
            "approval_checkpoint": "approval_checkpoint",
            "done": END,
        },
    )
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
) -> LangGraphWorkflowOutcome:
    """Advance a WorkItem through the optional LangGraph orchestration layer."""

    initial_state: WorkItemGraphState = {
        "request": request.model_dump(mode="json"),
        "node_path": [],
        "enable_interrupts": enable_interrupts,
        "improvements": langgraph_functionality_improvements(),
    }
    checkpoint_key = thread_id or f"work-item-graph-{uuid4().hex}"
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
    _record_langgraph_checkpoint_event(
        request=request,
        result=result,
        runtime=runtime,
        checkpoint_required=checkpoint_required,
        checkpoint_reason=checkpoint_reason,
        checkpoint_key=checkpoint_key,
        node_path=list(final_state.get("node_path", [])),
    )
    return LangGraphWorkflowOutcome(
        request=request,
        result=result,
        graph_runtime=runtime,
        graph_available=(runtime == "langgraph"),
        checkpoint_required=checkpoint_required,
        checkpoint_reason=checkpoint_reason,
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
    """Advance a WorkItem directly or through LangGraph based on explicit opt-in."""

    enabled = work_item_langgraph_enabled() if use_langgraph is None else bool(use_langgraph)
    if not enabled:
        return advance_work_item(request)
    graph_thread_id = thread_id or work_item_graph_thread_id(request.work_item_id or "")
    outcome = run_work_item_langgraph(
        request,
        thread_id=graph_thread_id or None,
        require_langgraph=require_langgraph,
    )
    return outcome.result


def next_langgraph_node_for_result(
    result: WorkflowRunResult,
) -> Literal["approval_checkpoint", "done"]:
    """Return the next graph node implied by a WorkItem advancement result."""

    if _approval_checkpoint_reason(result):
        return "approval_checkpoint"
    return "done"


def _run_dependency_free_graph(state: WorkItemGraphState) -> WorkItemGraphState:
    state = _advance_work_item_node(state)
    if _route_after_advance(state) == "approval_checkpoint":
        state = _approval_checkpoint_node(state)
    return state


def _advance_work_item_node(state: WorkItemGraphState) -> WorkItemGraphState:
    request = WorkflowRunRequest.model_validate(state.get("request") or {})
    result = advance_work_item(request)
    checkpoint_reason = _approval_checkpoint_reason(result)
    return {
        **state,
        "result": result.model_dump(mode="json"),
        "route": result.route.value,
        "status": result.status.value,
        "advanced": result.advanced,
        "node_path": [*state.get("node_path", []), "advance_work_item"],
        "checkpoint_required": bool(checkpoint_reason),
        "checkpoint_reason": checkpoint_reason,
        "improvements": state.get("improvements") or langgraph_functionality_improvements(),
    }


def _approval_checkpoint_node(state: WorkItemGraphState) -> WorkItemGraphState:
    result = WorkflowRunResult.model_validate(state["result"])
    payload = {
        "work_item_id": result.work_item.id,
        "route": result.route.value,
        "status": result.status.value,
        "next_action": (result.next_action.model_dump(mode="json") if result.next_action else None),
        "blockers": [blocker.model_dump(mode="json") for blocker in result.blockers],
        "reason": state.get("checkpoint_reason", ""),
    }
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


def _route_after_advance(state: WorkItemGraphState) -> Literal["approval_checkpoint", "done"]:
    if state.get("checkpoint_required"):
        return "approval_checkpoint"
    return "done"


def _approval_checkpoint_reason(result: WorkflowRunResult) -> str:
    next_action = result.next_action
    if _next_action_requires_approval(next_action):
        return _next_action_reason(next_action)
    if result.status == WorkItemStatus.NEEDS_APPROVAL:
        return "WorkItem status requires human approval before the next action."
    for blocker in result.blockers:
        text = f"{blocker.code} {blocker.message}".lower()
        if "approval" in text or "approved" in text:
            return blocker.message
    return ""


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
        },
        store=store,
    )
