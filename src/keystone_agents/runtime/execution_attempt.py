"""Privacy-safe persistence and terminal envelopes for top-level KBA attempts."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from keystone_agents.operator_failures import (
    OperatorReadableFailure,
    known_exception_to_operator_failure,
)
from keystone_agents.runtime.decision_trace_harness import (
    assemble_backend_decision_scenario_trace_from_runtime,
    build_accounting_usage_trace_groups,
    build_backend_decision_stage_trace_from_runtime,
    stable_fingerprint,
)
from keystone_agents.runtime.request_budget import (
    current_model_request_budget_snapshot,
)
from keystone_agents.runtime.tool_execution import (
    build_tool_execution_summary,
    external_write_state_from_evidence,
)
from keystone_agents.storage.sqlite_store import (
    SQLiteStore,
    sqlite_path_from_url,
    stable_hash,
    stable_json,
)

EXECUTION_ATTEMPT_SCHEMA = "keystone.execution_attempt.v1"
EXECUTION_FAILURE_SCHEMA = "keystone.execution_failure.v1"
EXECUTION_ATTEMPT_LINK_SCHEMA = "keystone.execution_attempt_link.v1"


def bind_execution_attempt(attempt: ExecutionAttempt, execution_id: str) -> dict[str, Any]:
    """Persist both directions of an exact audit link before any graph/model work."""
    from keystone_agents.runtime.durable_execution import ExecutionConflict

    row = attempt.store.get_agent_run(attempt.run_id)
    payload = dict((row or {}).get("output") or {})
    if (
        not row or row.get("agent_name") != "kba_entrypoint" or row.get("status") != "started"
        or row.get("input_hash") != attempt.request_fingerprint
        or payload.get("schema") != EXECUTION_ATTEMPT_SCHEMA
        or payload.get("attempt_id") != attempt.run_id
        or payload.get("request", {}).get("fingerprint") != attempt.request_fingerprint
        or payload.get("durable_execution_id") not in (None, execution_id)
    ):
        raise ExecutionConflict("The CLI attempt cannot be bound to this execution.")
    payload["durable_execution_id"] = execution_id
    attempt.store.finalize_agent_run_attempt(attempt.run_id, output=payload, status="started")
    return {
        "schema": EXECUTION_ATTEMPT_LINK_SCHEMA,
        "execution_id": execution_id,
        "attempt_id": attempt.run_id,
        "request_fingerprint": attempt.request_fingerprint,
    }


def record_execution_attempt_recovery(
    *, database_url: str | None, execution_id: str, request_fingerprint: str,
    link: Mapping[str, Any], recovery_status: str, work_item_id: str = "", error_type: str = "",
) -> None:
    """Reconcile only an exactly linked abandoned CLI attempt, never its delivery.

    The original remains interrupted even when native recovery completes. Its
    original metadata is retained; no original exit code or full cost is inferred.
    """
    from keystone_agents.runtime.durable_execution import ExecutionConflict
    from keystone_agents.storage.sqlite_store import _assert_test_database_is_isolated

    if (
        link.get("schema") != EXECUTION_ATTEMPT_LINK_SCHEMA
        or link.get("execution_id") != execution_id
        or type(link.get("attempt_id")) is not int or link["attempt_id"] <= 0
        or not request_fingerprint or link.get("request_fingerprint") != request_fingerprint
        or recovery_status not in {"running", "failed", "interrupted", "completed"}
    ):
        raise ExecutionConflict("Execution audit link is incompatible.")
    path = Path(sqlite_path_from_url(database_url)).expanduser().resolve()
    _assert_test_database_is_isolated(str(path))
    # mode=rw must not create a missing business database during reconciliation.
    connection = sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT agent_name,input_hash,output_json,status FROM agent_runs WHERE id=?",
                (link["attempt_id"],),
            ).fetchone()
            payload = json.loads(row["output_json"]) if row else {}
            if (
                not row or row["agent_name"] != "kba_entrypoint"
                or row["input_hash"] != request_fingerprint
                or not isinstance(payload, dict)
                or payload.get("schema") != EXECUTION_ATTEMPT_SCHEMA
                or payload.get("attempt_id") != link["attempt_id"]
                or payload.get("durable_execution_id") != execution_id
                or payload.get("request", {}).get("fingerprint") != request_fingerprint
            ):
                raise ExecutionConflict("Execution audit link does not match its CLI attempt.")
            prior = payload.get("native_graph_recovery") or {}
            if row["status"] != "started" and not (
                row["status"] == "interrupted" and prior.get("execution_id") == execution_id
            ):
                return  # A separately finalized original attempt is not abandoned.
            event = {
                "status": recovery_status, "work_item_id": work_item_id,
                "error_type": error_type[:120],
            }
            history = list(prior.get("observations") or [])
            if history and all(history[-1].get(key) == value for key, value in event.items()):
                return
            history.append({**event, "observed_at": datetime.now(UTC).isoformat()})
            payload["terminal"] = {**payload.get("terminal", {}), "status": "interrupted"}
            payload["native_graph_recovery"] = {
                "execution_id": execution_id, "status": recovery_status,
                "observations": history[-20:],
                "prior_observations_omitted": int(prior.get("prior_observations_omitted") or 0)
                + max(0, len(history) - 20),
                "original_cli_exit_code": None,
                "original_cli_response_delivery": "unverified",
                "preflight_usage_reference": "graph_contract.request.orchestrator_preflight",
            }
            connection.execute(
                "UPDATE agent_runs SET status='interrupted',output_json=? "
                "WHERE id=? AND status=? AND input_hash=?",
                (stable_json(payload), link["attempt_id"], row["status"], request_fingerprint),
            )
    finally:
        connection.close()


@dataclass
class ExecutionAttempt:
    """One durable top-level attempt created before Orchestrator preflight."""

    store: SQLiteStore
    run_id: int
    request_fingerprint: str
    route_hint: str
    live: bool
    max_model_requests: int | None = None
    finalized: bool = False

    def record_preflight(
        self, preflight: Mapping[str, Any], *, plan_reference: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist returned model usage before later CLI refinement or preparation."""
        row = self.store.get_agent_run(self.run_id)
        if self.finalized or not row or row.get("status") != "started":
            raise RuntimeError(
                "Preflight observation requires the original active entrypoint attempt."
            )
        payload = dict(row.get("output") or {})
        route = _nested_mapping(preflight, "route_result")
        events = []
        for original_event in _mapping_sequence(preflight.get("sdk_usage_events")):
            event = dict(original_event)
            if isinstance(event.get("request_cache"), Mapping):
                stage = build_backend_decision_stage_trace_from_runtime(
                    stage_id="preflight-observation",
                    agent=str(event.get("agent_name") or "orchestrator"),
                    stage=str(event.get("run_stage") or "orchestrator_preflight"),
                    linked_output=event,
                )
                event["request_cache"] = stage.request_cache
                # Retain the original opaque identity through privacy projection,
                # so the observation and its copied child event count only once.
                # No other trace-summary fields or raw cache content belong here.
                summary = _nested_mapping(original_event["request_cache"], "trace_summary")
                trace_id, event_id = summary.get("trace_id"), summary.get("event_id")
                if (
                    isinstance(trace_id, str)
                    and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", trace_id)
                    and type(event_id) is int
                    and 0 < event_id <= 2**63 - 1
                ):
                    event["request_cache"]["trace_summary"] = {
                        "trace_id": trace_id, "event_id": event_id,
                    }
            # Preserve legacy event identity even though its cache is projected.
            event["preflight_event_fingerprint"] = stable_fingerprint(original_event)
            events.append(event)
        observation = {
            "capture_stage": "orchestrator_returned_before_cli_refinement",
            "selected_agent": preflight.get("selected_agent"),
            "route": route.get("route"),
            "workflow": list(route.get("workflow") or []),
            "blocked_by_orchestrator": bool(preflight.get("blocked_by_orchestrator")),
            "sdk_usage_events": events,
            "plan_reference": dict(plan_reference or {}),
        }
        payload["orchestrator_preflight_observation"] = observation
        groups, usage_issues = build_accounting_usage_trace_groups([
            {"orchestrator_preflight": observation},
        ])
        stages = groups[0]
        observed = sum(stage.consumption.model_request_count for stage in stages)
        budget = current_model_request_budget_snapshot() or {}
        payload["request_budget"] = {
            **dict(payload.get("request_budget") or {}), **budget,
            "observed_model_requests": observed,
            "observed_count_confirmed": bool(stages) and not usage_issues and all(
                stage.consumption.model_request_count_confirmed for stage in stages
            ),
        }
        self.store.finalize_agent_run_attempt(self.run_id, output=payload, status="started")

    def finalize(
        self,
        *,
        status: str,
        exit_code: int,
        linked_agent_run_id: int | None = None,
        linked_agent_run_ids: Sequence[int] = (),
        work_item_id: str = "",
        telemetry: Mapping[str, Any] | None = None,
        failure: BaseException | None = None,
        failure_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Finalize once and retain only bounded evidence projections."""

        if self.finalized:
            row = self.store.get_agent_run(self.run_id)
            return dict(row.get("output") or {}) if row else {}

        original = self.store.get_agent_run(self.run_id)
        original_payload = dict((original or {}).get("output") or {})
        preflight_observation = _nested_mapping(
            original_payload, "orchestrator_preflight_observation",
        )

        linked_ids = list(
            dict.fromkeys(
                int(value)
                for value in [*linked_agent_run_ids, linked_agent_run_id]
                if value is not None and int(value) != self.run_id
            )
        )
        linked_rows = self.store.get_agent_runs(linked_ids)
        linked_outputs = [
            dict(row["output"]) if isinstance(row.get("output"), Mapping) else {}
            for row in linked_rows
        ]
        metadata = dict(failure_metadata or {})
        linked_tool_executions = [
            _tool_execution_projection(output, {}) for output in linked_outputs
        ]
        tool_execution = (
            linked_tool_executions[-1]
            if linked_tool_executions
            else _tool_execution_projection({}, metadata)
        )
        receipts = _receipt_projection_many(linked_outputs, metadata)
        tool_invocations = _mapping_sequence(
            metadata.get("tool_invocations")
            or _nested_mapping(metadata, "request_cache").get("tool_invocations")
        )
        for output in linked_outputs:
            tool_invocations.extend(
                _mapping_sequence(_linked_request_cache(output).get("tool_invocations"))
            )
        external_write_state = external_write_state_from_evidence(
            receipts=receipts,
            tool_names=[
                *[
                    name
                    for summary in linked_tool_executions or [tool_execution]
                    for key in (
                        "model_called_tool_names",
                        "workflow_called_tool_names",
                        "workflow_called_helper_names",
                    )
                    for name in _string_sequence(summary.get(key))
                ],
            ],
            tool_invocations=tool_invocations,
            evidence_complete=bool(
                linked_tool_executions
                and all(
                    summary.get("provider_receipt_count_available") is True
                    and summary.get("model_tool_call_count") is not None
                    and summary.get("workflow_tool_call_count") is not None
                    for summary in linked_tool_executions
                )
            ),
        )
        failure_payload = (
            _operator_failure(failure, context="KBA natural-language run").to_dict()
            if failure is not None
            else None
        )
        terminal_status = "failed" if failure is not None else status
        preflight_groups, usage_trace_issues = build_accounting_usage_trace_groups(
            [{"orchestrator_preflight": preflight_observation}, *linked_outputs, metadata]
        )
        stages = list(preflight_groups[0])
        for index, row in enumerate(linked_rows, start=1):
            nested_stages = preflight_groups[index]
            stages.extend(
                stage
                for stage in nested_stages
                if stage.stage != "instruction_following.output_repair"
            )
            output = linked_outputs[index - 1]
            stage_metadata = metadata if index == len(linked_rows) else {}
            stages.append(
                build_backend_decision_stage_trace_from_runtime(
                    stage_id=f"agent-run-{row['id']}",
                    agent=str(row.get("agent_name") or self.route_hint or "orchestrator"),
                    stage=_linked_stage(output),
                    run_id=str(row["id"]),
                    request_cache=_linked_request_cache(output),
                    failure_metadata=stage_metadata,
                    linked_output=output,
                    terminal_status=str(row.get("status") or terminal_status),
                    latency_ms=_linked_latency_ms(output),
                )
            )
            stages.extend(
                stage
                for stage in nested_stages
                if stage.stage == "instruction_following.output_repair"
            )
            if index == len(linked_rows):
                stages.extend(preflight_groups[-1])
        if not linked_rows:
            stages.extend(preflight_groups[-1])
            # The shared dispatch ledger can prove zero additional model calls
            # after a captured preflight; do not turn unavailable SDK usage into zero.
            budget = current_model_request_budget_snapshot() or {}
            if (
                preflight_observation and "usage" not in metadata
                and stages
                and all(stage.consumption.model_request_count_confirmed for stage in stages)
                and budget.get("consumed") == sum(
                    stage.consumption.model_request_count for stage in stages
                )
            ):
                metadata = {**metadata, "usage": {
                    "available": True, "complete": True, "requests": 0,
                    "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                    "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                    "provider_request_count_confirmed": True,
                }, "cost": {"estimated_usd": 0, "source": "no_additional_sdk_dispatches"}}
            stages.append(
                build_backend_decision_stage_trace_from_runtime(
                    stage_id=f"entry-attempt-{self.run_id}",
                    agent=self.route_hint or "orchestrator",
                    stage=("entrypoint_after_orchestrator_preflight" if preflight_observation
                           else "entrypoint_and_orchestrator_preflight"),
                    run_id=str(self.run_id),
                    request_cache=_nested_mapping(metadata, "request_cache"),
                    failure_metadata=metadata,
                    receipts=receipts,
                    terminal_status=terminal_status,
                    latency_ms=_telemetry_latency_ms(telemetry or {}),
                )
            )
        trace = assemble_backend_decision_scenario_trace_from_runtime(
            scenario_id=f"entry-attempt-{self.run_id}",
            request_fingerprint=self.request_fingerprint,
            stages=stages,
        )
        request_count_confirmed = not usage_trace_issues and bool(trace.stages) and all(
            stage.consumption.model_request_count_confirmed
            for stage in trace.stages
        )
        within_request_ceiling = (
            trace.model_request_count <= self.max_model_requests
            if self.max_model_requests is not None and request_count_confirmed
            else None
        )
        enforced_budget = current_model_request_budget_snapshot()
        consumed = enforced_budget.get("consumed") if enforced_budget is not None else None
        usage_reconciled = (
            trace.model_request_count == int(consumed)
            if consumed is not None
            else None
        )
        if usage_reconciled is False:
            request_count_confirmed = False
            trace = trace.model_copy(
                update={
                    "evaluation_status": (
                        "fail" if trace.evaluation_status == "fail" else "partial"
                    ),
                    "findings": list(dict.fromkeys([
                        *trace.findings,
                        "observed_usage_does_not_reconcile_with_request_ledger",
                    ])),
                }
            )
        if within_request_ceiling is False:
            trace = trace.model_copy(
                update={
                    "evaluation_status": "fail",
                    "findings": list(
                        dict.fromkeys(
                            [
                                *trace.findings,
                                "model_request_ceiling_exceeded",
                            ]
                        )
                    ),
                }
            )
        unavailable = [
            item for stage_trace in stages for item in stage_trace.unavailable_evidence
        ]
        if usage_trace_issues:
            unavailable.extend(usage_trace_issues)
            trace = trace.model_copy(update={
                "evaluation_status": "fail" if trace.evaluation_status == "fail" else "partial",
                "findings": list(dict.fromkeys([
                    *trace.findings,
                    *usage_trace_issues,
                ])),
            })
        present_linked_ids = {int(row["id"]) for row in linked_rows}
        missing_linked_ids = [
            run_id for run_id in linked_ids if run_id not in present_linked_ids
        ]
        if missing_linked_ids:
            unavailable.append("linked_agent_run_record_not_found")
            request_count_confirmed = False
        if usage_reconciled is False:
            unavailable.append("request_usage_reconciliation_incomplete")
        if not linked_rows:
            unavailable.append("specialist_agent_runs_not_linked")
        if not work_item_id:
            unavailable.append("work_item_not_linked")
        enforced_limit = (
            enforced_budget.get("limit") if enforced_budget is not None else None
        )
        enforced_consumed = (
            enforced_budget.get("consumed") if enforced_budget is not None else None
        )
        enforced_within_ceiling = (
            int(enforced_consumed) <= int(enforced_limit)
            if enforced_limit is not None and enforced_consumed is not None
            else within_request_ceiling
        )
        payload: dict[str, Any] = {
            "schema": EXECUTION_ATTEMPT_SCHEMA,
            "attempt_id": self.run_id,
            "entrypoint": "ask",
            "request": {
                "fingerprint": self.request_fingerprint,
                "text_retained": False,
            },
            "route_hint": self.route_hint,
            "live": self.live,
            "links": {
                "agent_run_id": linked_ids[-1] if linked_ids else None,
                "agent_run_ids": linked_ids,
                "work_item_id": work_item_id,
            },
            "terminal": {
                "status": terminal_status,
                "exit_code": int(exit_code),
            },
            "request_budget": {
                "model_request_ceiling": self.max_model_requests,
                "ceiling_source": (
                    "cli_max_openai_requests"
                    if self.max_model_requests is not None
                    else "not_configured"
                ),
                "admission_gate": (
                    "ask_entrypoint_estimated_request_maximum"
                    if self.max_model_requests is not None
                    else "not_configured"
                ),
                "observed_model_requests": trace.model_request_count,
                "observed_count_confirmed": request_count_confirmed,
                "usage_reconciled_with_ledger": usage_reconciled,
                "limit": (
                    enforced_budget.get("limit")
                    if enforced_budget is not None
                    else self.max_model_requests
                ),
                "consumed": (
                    enforced_budget.get("consumed")
                    if enforced_budget is not None
                    else trace.model_request_count
                    if request_count_confirmed
                    else None
                ),
                "remaining": (
                    enforced_budget.get("remaining")
                    if enforced_budget is not None
                    else None
                ),
                "exhausted": bool(
                    enforced_budget and enforced_budget.get("exhausted")
                ),
                "exhaustion_stage": str(
                    (enforced_budget or {}).get("exhaustion_stage") or ""
                ),
                "correlation_id": str(
                    (enforced_budget or {}).get("correlation_id") or ""
                ),
                "enforcement": (
                    str(enforced_budget.get("enforcement") or "")
                    if enforced_budget is not None
                    else "estimated_admission_only"
                ),
                "within_ceiling": enforced_within_ceiling,
                "status": (
                    "not_configured"
                    if enforced_limit is None and self.max_model_requests is None
                    else "exhausted"
                    if enforced_budget and enforced_budget.get("exhausted")
                    else "unverified"
                    if enforced_consumed is None and not request_count_confirmed
                    else "within_ceiling"
                    if enforced_within_ceiling
                    else "exceeded"
                ),
            },
            "tool_execution": tool_execution,
            "external_write_state": external_write_state,
            "decision_trace": trace.model_dump(mode="json"),
            "execution_telemetry": {
                **dict(telemetry or {}),
                **(
                    {"model_request_budget": enforced_budget}
                    if enforced_budget is not None
                    else {}
                ),
            },
            "unavailable_evidence": list(dict.fromkeys(unavailable)),
        }
        if preflight_observation:
            payload["orchestrator_preflight_observation"] = preflight_observation
        durable_id = original_payload.get("durable_execution_id")
        if durable_id:
            payload["durable_execution_id"] = durable_id
        if failure_payload is not None:
            payload["failure"] = failure_payload
            payload["internal_diagnostics"] = {
                "error_type": type(failure).__name__,
                "error_fingerprint": stable_hash(
                    {
                        "error_type": type(failure).__name__,
                        "error": str(failure),
                    }
                ),
                "raw_traceback_retained": False,
            }
        self.store.finalize_agent_run_attempt(
            self.run_id,
            output=payload,
            status=terminal_status,
            error=str(failure_payload.get("kind") or "") if failure_payload else None,
            agent_name="kba_entrypoint",
            model=str(
                metadata.get("model")
                or (linked_rows[-1].get("model") if linked_rows else "")
                or ""
            ),
            dry_run=not self.live,
        )
        self.finalized = True
        return payload


def start_execution_attempt(
    *,
    store: SQLiteStore,
    request_text: str,
    route_hint: str = "",
    live: bool,
    max_model_requests: int | None = None,
) -> ExecutionAttempt:
    """Persist a content-free attempt before any model-owned preflight decision."""

    request_fingerprint = stable_hash({"request_text": str(request_text or "")})
    normalized_route = str(route_hint or "orchestrator").strip() or "orchestrator"
    initial = {
        "schema": EXECUTION_ATTEMPT_SCHEMA,
        "entrypoint": "ask",
        "request": {
            "fingerprint": request_fingerprint,
            "text_retained": False,
        },
        "route_hint": normalized_route,
        "live": bool(live),
        "request_budget": {
            "model_request_ceiling": max_model_requests,
            "ceiling_source": (
                "cli_max_openai_requests"
                if max_model_requests is not None
                else "not_configured"
            ),
            "admission_gate": (
                "ask_entrypoint_estimated_request_maximum"
                if max_model_requests is not None
                else "not_configured"
            ),
            "status": "pending" if max_model_requests is not None else "not_configured",
        },
        "terminal": {"status": "started"},
        "external_write_state": "unknown",
        "unavailable_evidence": [
            "orchestrator_preflight_not_completed",
            "tool_execution_not_started",
            "provider_receipts_not_recorded",
        ],
    }
    run_id = store.save_agent_run(
        agent_name="kba_entrypoint",
        input_hash=request_fingerprint,
        input_summary=(
            f"KBA natural-language request for {normalized_route}; content withheld."
        ),
        output=initial,
        model="preflight_pending",
        dry_run=not live,
        status="started",
    )
    initial["attempt_id"] = run_id
    store.finalize_agent_run_attempt(
        run_id,
        output=initial,
        status="started",
        model="preflight_pending",
        dry_run=not live,
    )
    return ExecutionAttempt(
        store=store,
        run_id=run_id,
        request_fingerprint=request_fingerprint,
        route_hint=normalized_route,
        live=bool(live),
        max_model_requests=max_model_requests,
    )


def build_execution_failure_envelope(
    exc: BaseException,
    *,
    execution_attempt_id: int | None = None,
    failure_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the structured public failure consumed by CLI and Slack adapters."""

    metadata = dict(failure_metadata or {})
    failure = _operator_failure(exc, context="KBA natural-language run")
    tool_execution = _tool_execution_projection({}, metadata)
    receipts = _receipt_projection({}, metadata)
    external_write_state = external_write_state_from_evidence(
        receipts=receipts,
        tool_names=[
            *_string_sequence(tool_execution.get("model_called_tool_names")),
            *_string_sequence(tool_execution.get("workflow_called_tool_names")),
            *_string_sequence(tool_execution.get("workflow_called_helper_names")),
        ],
        tool_invocations=_mapping_sequence(
            metadata.get("tool_invocations")
            or _nested_mapping(metadata, "request_cache").get("tool_invocations")
        ),
        evidence_complete=bool(
            tool_execution.get("provider_receipt_count_available") is True
            and tool_execution.get("model_tool_call_count") is not None
            and tool_execution.get("workflow_tool_call_count") is not None
        ),
    )
    unavailable = [
        name
        for name, available in (
            (
                "provider_request_attempt_count_not_recorded",
                tool_execution.get("provider_request_attempt_count_available"),
            ),
            (
                "provider_request_success_count_not_recorded",
                tool_execution.get("provider_request_success_count_available"),
            ),
            (
                "provider_receipt_count_not_recorded",
                tool_execution.get("provider_receipt_count_available"),
            ),
        )
        if available is not True
    ]
    if external_write_state == "unknown":
        unavailable.append("external_write_evidence_not_recorded")
    budget_snapshot = current_model_request_budget_snapshot()
    return {
        "schema": EXECUTION_FAILURE_SCHEMA,
        "mode": "execution_attempt",
        "status": "failed",
        "selected_agent": "orchestrator",
        "agent_name": "Orchestrator",
        "send_enabled": False,
        "execution_attempt_id": execution_attempt_id,
        "failure": failure.to_dict(),
        "tool_execution": tool_execution,
        "side_effects": {
            "external_write_state": external_write_state,
        },
        **(
            {"request_budget": budget_snapshot}
            if budget_snapshot is not None
            else {}
        ),
        "unavailable_evidence": unavailable,
    }


def _operator_failure(exc: BaseException, *, context: str) -> OperatorReadableFailure:
    failure = known_exception_to_operator_failure(exc, context=context)
    if failure.kind != "schema_or_parse_error":
        return failure
    return OperatorReadableFailure(
        kind=failure.kind,
        summary=failure.summary,
        reason="The model output did not satisfy the structured decision contract.",
        next_step=(
            "Preserve this failed attempt, inspect the decision fields, and retry only "
            "after the offline repair path passes."
        ),
        retryable=True,
        safe_to_continue=True,
    )


def _tool_execution_projection(
    linked_output: Mapping[str, Any],
    failure_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    linked_cache = _linked_request_cache(linked_output)
    linked_failure = _nested_mapping(linked_output, "sdk_run_failure")
    candidates = [
        linked_output.get("tool_execution"),
        linked_cache.get("tool_execution"),
        linked_failure.get("tool_execution"),
        failure_metadata.get("tool_execution"),
        _nested_mapping(failure_metadata, "request_cache").get("tool_execution"),
    ]
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            return dict(candidate)
    return build_tool_execution_summary(
        mode="entry_failure_before_tool_evidence",
        scope_source="top_level_execution_attempt",
    )


def _receipt_projection(
    linked_output: Mapping[str, Any],
    failure_metadata: Mapping[str, Any],
) -> list[dict[str, Any]]:
    values: list[Any] = []
    for source in (linked_output, failure_metadata):
        for key in ("tool_receipts", "preacquired_tool_receipts"):
            raw = source.get(key)
            if isinstance(raw, Sequence) and not isinstance(
                raw, str | bytes | bytearray
            ):
                values.extend(raw)
    receipts: list[dict[str, Any]] = []
    fingerprints: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            continue
        fingerprint = stable_hash(dict(value))
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        receipts.append(dict(value))
    return receipts


def _receipt_projection_many(
    linked_outputs: Sequence[Mapping[str, Any]],
    failure_metadata: Mapping[str, Any],
) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for output in linked_outputs:
        receipts.extend(_receipt_projection(output, {}))
        nested_failure = _nested_mapping(output, "sdk_run_failure")
        receipts.extend(_receipt_projection({}, nested_failure))
    receipts.extend(_receipt_projection({}, failure_metadata))
    deduped: list[dict[str, Any]] = []
    fingerprints: set[str] = set()
    for receipt in receipts:
        fingerprint = stable_hash(receipt)
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        deduped.append(receipt)
    return deduped


def _linked_request_cache(output: Mapping[str, Any]) -> dict[str, Any]:
    cache: dict[str, Any] = {}
    for candidate in (
        output.get("_sdk_request_cache"),
        output.get("request_cache"),
        _nested_mapping(output, "sdk_run_failure").get("request_cache"),
    ):
        if isinstance(candidate, Mapping):
            cache.update(dict(candidate))
    return cache


def _linked_stage(output: Mapping[str, Any]) -> str:
    cache = _linked_request_cache(output)
    decision = _nested_mapping(cache, "decision_ownership")
    trace_summary = _nested_mapping(cache, "trace_summary")
    return str(
        decision.get("decision_stage")
        or trace_summary.get("stage")
        or output.get("stage")
        or "sdk_agent_run"
    )[:160]


def _linked_latency_ms(output: Mapping[str, Any]) -> float | None:
    for candidate in (
        output.get("_execution_telemetry"),
        output.get("execution_telemetry"),
        output.get("_entry_execution_telemetry"),
    ):
        if isinstance(candidate, Mapping):
            latency = _telemetry_latency_ms(candidate)
            if latency is not None:
                return latency
    return None


def _nested_mapping(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    nested = value.get(key)
    return dict(nested) if isinstance(nested, Mapping) else {}


def _string_sequence(value: Any) -> list[str]:
    values = (
        list(value)
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)
        else [value]
    )
    return [str(item or "").strip() for item in values if str(item or "").strip()]


def _mapping_sequence(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _telemetry_latency_ms(telemetry: Mapping[str, Any]) -> float | None:
    for key in ("total_duration_ms", "duration_ms", "final_response_ms"):
        value = telemetry.get(key)
        if value is None:
            continue
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return None


__all__ = [
    "EXECUTION_ATTEMPT_SCHEMA",
    "EXECUTION_FAILURE_SCHEMA",
    "ExecutionAttempt",
    "build_execution_failure_envelope",
    "start_execution_attempt",
]
