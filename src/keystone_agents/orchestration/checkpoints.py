"""SQLite-backed graph start, inspection, resume and isolated diagnostic forks."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from uuid import uuid4

from keystone_agents.runtime.context_snapshot import (
    freeze_workflow_context,
    restore_workflow_context,
)
from keystone_agents.runtime.durable_execution import (
    EXECUTION_SCHEMA,
    ExecutionConflict,
    ExecutionStore,
    activate_execution,
    current_execution,
    execution_database_path,
    execution_lock,
)
from keystone_agents.runtime.provenance import current_runtime_fingerprint
from keystone_agents.runtime.request_budget import current_model_request_budget_snapshot
from keystone_agents.runtime.work_item_lock import work_item_execution_lock
from keystone_agents.schemas.work_item import WorkflowRunRequest, WorkItem
from keystone_agents.storage.sqlite_store import SQLiteStore, stable_hash

GRAPH_INPUT: ContextVar[tuple[bool, Any]] = ContextVar(
    "kba_graph_resume_input", default=(False, None)
)


def _compatibility_fingerprint() -> str:
    fingerprint = current_runtime_fingerprint()
    return fingerprint.get("runtime_sha256") or fingerprint["source_sha256"]


def run_durable_direct(request: WorkflowRunRequest, runner, **options: Any):
    """Use the same operation/budget journal for non-graph WorkItem execution."""
    from keystone_agents.schemas.work_item import WorkflowRunResult

    store = ExecutionStore(execution_database_path(request.database_url))
    existing = store.find(execution_id=request.execution_id, origin=request.origin_event_id)
    request = (
        restore_workflow_context(request, json.loads(existing["request_json"])["request"])
        if existing else freeze_workflow_context(request)
    )
    budget = current_model_request_budget_snapshot() or {}
    row = store.begin(
        {
            "request": request.model_dump(mode="json"),
            "backend": "direct",
            "options": {"max_steps": options.get("max_steps", 1)},
        },
        execution_id=request.execution_id,
        origin=request.origin_event_id,
        budget_limit=request.model_request_limit
        if request.model_request_limit is not None
        else budget.get("remaining"),
        source_fingerprint=_compatibility_fingerprint(),
    )
    execution_id = row["id"]
    if row["status"] == "completed" and row["result_json"]:
        return WorkflowRunResult.model_validate_json(row["result_json"])
    with execution_lock(
        store.path.with_name(f"{store.path.name}.{stable_hash(execution_id)[:20]}.lock")
    ):
        latest = store.get(execution_id)
        if latest["status"] == "completed" and latest["result_json"]:
            return WorkflowRunResult.model_validate_json(latest["result_json"])
        if latest["source_fingerprint"] != _compatibility_fingerprint():
            raise ExecutionConflict(
                "Execution runtime changed; review before replaying direct work."
            )
        with (
            work_item_execution_lock(
                request.database_url,
                request.work_item_id or latest["work_item_id"],
                owner=execution_id,
            ),
            activate_execution(store, execution_id),
        ):
            store.update(execution_id, status="running")
            try:
                result = runner(request, **options)
            except BaseException:
                store.update(execution_id, status="failed")
                raise
            store.update(
                execution_id,
                status="completed",
                work_item_id=result.work_item.id,
                result=result.model_dump(mode="json"),
            )
            return result


@contextmanager
def _saver(path: Path, *, read_only: bool = False):
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError as exc:
        raise ExecutionConflict(
            "Durable graph execution requires the orchestration extra (SQLite checkpointer)."
        ) from exc
    if read_only:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
            if not {"checkpoints", "writes"}.issubset(tables):
                raise ExecutionConflict("No native graph checkpoints exist for this execution.")
            saver = SqliteSaver(connection)
            saver.is_setup = True  # Tables verified; the read path must not initialize storage.
            yield saver
        finally:
            connection.close()
    else:
        with SqliteSaver.from_conn_string(str(path)) as saver:
            yield saver


def _request_contract(request: WorkflowRunRequest, options: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "request": request.model_dump(mode="json"),
        "options": {
            "manager_loop": bool(options.get("manager_loop")),
            "max_manager_steps": options.get("max_manager_steps", 3),
            "enable_interrupts": bool(options.get("enable_interrupts", True)),
        },
    }


def run_durable_graph(request: WorkflowRunRequest, **options: Any):
    from keystone_agents.langgraph_workflow import LangGraphWorkflowOutcome, run_work_item_langgraph

    active = current_execution()
    store = (
        active.store if active else ExecutionStore(execution_database_path(request.database_url))
    )
    existing = (
        store.get(active.execution_id) if active
        else store.find(execution_id=request.execution_id, origin=request.origin_event_id)
    )
    saved_contract = (
        store.stage_result(existing["id"], "graph_contract", {}) if existing else None
    )
    if saved_contract is None and existing and not active:
        saved_contract = json.loads(existing["request_json"])
    request = (
        restore_workflow_context(request, saved_contract["request"])
        if saved_contract else freeze_workflow_context(request)
    )
    budget = current_model_request_budget_snapshot() or {}
    row = (
        store.get(active.execution_id)
        if active
        else store.begin(
            _request_contract(request, options),
            execution_id=request.execution_id,
            origin=request.origin_event_id,
            budget_limit=request.model_request_limit
            if request.model_request_limit is not None
            else budget.get("remaining"),
            source_fingerprint=_compatibility_fingerprint(),
        )
    )
    execution_id = row["id"]
    store.save_stage(execution_id, "graph_contract", {}, _request_contract(request, options))
    if not active and row["result_json"] and row["status"] in {"completed", "interrupted"}:
        return LangGraphWorkflowOutcome.model_validate_json(row["result_json"])
    if not active and row["status"] in {"running", "failed"}:
        raise ExecutionConflict(
            f"Execution {execution_id} has saved progress; use explicit resume."
        )
    with (
        nullcontext()
        if active
        else execution_lock(
            store.path.with_name(f"{store.path.name}.{stable_hash(execution_id)[:20]}.lock")
        )
    ):
        latest = store.get(execution_id)
        if (
            not active
            and latest["status"] in {"completed", "interrupted"}
            and latest["result_json"]
        ):
            return LangGraphWorkflowOutcome.model_validate_json(latest["result_json"])
        if not active and latest["status"] in {"running", "failed"}:
            raise ExecutionConflict("Saved progress requires explicit resume.")
        with work_item_execution_lock(
            request.database_url,
            request.work_item_id or latest["work_item_id"],
            owner=execution_id,
        ):
            store.update(execution_id, status="running")
            try:
                with _saver(store.path) as saver, activate_execution(store, execution_id):
                    outcome = run_work_item_langgraph(
                        request,
                        checkpointer=saver,
                        thread_id=options.get("thread_id") or execution_id,
                        native_thread_id=execution_id,
                        **{k: v for k, v in options.items() if k != "thread_id"},
                    )
                    snapshot = saver.get_tuple({"configurable": {"thread_id": execution_id}})
                    if snapshot is None:
                        raise ExecutionConflict("Graph completed without a persisted checkpoint.")
                outcome = outcome.model_copy(update={"execution_id": execution_id, "durable": True})
                store.update(
                    execution_id,
                    status="interrupted" if outcome.interrupted else "completed",
                    work_item_id=outcome.result.work_item.id,
                    result=outcome.model_dump(mode="json"),
                )
                return outcome
            except BaseException:
                store.update(execution_id, status="failed")
                raise


def inspect_graph_execution(
    execution_id: str, *, database_url: str | None = None, include_state: bool = False
) -> dict[str, Any]:
    """Read checkpoints without invoking graph nodes, models, or provider tools."""
    from keystone_agents.langgraph_workflow import build_work_item_langgraph

    store = ExecutionStore(execution_database_path(database_url), initialize=False)
    row = store.get(execution_id)
    with _saver(store.path, read_only=True) as saver:
        graph = build_work_item_langgraph(checkpointer=saver)
        snapshots = list(graph.get_state_history({"configurable": {"thread_id": execution_id}}))
    history = []
    previous: dict[str, Any] = {}
    for snapshot in reversed(snapshots):
        values = dict(snapshot.values)
        history.append(
            {
                "checkpoint_id": snapshot.config["configurable"].get("checkpoint_id"),
                "next_nodes": list(snapshot.next),
                "created_at": snapshot.created_at,
                "node_path": list(values.get("node_path", [])),
                "changed_keys": sorted(
                    k for k in set(values) | set(previous) if values.get(k) != previous.get(k)
                ),
                "state_fingerprint": stable_hash(values),
                **({"state": values} if include_state else {}),
                "work_item_id": (values.get("result") or {}).get("work_item", {}).get("id", ""),
                "interrupts": [
                    str(getattr(i, "id", "")) for task in snapshot.tasks for i in task.interrupts
                ],
            }
        )
        previous = values
    return {
        "schema": EXECUTION_SCHEMA,
        "execution_id": execution_id,
        "status": row["status"],
        "work_item_id": row["work_item_id"],
        "budget_limit": row["budget_limit"],
        "consumed": row["consumed"],
        "history": history,
        "operations": store.operations(execution_id),
        "provider_calls_made": 0,
    }


def _refresh_work_item_state(
    state: dict[str, Any], request: WorkflowRunRequest, *, approval: bool
) -> dict:
    """Reconcile approval changes only; reject a changed objective/target/evidence universe."""
    prepared = dict(state.get("prepared_step") or {})
    old_data = (
        state.get("business_state")
        or (state.get("result") or {}).get("work_item")
        or prepared.get("work_item")
    )
    if not old_data:
        return state
    old = WorkItem.model_validate(old_data)
    current = SQLiteStore(request.database_url).get_work_item(old.id)
    if current is None:
        raise ExecutionConflict("Checkpoint WorkItem no longer exists.")
    if current.status.value == "archived":
        raise ExecutionConflict("The WorkItem was archived while paused.")
    if (
        old.target != current.target
        or old.request_text != current.request_text
        or old.sources != current.sources
    ):
        raise ExecutionConflict(
            "WorkItem scope or evidence changed while paused; start a reviewed new execution."
        )
    if not approval and (
        any(
            getattr(old, key) != getattr(current, key)
            for key in (
                "status",
                "current_route",
                "next_action",
                "blockers",
                "facts",
                "approval_gates",
                "artifact_refs",
            )
        )
    ):
        raise ExecutionConflict("WorkItem execution state changed; inspect before resuming.")
    if approval:
        from keystone_agents.schemas.approval import approved_state_for_scope

        def expected_state(scope: str) -> str:
            normalized = "drafting" if scope == "drafting_context" else scope
            return approved_state_for_scope(normalized).value

        gates = {g.approval_id: g for g in current.approval_gates}
        old_artifacts = [
            a.model_dump(mode="json", exclude={"approval_state"}) for a in old.artifact_refs
        ]
        new_artifacts = [
            a.model_dump(mode="json", exclude={"approval_state"}) for a in current.artifact_refs
        ]
        if (
            old_artifacts != new_artifacts
            or old.facts != current.facts
            or old.current_route != current.current_route
        ):
            raise ExecutionConflict(
                "Approval cannot authorize changed artifacts, facts, or ownership."
            )
        changed = [
            g
            for g in old.approval_gates
            if g.required and g.state != expected_state(g.scope)
        ]
        if not changed or any(
            g.approval_id not in gates
            or gates[g.approval_id].state != expected_state(g.scope)
            for g in changed
        ):
            raise ExecutionConflict(
                "The exact pending approval must be approved in authoritative WorkItem state."
            )
    if prepared:
        prepared["work_item"] = current.model_dump(mode="json")
        state["prepared_step"] = prepared
    if state.get("result"):
        state["result"] = {**state["result"], "work_item": current.model_dump(mode="json")}
    return state


def resume_graph_execution(
    execution_id: str,
    *,
    database_url: str | None = None,
    live_sdk: bool = False,
    live_search: bool = False,
    approved: bool = False,
):
    """Resume the same native checkpoint; live capabilities must be explicitly reaffirmed."""
    from keystone_agents.langgraph_workflow import (
        LangGraphWorkflowOutcome,
        build_work_item_langgraph,
        run_work_item_langgraph,
    )

    store = ExecutionStore(execution_database_path(database_url), initialize=False)
    row = store.get(execution_id)
    entry_link = store.stage_result(execution_id, "entry_attempt", {})

    def persist_recovered_public_result(outcome) -> None:
        if outcome.interrupted:
            return
        from keystone_agents.presentation.public_result import (
            attach_execution_public_result,
            build_work_item_result_payload,
            ensure_work_item_user_facing_summary,
        )
        from keystone_agents.presentation.renderers import render_work_item_result_text

        prior = store.stage_result(execution_id, "public_result", {})
        if prior is not None:
            # Recover a crash between the two deterministic cache writes without
            # inventing plain stdout for an original, normally captured response.
            if (
                prior.get("recovery", {}).get("source") == "saved_native_graph_result"
                and store.stage_result(execution_id, "public_text", {}) is None
            ):
                result, _ = ensure_work_item_user_facing_summary(outcome.result)
                store.save_stage(execution_id, "public_text", {}, {
                    "text": render_work_item_result_text(result) + "\n",
                    "source": "new_render_from_saved_graph_result",
                })
            return
        result, verified = ensure_work_item_user_facing_summary(outcome.result)
        latest = store.get(execution_id)
        payload = build_work_item_result_payload(
            result, user_facing_result_verified=verified,
            graph_metadata={
                "runtime": outcome.graph_runtime, "execution_id": execution_id,
                "checkpoint_key": outcome.checkpoint_key, "node_path": list(outcome.node_path),
                "checkpoint_required": outcome.checkpoint_required,
                "checkpoint_reason": outcome.checkpoint_reason,
            },
            execution_metadata={
                "live_sdk": outcome.request.live_sdk, "live_search": outcome.request.live_search,
                "langgraph": True, "model_requests_admitted": latest["consumed"],
            },
        )
        payload["recovery"] = {
            "execution_id": execution_id, "source": "saved_native_graph_result",
            "original_cli_response_delivery": "unverified", "render_model_requests": 0,
        }
        attach_execution_public_result(payload)
        store.save_stage(execution_id, "public_result", {}, payload)
        store.save_stage(execution_id, "public_text", {}, {
            "text": render_work_item_result_text(result) + "\n",
            "source": "new_render_from_saved_graph_result",
        })

    def record_recovery(status: str, *, work_item_id: str = "", error_type: str = "") -> None:
        if entry_link is None:
            return  # Legacy executions have no exact link; never infer one from wording.
        from keystone_agents.runtime.execution_attempt import record_execution_attempt_recovery

        record_execution_attempt_recovery(
            database_url=database_url, execution_id=execution_id, link=entry_link,
            request_fingerprint=json.loads(row["request_json"]).get("request_fingerprint", ""),
            recovery_status=status, work_item_id=work_item_id, error_type=error_type,
        )

    contract = store.stage_result(execution_id, "graph_contract", {}) or json.loads(
        row["request_json"]
    )
    if contract.get("backend") == "direct" or "request" not in contract:
        raise ExecutionConflict(
            "This execution has no native graph contract; use its original direct continuation."
        )
    from langgraph.types import Command
    if row["status"] == "completed" and row["result_json"]:
        outcome = LangGraphWorkflowOutcome.model_validate_json(row["result_json"])
        record_recovery("completed", work_item_id=outcome.result.work_item.id)
        persist_recovered_public_result(outcome)
        return outcome
    if row["source_fingerprint"] != _compatibility_fingerprint():
        raise ExecutionConflict(
            "Execution code or runtime changed; use an isolated fork or reviewed migration."
        )
    request = WorkflowRunRequest.from_checkpoint(contract["request"])
    if request.context_file_path and request.context_file_snapshot is None:
        raise ExecutionConflict(
            "Saved execution has no accepted context snapshot; review before resume."
        )
    if request.live_sdk and not live_sdk or request.live_search and not live_search:
        raise ExecutionConflict(
            "Resume requires explicit reaffirmation of the saved live capabilities."
        )
    # Never accept a different business DB from the persisted execution.
    if execution_database_path(request.database_url) != store.path:
        raise ExecutionConflict("Execution belongs to another business database.")
    config = {"configurable": {"thread_id": execution_id}, "recursion_limit": 100}
    with execution_lock(
        store.path.with_name(f"{store.path.name}.{stable_hash(execution_id)[:20]}.lock")
    ):
        row = store.get(execution_id)
        if row["status"] == "completed" and row["result_json"]:
            outcome = LangGraphWorkflowOutcome.model_validate_json(row["result_json"])
            record_recovery("completed", work_item_id=outcome.result.work_item.id)
            persist_recovered_public_result(outcome)
            return outcome
        with (
            work_item_execution_lock(
                request.database_url,
                request.work_item_id or row["work_item_id"],
                owner=execution_id,
            ),
            _saver(store.path) as saver,
            activate_execution(store, execution_id),
        ):
            graph = build_work_item_langgraph(checkpointer=saver)
            snapshot = graph.get_state(config)
            if not snapshot.values:
                raise ExecutionConflict("No graph checkpoint exists for this execution.")
            interrupted = any(t.interrupts for t in snapshot.tasks)
            if interrupted and not approved:
                raise ExecutionConflict(
                    "Execution is waiting for an authoritative approval decision."
                )
            _refresh_work_item_state(dict(snapshot.values), request, approval=interrupted)
            record_recovery("running", work_item_id=row["work_item_id"])
            token = GRAPH_INPUT.set(
                (True, Command(resume={"approved": True}) if interrupted else None)
            )
            try:
                store.update(execution_id, status="running")
                outcome = run_work_item_langgraph(
                    request, checkpointer=saver, thread_id=execution_id, **contract["options"]
                )
            except BaseException as exc:
                store.update(execution_id, status="failed")
                try:
                    record_recovery(
                        "failed", work_item_id=row["work_item_id"], error_type=type(exc).__name__,
                    )
                except Exception as audit_error:
                    exc.add_note(
                        f"Recovery audit could not be updated ({type(audit_error).__name__})."
                    )
                raise
            finally:
                GRAPH_INPUT.reset(token)
        outcome = outcome.model_copy(update={"execution_id": execution_id, "durable": True})
        store.update(
            execution_id,
            status="interrupted" if outcome.interrupted else "completed",
            work_item_id=outcome.result.work_item.id,
            result=outcome.model_dump(mode="json"),
        )
        record_recovery(
            "interrupted" if outcome.interrupted else "completed",
            work_item_id=outcome.result.work_item.id,
        )
        persist_recovered_public_result(outcome)
        return outcome


def fork_graph_checkpoint(
    execution_id: str,
    checkpoint_id: str,
    *,
    database_url: str | None = None,
    changes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Export a source-linked branch for a separate isolated fixture run."""
    from keystone_agents.langgraph_workflow import build_work_item_langgraph

    changes = dict(changes or {})
    if set(changes) - {"request_text", "max_results"}:
        raise ExecutionConflict(
            "Diagnostic patches may change only request text or requested result count."
        )
    store = ExecutionStore(execution_database_path(database_url), initialize=False)
    store.get(execution_id)
    with _saver(store.path, read_only=True) as saver:
        graph = build_work_item_langgraph(checkpointer=saver)
        state = graph.get_state(
            {"configurable": {"thread_id": execution_id, "checkpoint_id": checkpoint_id}}
        )
    if not state.values:
        raise ExecutionConflict("Checkpoint was not found.")
    request = WorkflowRunRequest.from_checkpoint(state.values["original_request"])
    # Export references only. No stored approvals, live flags, or provider identity grants transfer.
    diagnostic_request = WorkflowRunRequest(
        request_text=str(changes.get("request_text", request.request_text)),
        max_results=int(changes.get("max_results", request.max_results)),
        max_results_explicit=("max_results" in changes or request.max_results_explicit),
        save=False,
    )
    return {
        "schema": "keystone.graph_diagnostic_fork.v1",
        "fork_id": f"fork_{uuid4().hex}",
        "source_execution_id": execution_id,
        "source_checkpoint_id": checkpoint_id,
        "source_state_fingerprint": stable_hash(state.values),
        "next_nodes": list(state.next),
        "request": diagnostic_request.model_dump(mode="json"),
        "execution_enabled": False,
        "provider_calls_made": 0,
        "note": ("Supply sanitized evidence in an isolated experiment; "
                 "no live effects are replayed."),
    }
