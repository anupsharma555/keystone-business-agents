"""Runtime for agent-led signal selection over bounded read-only history tools."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import copy
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
from typing import Any, Literal

from keystone_agents.agents.preprints_context import build_preprints_context_agent
from keystone_agents.agents.rss_context import build_rss_context_agent
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.run import run_typed_sdk_agent, sdk_run_failure_metadata
from keystone_agents.runtime.tool_call_budget import (
    ToolCallBudgetContract,
    ToolCallLimit,
)
from keystone_agents.runtime.tool_execution import (
    ToolEvidenceGroup,
    ToolExecutionContract,
    ToolExecutionMode,
    evaluate_tool_execution_contract,
    sdk_tool_execution_records,
    sdk_tool_output_payloads,
)
from keystone_agents.schemas.decision_ownership import (
    DecisionTelemetryEvent,
    DecisionValidatorOutcome,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.operational_context import (
    PreprintsContextResult,
    RssContextResult,
)
from keystone_agents.tools.announcement_context_tools import (
    linked_history_authority_constraint_records,
    linked_history_authority_constraints,
    linked_history_candidate_constraint_values,
)

SignalKind = Literal["rss", "preprints"]
_SIGNAL_DECISION_STAGE = "signal_relevance_selection"
_MAX_SIGNAL_DECISION_CANDIDATES = 20
_MAX_SIGNAL_HISTORY_CALLS = 2
_MAX_SIGNAL_EVIDENCE_READ_CALLS = 3
_MAX_SIGNAL_TOOL_CALLS = _MAX_SIGNAL_HISTORY_CALLS + _MAX_SIGNAL_EVIDENCE_READ_CALLS
_SIGNAL_MUTATION_TOOLS = frozenset(
    {
        "prepare_signal_lifecycle_checkpoint",
        "advance_signal_lifecycle_checkpoint",
    }
)


class SignalAgentDecisionError(RuntimeError):
    """The signal specialist returned an identity outside its tool evidence."""

    def __init__(self, telemetry: Mapping[str, Any]) -> None:
        self.telemetry = dict(telemetry)
        outcome = self.telemetry.get("validator_outcome")
        reason = outcome.get("reason_code") if isinstance(outcome, Mapping) else ""
        super().__init__(
            "Signal specialist selection was not bound to its bounded history result. "
            f"reason_code={reason or 'unknown'}"
        )


def run_signal_context_sdk(
    kind: SignalKind,
    request_text: str,
    *,
    manual_request_plan: ManualRequestPlan | Mapping[str, Any] | None = None,
    run_config: Any | None = None,
    live: bool = False,
    model: str | None = None,
    session: Any | None = None,
    max_turns: int = 4,
    max_selected: int = 8,
    agent: Any | None = None,
) -> TypedAgentRunResult[RssContextResult | PreprintsContextResult]:
    """Run a read-only specialist selection with one bounded decision repair."""

    route = "preprints_context_agent" if kind == "preprints" else "rss_context_agent"
    tool_name = (
        "retrieve_preprint_announcement_history"
        if kind == "preprints"
        else "retrieve_rss_announcement_history"
    )
    evidence_tool_name = (
        "read_preprint_announcement_evidence"
        if kind == "preprints"
        else "read_rss_announcement_evidence"
    )
    output_type = PreprintsContextResult if kind == "preprints" else RssContextResult
    builder = (
        build_preprints_context_agent if kind == "preprints" else build_rss_context_agent
    )
    resolved_agent = agent or builder(
        model=model,
        request_text=request_text,
        manual_plan=manual_request_plan,
        tool_tier="core_read",
    )
    attached_tool_names = {
        str(getattr(tool, "name", "") or "").strip()
        for tool in list(getattr(resolved_agent, "tools", []) or [])
        if str(getattr(tool, "name", "") or "").strip()
    }
    forbidden_tools = sorted(attached_tool_names.intersection(_SIGNAL_MUTATION_TOOLS))
    if forbidden_tools:
        raise ValueError(
            "Signal relevance selection must remain read-only; checkpoint mutation "
            f"tools were attached: {', '.join(forbidden_tools)}"
        )

    tool_contract = ToolExecutionContract.required(
        ToolEvidenceGroup("signal_history", (tool_name,)),
        stage=f"{route}_current_state",
    )
    initial_result = run_typed_sdk_agent(
        agent=resolved_agent,
        typed_input=request_text,
        output_type=output_type,
        run_config=run_config,
        live=live,
        session=session,
        max_turns=max(2, min(int(max_turns), 6)),
        tool_execution_contract=tool_contract,
        tool_call_budget_contract=ToolCallBudgetContract(
            limits=(
                ToolCallLimit(tool_name, _MAX_SIGNAL_HISTORY_CALLS),
                ToolCallLimit(evidence_tool_name, _MAX_SIGNAL_EVIDENCE_READ_CALLS),
            ),
            max_total_calls=_MAX_SIGNAL_TOOL_CALLS,
            stage=f"{route}_current_state",
        ),
    )
    evidence = signal_decision_evidence(
        initial_result.raw_result,
        tool_name=tool_name,
        evidence_tool_name=evidence_tool_name,
        authoritative_request=request_text,
    )
    outcome = validate_signal_agent_decision(
        initial_result.final_output,
        evidence,
        max_selected=max_selected,
    ).model_copy(update={"repair_attempted": False})
    attempt_telemetry = [
        _signal_decision_attempt_telemetry(
            initial_result.final_output,
            evidence,
            outcome,
            attempt=1,
            tool_mode="model_called",
        )
    ]
    results = [initial_result]
    repair_tool_outcome: dict[str, Any] | None = None
    if outcome.status != "accepted":
        repair_contract = ToolExecutionContract(
            mode=ToolExecutionMode.FORBIDDEN,
            stage=f"{route}_decision_repair",
        )
        repair_agent = _tool_free_signal_repair_agent(resolved_agent)
        repair_prompt = request_text + _signal_decision_repair_prompt(
            route=route,
            evidence=evidence,
            outcome=outcome,
        )
        try:
            repair_result = run_typed_sdk_agent(
                agent=repair_agent,
                typed_input=repair_prompt,
                output_type=output_type,
                run_config=run_config,
                live=live,
                session=None,
                max_turns=max(2, min(int(max_turns), 6)),
                tool_execution_contract=repair_contract,
            )
        except Exception as exc:
            _attach_signal_repair_execution_failure(
                exc,
                route=route,
                initial_result=initial_result,
                evidence=evidence,
            )
            raise
        results.append(repair_result)
        repair_tool_outcome = evaluate_tool_execution_contract(
            repair_result.raw_result,
            repair_contract,
            tool_receipts=repair_result.tool_receipts,
        ).receipt()
        outcome = validate_signal_agent_decision(
            repair_result.final_output,
            evidence,
            max_selected=max_selected,
        ).model_copy(update={"repair_attempted": True})
        attempt_telemetry.append(
            _signal_decision_attempt_telemetry(
                repair_result.final_output,
                evidence,
                outcome,
                attempt=2,
                tool_mode="verified_context_tool_free",
            )
        )

    result = _combine_signal_results(results)
    telemetry = signal_decision_telemetry(
        result.final_output,
        evidence,
        outcome,
        route=route,
        attempt=len(attempt_telemetry),
        attempts=attempt_telemetry,
    )
    request_cache = _combined_signal_request_cache(results)
    request_cache["request_tool_scope"] = _signal_request_tool_scope(
        resolved_agent,
        request_cache.get("request_tool_scope"),
    )
    tool_execution_outcome = evaluate_tool_execution_contract(
        initial_result.raw_result,
        tool_contract,
        tool_receipts=initial_result.tool_receipts,
    )
    request_cache["tool_execution_postcondition"] = tool_execution_outcome.receipt()
    sanitized_evidence = _sanitized_signal_decision_evidence(evidence)
    request_cache["signal_decision_evidence"] = sanitized_evidence
    request_cache["tool_execution_attempts"] = [
        {
            "attempt": 1,
            "role": "model_called_history_and_decision",
            "postcondition": tool_execution_outcome.receipt(),
        },
        *(
            [
                {
                    "attempt": 2,
                    "role": "tool_free_decision_repair",
                    "postcondition": repair_tool_outcome,
                }
            ]
            if repair_tool_outcome is not None
            else []
        ),
    ]
    request_cache["decision_ownership"] = telemetry
    if len(attempt_telemetry) > 1:
        repair_evidence = {
            "schema": "keystone.decision_repair_evidence_replay.v1",
            "status": "ready",
            "source": "first_attempt_model_called_history_tool",
            "candidate_ids": list(sanitized_evidence["candidate_ids"]),
            "evidence_fingerprint": sanitized_evidence["evidence_fingerprint"],
            "replayed_tool_names": [tool_name],
            "disabled_read_tool_names": sorted(attached_tool_names),
            "provider_calls_during_repair": 0,
            "raw_provider_payload_retained": False,
        }
        request_cache["decision_repair_evidence"] = repair_evidence
        telemetry["repair_evidence"] = repair_evidence
        request_cache["decision_repairs"] = 1
    if outcome.status != "accepted":
        error = SignalAgentDecisionError(telemetry)
        error.keystone_sdk_run_failure = {  # type: ignore[attr-defined]
            "schema": "keystone.sdk_run_failure.v1",
            "agent_name": route,
            "run_mode": "live_sdk" if result.live else "local_sdk",
            "failure_kind": "signalagentdecisionerror",
            "attempt_count": len(attempt_telemetry),
            "usage": dict(result.usage),
            "cost": dict(result.cost),
            "budget_guard": dict(result.budget_guard),
            "request_cache": request_cache,
            "tool_receipts": list(result.tool_receipts),
            "tool_execution": request_cache.get("tool_execution", {}),
            "tool_execution_postcondition": request_cache[
                "tool_execution_postcondition"
            ],
            "execution_telemetry": dict(result.execution_telemetry),
        }
        raise error
    return replace(result, request_cache=request_cache)


def signal_decision_evidence(
    raw_result: Any,
    *,
    tool_name: str,
    evidence_tool_name: str = "",
    authoritative_request: str = "",
) -> dict[str, Any]:
    """Project model-called history output into a trace-safe candidate set."""

    records = sdk_tool_execution_records(raw_result)
    admitted_tool_names = {tool_name, evidence_tool_name} - {""}
    model_call_count = sum(
        record.tool_name in admitted_tool_names for record in records
    )
    call_count = sum(
        record.tool_name in admitted_tool_names and record.succeeded
        for record in records
    )
    blocked_call_count = sum(
        record.tool_name in admitted_tool_names
        and not record.succeeded
        and record.status == "blocked"
        for record in records
    )
    unresolved_call_count = max(
        0,
        model_call_count - call_count - blocked_call_count,
    )
    candidates: list[dict[str, Any]] = []
    history_outputs = sdk_tool_output_payloads(
        raw_result,
        tool_name=tool_name,
        successful_only=True,
    )
    evidence_outputs = (
        sdk_tool_output_payloads(
            raw_result,
            tool_name=evidence_tool_name,
            successful_only=True,
        )
        if evidence_tool_name
        else []
    )
    history_query_contracts = _signal_tool_query_contracts(
        raw_result,
        tool_name=tool_name,
    )
    tool_result_item_counts = [
        len(list((entry.get("output") or {}).get("items") or []))
        for entry in history_outputs
    ]
    reformulation = _bounded_signal_reformulation(
        tool_result_item_counts=tool_result_item_counts,
        query_contracts=history_query_contracts,
    )
    authoritative_constraints = linked_history_authority_constraint_records(
        authoritative_request
    )
    for entry in history_outputs:
        output = entry.get("output") or {}
        for item in output.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            item_id = str(item.get("feed_item_id") or "").strip()
            if not item_id:
                continue
            candidate = {
                "candidate_id": item_id,
                "title": str(item.get("title") or "")[:300],
                "url": str(item.get("url") or "")[:1200],
                "published_at": str(item.get("published_at") or "")[:80],
                "source": str(item.get("source") or "")[:160],
                "summary": str(
                    item.get("summary") or item.get("detailed_summary_seed") or ""
                )[:1400],
                "tags": _bounded_signal_strings(item.get("tags"), limit=20, width=120),
                "selected": bool(item.get("selected")),
                "relevance_status": str(item.get("relevance_status") or "")[:80],
                "selection_reason": str(item.get("selection_reason") or "")[:700],
                "source_basis": str(item.get("source_basis") or "")[:700],
                "evidence_status": str(item.get("evidence_status") or "")[:80],
                "publication_ids": _bounded_signal_strings(
                    item.get("publication_ids"), limit=12, width=200
                ),
                "evidence_notes": _bounded_signal_strings(
                    item.get("evidence_notes"), limit=6, width=700
                ),
                "selected_evidence_read_required": bool(
                    item.get("selected_evidence_read_required")
                ),
                "raw_item": dict(item),
            }
            if all(existing["candidate_id"] != item_id for existing in candidates):
                candidates.append(candidate)
    evidence_reads: list[dict[str, Any]] = []
    for entry in evidence_outputs:
        output = entry.get("output") or {}
        text = str(output.get("text") or "")
        source_snapshot = output.get("source_snapshot")
        evidence_reads.append(
            {
                "status": str(output.get("status") or "")[:80],
                "read_mode": str(output.get("read_mode") or "")[:80],
                "feed_item_id": str(output.get("feed_item_id") or "")[:500],
                "evidence_id": str(output.get("evidence_id") or "")[:200],
                "content_status": str(output.get("content_status") or "")[:80],
                "content_complete": bool(output.get("content_complete")),
                "text_char_count": len(text),
                "text_sha256": sha256(text.encode("utf-8")).hexdigest() if text else "",
                "snapshot_sha256": (
                    str(source_snapshot.get("sha256") or "")
                    if isinstance(source_snapshot, Mapping)
                    else ""
                ),
            }
        )
    return {
        "candidate_ids": [item["candidate_id"] for item in candidates],
        "candidates": candidates[:25],
        "tool_name": tool_name,
        "evidence_tool_name": evidence_tool_name,
        "tool_call_count": call_count,
        "history_tool_call_count": len(history_outputs),
        "evidence_read_call_count": len(evidence_outputs),
        "evidence_reads": evidence_reads,
        "model_tool_call_count": model_call_count,
        "blocked_tool_call_count": blocked_call_count,
        "unresolved_tool_call_count": unresolved_call_count,
        "tool_output_count": len(history_outputs) + len(evidence_outputs),
        "tool_result_item_counts": tool_result_item_counts,
        "history_scopes": [
            str(contract.get("history_scope") or "")
            for contract in history_query_contracts
        ],
        "bounded_reformulation": reformulation,
        "authoritative_constraints": [
            dict(item) for item in authoritative_constraints
        ],
    }


def _signal_tool_query_contracts(
    raw_result: Any,
    *,
    tool_name: str,
) -> list[dict[str, Any]]:
    items = list(getattr(raw_result, "new_items", []) or [])
    if isinstance(raw_result, Mapping):
        items = list(raw_result.get("new_items") or raw_result.get("items") or [])
    successful_call_ids = {
        record.call_id
        for record in sdk_tool_execution_records(raw_result)
        if record.tool_name == tool_name and record.succeeded
    }
    contracts: list[dict[str, Any]] = []
    for item in items:
        raw_item = (
            item.get("raw_item")
            if isinstance(item, Mapping)
            else getattr(item, "raw_item", None)
        )
        item_type = str(
            (item.get("type") if isinstance(item, Mapping) else getattr(item, "type", ""))
            or (
                raw_item.get("type")
                if isinstance(raw_item, Mapping)
                else getattr(raw_item, "type", "")
            )
            or ""
        ).lower()
        if "call" not in item_type or "output" in item_type:
            continue
        observed_name = str(
            (item.get("tool_name") if isinstance(item, Mapping) else getattr(item, "tool_name", ""))
            or (item.get("name") if isinstance(item, Mapping) else getattr(item, "name", ""))
            or (
                raw_item.get("name")
                if isinstance(raw_item, Mapping)
                else getattr(raw_item, "name", "")
            )
            or ""
        )
        if observed_name != tool_name:
            continue
        call_id = str(
            (item.get("call_id") if isinstance(item, Mapping) else getattr(item, "call_id", ""))
            or (
                raw_item.get("call_id")
                if isinstance(raw_item, Mapping)
                else getattr(raw_item, "call_id", "")
            )
            or ""
        )
        if call_id not in successful_call_ids:
            continue
        arguments = (
            item.get("arguments")
            if isinstance(item, Mapping)
            else getattr(item, "arguments", None)
        )
        if arguments is None:
            arguments = (
                raw_item.get("arguments")
                if isinstance(raw_item, Mapping)
                else getattr(raw_item, "arguments", None)
            )
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        query = str(arguments.get("query") or "") if isinstance(arguments, Mapping) else ""
        if isinstance(arguments, Mapping):
            history_scope = str(arguments.get("history_scope") or "").strip().lower()
            if not history_scope:
                history_scope = (
                    "selected" if arguments.get("selected_only") is True else "discovery"
                )
        else:
            history_scope = ""
        feed_item_id = (
            str(arguments.get("feed_item_id") or "").strip()
            if isinstance(arguments, Mapping)
            else ""
        )
        normalized = " ".join(query.split()).lower()
        terms = tuple(dict.fromkeys(re.findall(r"[a-z0-9]+", normalized)))
        explicit_constraints = linked_history_authority_constraints(normalized)
        contracts.append(
            {
                "query_sha256": sha256(normalized.encode("utf-8")).hexdigest(),
                "nonblank": bool(normalized),
                "term_count": len(terms),
                "terms": terms,
                "history_scope": history_scope,
                "operation": "evidence_read" if feed_item_id else "history_discovery",
                "feed_item_id": feed_item_id,
                "explicit_constraint_hashes": tuple(
                    sha256(value.encode("utf-8")).hexdigest()
                    for value in explicit_constraints
                ),
            }
        )
    return contracts


def _bounded_signal_reformulation(
    *,
    tool_result_item_counts: list[int],
    query_contracts: list[dict[str, Any]],
) -> dict[str, Any]:
    if len(tool_result_item_counts) <= 1:
        return {"used": False, "valid": True, "reason": "not_used"}
    if len(tool_result_item_counts) != 2 or len(query_contracts) != 2:
        return {
            "used": True,
            "valid": False,
            "reason": "reformulation_evidence_incomplete",
        }
    if tool_result_item_counts[0] != 0:
        return {
            "used": True,
            "valid": False,
            "reason": "first_result_was_not_empty",
        }
    if query_contracts[0].get("history_scope") != query_contracts[1].get(
        "history_scope"
    ):
        return {
            "used": True,
            "valid": False,
            "reason": "history_scope_changed",
        }
    if query_contracts[1].get("nonblank") is not True:
        return {
            "used": True,
            "valid": False,
            "reason": "second_query_was_blank",
        }
    if query_contracts[0]["query_sha256"] == query_contracts[1]["query_sha256"]:
        return {
            "used": True,
            "valid": False,
            "reason": "second_query_was_unchanged",
        }
    return {
        "used": True,
        "valid": True,
        "reason": "changed_query_after_empty_result",
    }


def _bounded_signal_strings(value: Any, *, limit: int, width: int) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [
        str(item)[:width]
        for item in list(value)[:limit]
        if str(item).strip()
    ]


def _selected_signal_constraint_failure(
    selected_ids: list[str],
    candidates: list[Mapping[str, Any]],
    constraints: list[Mapping[str, Any]],
) -> tuple[str, str] | None:
    if not constraints:
        return None
    candidates_by_id = {
        str(candidate.get("candidate_id") or ""): candidate
        for candidate in candidates
        if str(candidate.get("candidate_id") or "")
    }
    for selected_id in selected_ids:
        candidate = candidates_by_id.get(selected_id)
        if candidate is None:
            continue
        evidence = linked_history_candidate_constraint_values(candidate)
        for constraint in constraints:
            kind = str(constraint.get("kind") or "")
            relation = str(constraint.get("relation") or "exact")
            expected = str(constraint.get("value") or "")
            if kind == "malformed":
                return (
                    "selected_signal_constraint_evidence_missing",
                    (
                        "The operator constraint is malformed, so the selected signal "
                        "cannot be verified against it."
                    ),
                )
            observed = tuple(evidence.get(kind) or ())
            if not observed:
                return (
                    "selected_signal_constraint_evidence_missing",
                    (
                        f"Selected signal {selected_id} has no {kind} evidence needed "
                        "to verify the operator constraint. Decline selection or report "
                        "that more context is needed."
                    ),
                )
            if kind == "date":
                matched = any(
                    _signal_date_constraint_matches(
                        observed_date,
                        relation=relation,
                        expected=expected,
                    )
                    for observed_date in observed
                )
            else:
                matched = expected in observed
            if not matched:
                return (
                    "selected_signal_constraint_violation",
                    (
                        f"Selected signal {selected_id} does not satisfy the explicit "
                        f"{kind} constraint. Choose only a returned candidate whose "
                        "evidence satisfies the operator constraint, or decline selection."
                    ),
                )
    return None


def _signal_date_constraint_matches(
    observed: str,
    *,
    relation: str,
    expected: str,
) -> bool:
    if relation == "after":
        return observed > expected
    if relation == "on_or_after":
        return observed >= expected
    if relation == "before":
        return observed < expected
    if relation == "on_or_before":
        return observed <= expected
    return observed == expected


def validate_signal_agent_decision(
    result: RssContextResult | PreprintsContextResult,
    evidence: Mapping[str, Any],
    *,
    max_selected: int,
) -> DecisionValidatorOutcome:
    candidate_ids = [str(value) for value in evidence.get("candidate_ids") or []]
    selected_ids = list(result.decision.selected_candidate_ids)
    assessment_ids = [
        assessment.candidate_id for assessment in result.decision.candidate_assessments
    ]
    output_ids = list(dict.fromkeys([*result.retrieved_item_ids, *[
        article.feed_item_id for article in result.articles if article.feed_item_id
    ]]))
    tool_call_count = int(evidence.get("tool_call_count") or 0)
    history_tool_call_count = int(
        evidence["history_tool_call_count"]
        if "history_tool_call_count" in evidence
        else tool_call_count
    )
    evidence_read_call_count = int(evidence.get("evidence_read_call_count") or 0)
    if not 1 <= history_tool_call_count <= _MAX_SIGNAL_HISTORY_CALLS:
        return _signal_outcome(
            "rejected",
            "signal_history_call_count_out_of_bounds",
            (
                "Call the bounded history tool once, or at most twice when the "
                "first result is empty and the second query is changed and nonblank."
            ),
            candidate_ids,
        )
    if evidence_read_call_count > _MAX_SIGNAL_EVIDENCE_READ_CALLS:
        return _signal_outcome(
            "rejected",
            "signal_evidence_read_count_out_of_bounds",
            "Use no more than the bounded saved-evidence read allowance.",
            candidate_ids,
        )
    if tool_call_count > _MAX_SIGNAL_TOOL_CALLS:
        return _signal_outcome(
            "rejected",
            "signal_tool_call_count_out_of_bounds",
            "The combined history and saved-evidence read allowance was exceeded.",
            candidate_ids,
        )
    if int(evidence.get("unresolved_tool_call_count") or 0) != 0:
        return _signal_outcome(
            "rejected",
            "signal_history_call_count_out_of_bounds",
            "A history-tool attempt did not produce bounded usable evidence.",
            candidate_ids,
        )
    if int(evidence.get("tool_output_count") or 0) != tool_call_count:
        return _signal_outcome(
            "rejected",
            "signal_history_output_missing_or_malformed",
            "Every bounded history call must return one structured result before selection.",
            candidate_ids,
        )
    reformulation = evidence.get("bounded_reformulation")
    if isinstance(reformulation, Mapping) and reformulation.get("valid") is not True:
        return _signal_outcome(
            "rejected",
            "signal_history_reformulation_invalid",
            (
                "A second history read is allowed only after an empty first result "
                "and must use a changed nonempty query within the same source scope."
            ),
            candidate_ids,
        )
    if len(candidate_ids) > _MAX_SIGNAL_DECISION_CANDIDATES:
        return _signal_outcome(
            "rejected",
            "signal_candidate_set_out_of_bounds",
            (
                "Repeat the bounded history read with no more than "
                f"{_MAX_SIGNAL_DECISION_CANDIDATES} candidates."
            ),
            candidate_ids,
        )
    if result.decision.decision_owner != "specialist_agent":
        return _signal_outcome(
            "rejected",
            "signal_decision_owner_mismatch",
            "Return decision_owner=specialist_agent for the relevance decision.",
            candidate_ids,
        )
    if result.decision.decision_stage != _SIGNAL_DECISION_STAGE:
        return _signal_outcome(
            "rejected",
            "signal_decision_stage_mismatch",
            f"Return decision_stage={_SIGNAL_DECISION_STAGE}.",
            candidate_ids,
        )
    if not result.decision.reasoning.strip():
        return _signal_outcome(
            "rejected",
            "missing_signal_decision_reasoning",
            "Return concise reasoning grounded in the bounded history result.",
            candidate_ids,
        )
    if result.decision.needs_more_context and (selected_ids or output_ids):
        return _signal_outcome(
            "rejected",
            "needs_more_context_with_claimed_signal_output",
            (
                "When more context is needed, do not claim selected, retrieved, or "
                "article identities."
            ),
            candidate_ids,
        )
    if set(assessment_ids) != set(candidate_ids) or len(assessment_ids) != len(
        candidate_ids
    ):
        return _signal_outcome(
            "rejected",
            "incomplete_signal_candidate_assessments",
            "Assess every returned feed_item_id exactly once before selecting.",
            candidate_ids,
        )
    if result.decision.needs_more_context:
        return _signal_outcome(
            "accepted",
            "agent_requested_more_signal_context",
            "The specialist declined to guess from the available history.",
            candidate_ids,
        )
    if not selected_ids:
        return _signal_outcome(
            "rejected",
            "missing_selected_signal_identity",
            "Select returned feed_item_id values or set needs_more_context=true.",
            candidate_ids,
        )
    if len(selected_ids) > max(1, min(int(max_selected), 8)):
        return _signal_outcome(
            "rejected",
            "selected_signal_count_out_of_bounds",
            "Select no more than the requested bounded result count.",
            candidate_ids,
        )
    if not set(selected_ids).issubset(candidate_ids):
        return _signal_outcome(
            "rejected",
            "fabricated_selected_signal_identity",
            "Every selected identity must come from the model-called history tool.",
            candidate_ids,
        )
    candidates_by_id = {
        str(item.get("candidate_id") or ""): item
        for item in list(evidence.get("candidates") or [])
        if isinstance(item, Mapping)
    }
    required_evidence_reads = {
        selected_id
        for selected_id in selected_ids
        if bool(
            candidates_by_id.get(selected_id, {}).get(
                "selected_evidence_read_required"
            )
        )
    }
    completed_evidence_reads = {
        str(item.get("feed_item_id") or "")
        for item in list(evidence.get("evidence_reads") or [])
        if isinstance(item, Mapping)
        and item.get("status") == "success"
        and item.get("read_mode") == "evidence_content"
        and str(item.get("evidence_id") or "")
        and item.get("content_status")
        in {"saved_content_available", "empty_saved_record", "unavailable_saved_record"}
    }
    missing_evidence_reads = sorted(
        required_evidence_reads - completed_evidence_reads
    )
    if missing_evidence_reads:
        return _signal_outcome(
            "rejected",
            "selected_signal_saved_evidence_not_read",
            (
                "Read at least one exact saved evidence_id for each selected canonical "
                "signal before producing the relevance decision. Missing: "
                + ", ".join(missing_evidence_reads)
            ),
            candidate_ids,
        )
    constraint_failure = _selected_signal_constraint_failure(
        selected_ids,
        [
            item
            for item in list(evidence.get("candidates") or [])
            if isinstance(item, Mapping)
        ],
        [
            item
            for item in list(evidence.get("authoritative_constraints") or [])
            if isinstance(item, Mapping)
        ],
    )
    if constraint_failure is not None:
        reason_code, feedback = constraint_failure
        return _signal_outcome(
            "rejected",
            reason_code,
            feedback,
            candidate_ids,
        )
    if set(output_ids) != set(selected_ids):
        return _signal_outcome(
            "rejected",
            "signal_output_identity_mismatch",
            "The decision, retrieved_item_ids, and article identities must agree.",
            candidate_ids,
        )
    return DecisionValidatorOutcome(
        status="accepted",
        decision_stage="signal_relevance_selection",
        selected_candidate_id=selected_ids[0],
        candidate_count=len(candidate_ids),
        selected_identity_in_candidate_set=True,
        selected_identity_was_read=True,
        reason_code="agent_signal_selection_bound_to_history_result",
        feedback="Selected signal identities came from the model-called history tool.",
    )


def _signal_outcome(
    status: Literal["accepted", "rejected"],
    reason_code: str,
    feedback: str,
    candidate_ids: list[str],
) -> DecisionValidatorOutcome:
    return DecisionValidatorOutcome(
        status=status,
        decision_stage="signal_relevance_selection",
        candidate_count=len(candidate_ids),
        selected_identity_in_candidate_set=False if status == "rejected" else None,
        selected_identity_was_read=False if status == "rejected" else None,
        reason_code=reason_code,
        feedback=feedback,
    )


def signal_decision_telemetry(
    result: RssContextResult | PreprintsContextResult,
    evidence: Mapping[str, Any],
    outcome: DecisionValidatorOutcome,
    *,
    route: str,
    attempt: int = 1,
    attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return one terminal envelope preserving every proposal and validator result."""

    candidate_ids = list(evidence.get("candidate_ids") or [])
    selected_candidate_ids = list(result.decision.selected_candidate_ids)
    excluded_candidate_ids = [
        item.candidate_id
        for item in result.decision.candidate_assessments
        if item.disposition == "excluded"
    ]
    attempt_rows = list(attempts or [])
    if not attempt_rows:
        attempt_rows.append(
            _signal_decision_attempt_telemetry(
                result,
                evidence,
                outcome,
                attempt=attempt,
            )
        )
    events = [
        event
        for attempt_row in attempt_rows
        for event in list(attempt_row.get("events") or [])
    ]
    events.append(
        DecisionTelemetryEvent(
            event_type="terminal",
            decision_owner=result.decision.decision_owner,
            decision_stage=_SIGNAL_DECISION_STAGE,
            attempt=attempt,
            candidate_ids=candidate_ids,
            selected_candidate_ids=selected_candidate_ids,
            excluded_candidate_ids=excluded_candidate_ids,
            validator_status=outcome.status,
            reason_code=outcome.reason_code,
            tool_mode=(
                "verified_context_tool_free" if attempt > 1 else "model_called"
            ),
            reasoning=result.decision.reasoning,
            limitations=list(result.decision.limitations),
        ).model_dump(mode="json")
    )
    return {
        "schema": "keystone.agent_decision_run.v1",
        "decision_owner": result.decision.decision_owner,
        "decision_stage": _SIGNAL_DECISION_STAGE,
        "route": route,
        "attempt_count": len(attempt_rows),
        "repair_attempted": len(attempt_rows) > 1,
        "attempts": attempt_rows,
        "candidate_count": len(candidate_ids),
        "candidate_ids": candidate_ids,
        "selected_candidate_ids": selected_candidate_ids,
        "excluded_candidate_ids": excluded_candidate_ids,
        "reasoning": result.decision.reasoning,
        "limitations": list(result.decision.limitations),
        "needs_more_context": result.decision.needs_more_context,
        "validator_outcome": outcome.model_dump(mode="json"),
        "events": events,
    }


def _signal_decision_attempt_telemetry(
    result: RssContextResult | PreprintsContextResult,
    evidence: Mapping[str, Any],
    outcome: DecisionValidatorOutcome,
    *,
    attempt: int,
    tool_mode: str = "model_called",
) -> dict[str, Any]:
    candidate_ids = list(evidence.get("candidate_ids") or [])
    selected_candidate_ids = list(result.decision.selected_candidate_ids)
    excluded_candidate_ids = [
        item.candidate_id
        for item in result.decision.candidate_assessments
        if item.disposition == "excluded"
    ]
    events = [
        DecisionTelemetryEvent(
            event_type="repair_proposed" if attempt > 1 else "proposed",
            decision_owner=result.decision.decision_owner,
            decision_stage=_SIGNAL_DECISION_STAGE,
            attempt=attempt,
            candidate_ids=candidate_ids,
            selected_candidate_ids=selected_candidate_ids,
            excluded_candidate_ids=excluded_candidate_ids,
            tool_mode=tool_mode,
            reasoning=result.decision.reasoning,
            limitations=list(result.decision.limitations),
        ),
        DecisionTelemetryEvent(
            event_type="validator_result",
            decision_owner=result.decision.decision_owner,
            decision_stage=_SIGNAL_DECISION_STAGE,
            attempt=attempt,
            candidate_ids=candidate_ids,
            selected_candidate_ids=selected_candidate_ids,
            excluded_candidate_ids=excluded_candidate_ids,
            validator_status=outcome.status,
            reason_code=outcome.reason_code,
            tool_mode=tool_mode,
            reasoning=result.decision.reasoning,
            limitations=list(result.decision.limitations),
        ),
    ]
    return {
        "attempt": attempt,
        "tool_mode": tool_mode,
        "decision_owner": result.decision.decision_owner,
        "decision_stage": _SIGNAL_DECISION_STAGE,
        "candidate_ids": candidate_ids,
        "selected_candidate_ids": selected_candidate_ids,
        "excluded_candidate_ids": excluded_candidate_ids,
        "reasoning": result.decision.reasoning,
        "limitations": list(result.decision.limitations),
        "needs_more_context": result.decision.needs_more_context,
        "validator_outcome": outcome.model_dump(mode="json"),
        "events": [event.model_dump(mode="json") for event in events],
    }


def _signal_decision_repair_prompt(
    *,
    route: str,
    evidence: Mapping[str, Any],
    outcome: DecisionValidatorOutcome,
) -> str:
    safe_evidence = _sanitized_signal_decision_evidence(evidence)
    serialized_evidence = json.dumps(
        safe_evidence,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        "\n\nDecision validator repair (one attempt only):\n"
        f"- Route: {route}\n"
        f"- Rejection: {outcome.reason_code}\n"
        f"- Feedback: {outcome.feedback}\n"
        "The successful history read from the first attempt is replayed below as "
        "authoritative bounded evidence. Do not call any tool, repeat retrieval, widen "
        "the candidate set, or perform a mutation. Reassess only these candidates and "
        "return the complete structured result again.\n"
        f"Sanitized original candidate evidence: {serialized_evidence}\n"
        "Return an explicit decision with "
        "decision_owner=specialist_agent, "
        f"decision_stage={_SIGNAL_DECISION_STAGE}, concise evidence-grounded reasoning, "
        "and one candidate_assessment for every replayed candidate_id. "
        "Keep selected_candidate_ids, retrieved_item_ids, and article identities aligned. "
        "If the evidence is insufficient, assess every candidate and set "
        "needs_more_context=true without claiming selected output.\n"
    )


def _tool_free_signal_repair_agent(agent: Any) -> Any:
    """Clone one specialist for evidence-only repair without changing its base instance."""

    repair_agent = copy(agent)
    repair_agent.tools = []
    return repair_agent


def _sanitized_signal_decision_evidence(
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Retain descriptive candidate evidence without raw provider payloads."""

    safe_keys = (
        "candidate_id",
        "title",
        "url",
        "published_at",
        "source",
        "summary",
        "tags",
        "selected",
        "relevance_status",
        "selection_reason",
        "source_basis",
        "evidence_status",
        "publication_ids",
        "evidence_notes",
        "selected_evidence_read_required",
    )
    candidates = [
        {key: candidate.get(key) for key in safe_keys if key in candidate}
        for candidate in list(evidence.get("candidates") or [])
        if isinstance(candidate, Mapping)
    ]
    payload = {
        "schema": "keystone.signal_decision_evidence.v1",
        "source": "first_attempt_model_called_history_tool",
        "tool_name": str(evidence.get("tool_name") or ""),
        "evidence_tool_name": str(evidence.get("evidence_tool_name") or ""),
        "tool_call_count": int(evidence.get("tool_call_count") or 0),
        "history_tool_call_count": int(
            evidence.get("history_tool_call_count") or 0
        ),
        "evidence_read_call_count": int(
            evidence.get("evidence_read_call_count") or 0
        ),
        "model_tool_call_count": int(evidence.get("model_tool_call_count") or 0),
        "blocked_tool_call_count": int(evidence.get("blocked_tool_call_count") or 0),
        "unresolved_tool_call_count": int(
            evidence.get("unresolved_tool_call_count") or 0
        ),
        "tool_output_count": int(evidence.get("tool_output_count") or 0),
        "tool_result_item_counts": [
            int(value)
            for value in list(evidence.get("tool_result_item_counts") or [])[:2]
        ],
        "history_scopes": [
            str(value)
            for value in list(evidence.get("history_scopes") or [])[:2]
            if str(value) in {"discovery", "selected"}
        ],
        "bounded_reformulation": dict(evidence.get("bounded_reformulation") or {}),
        "evidence_reads": [
            {
                "status": str(item.get("status") or ""),
                "read_mode": str(item.get("read_mode") or ""),
                "feed_item_id": str(item.get("feed_item_id") or ""),
                "evidence_id": str(item.get("evidence_id") or ""),
                "content_status": str(item.get("content_status") or ""),
                "content_complete": bool(item.get("content_complete")),
                "text_char_count": int(item.get("text_char_count") or 0),
                "text_sha256": str(item.get("text_sha256") or ""),
                "snapshot_sha256": str(item.get("snapshot_sha256") or ""),
            }
            for item in list(evidence.get("evidence_reads") or [])[
                :_MAX_SIGNAL_EVIDENCE_READ_CALLS
            ]
            if isinstance(item, Mapping)
        ],
        "authoritative_constraints": [
            {
                "kind": str(item.get("kind") or ""),
                "relation": str(item.get("relation") or ""),
                "value": str(item.get("value") or ""),
            }
            for item in list(evidence.get("authoritative_constraints") or [])
            if isinstance(item, Mapping)
        ],
        "candidate_count": len(candidates),
        "candidate_ids": [str(value) for value in evidence.get("candidate_ids") or []],
        "candidates": candidates,
        "raw_provider_payload_retained": False,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload["evidence_fingerprint"] = sha256(canonical.encode("utf-8")).hexdigest()
    return payload


def _signal_request_tool_scope(
    agent: Any,
    current_scope: Any,
) -> dict[str, Any]:
    """Retain exact attached-tool schema fingerprints without raw schemas."""

    scope = dict(current_scope) if isinstance(current_scope, Mapping) else {}
    declared_scope = getattr(agent, "request_tool_scope", None)
    if callable(getattr(declared_scope, "model_dump", None)):
        declared_scope = declared_scope.model_dump(mode="json")
    if isinstance(declared_scope, Mapping):
        scope.update(dict(declared_scope))

    attached_tools: list[dict[str, Any]] = []
    schema_fingerprints: dict[str, str] = {}
    for tool in list(getattr(agent, "tools", []) or []):
        name = str(getattr(tool, "name", "") or "").strip()
        if not name:
            continue
        schema = (
            getattr(tool, "params_json_schema", None)
            or getattr(tool, "parameters", None)
            or getattr(tool, "input_json_schema", None)
        )
        if not isinstance(schema, Mapping):
            continue
        canonical = json.dumps(
            dict(schema),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        fingerprint = sha256(canonical.encode("utf-8")).hexdigest()
        schema_fingerprints[name] = fingerprint
        attached_tools.append(
            {
                "name": name,
                "schema_fingerprint": fingerprint,
                "schema_source": "params_json_schema_fingerprint",
            }
        )
    if schema_fingerprints:
        scope["tool_schema_fingerprints"] = schema_fingerprints
        scope["attached_tools"] = attached_tools
    return scope


def _combine_signal_results(
    results: list[TypedAgentRunResult[RssContextResult | PreprintsContextResult]],
) -> TypedAgentRunResult[RssContextResult | PreprintsContextResult]:
    """Combine model attempts while retaining the final validated structured output."""

    final = results[-1]
    raw_results = [result.raw_result for result in results]
    combined_raw = SimpleNamespace(
        new_items=[
            item
            for raw_result in raw_results
            for item in list(getattr(raw_result, "new_items", []) or [])
        ],
        raw_responses=[
            response
            for raw_result in raw_results
            for response in list(getattr(raw_result, "raw_responses", []) or [])
        ],
        final_output=getattr(final.raw_result, "final_output", final.final_output),
        last_agent=getattr(final.raw_result, "last_agent", None),
    )
    usage = _aggregate_signal_usage([result.usage for result in results])
    cost = _aggregate_signal_cost([result.cost for result in results])
    budget_guard = dict(final.budget_guard or {})
    budget_guard["signal_attempts"] = [
        dict(result.budget_guard or {}) for result in results
    ]
    execution_telemetry = dict(final.execution_telemetry or {})
    execution_telemetry["signal_recovery"] = {
        "schema": "keystone.signal_recovery_execution.v1",
        "attempt_count": len(results),
        "model_request_count": usage.get("requests"),
        "attempts": [
            {
                "attempt": index,
                "role": (
                    "model_called_history_and_decision"
                    if index == 1
                    else "tool_free_decision_repair"
                ),
                "usage": dict(result.usage or {}),
                "cost": dict(result.cost or {}),
                "execution_telemetry": dict(result.execution_telemetry or {}),
            }
            for index, result in enumerate(results, start=1)
        ],
    }
    return replace(
        final,
        raw_result=combined_raw,
        usage=usage,
        cost=cost,
        budget_guard=budget_guard,
        execution_telemetry=execution_telemetry,
        tool_receipts=_combined_signal_receipts(results),
    )


def _combined_signal_request_cache(
    results: list[TypedAgentRunResult[RssContextResult | PreprintsContextResult]],
) -> dict[str, Any]:
    request_cache = dict(results[0].request_cache or {})
    model_usage: list[dict[str, Any]] = []
    stage_caches: list[dict[str, Any]] = []
    model_attempt_index = 0
    for outer_attempt, result in enumerate(results, start=1):
        cache = dict(result.request_cache or {})
        role = (
            "model_called_history_and_decision"
            if outer_attempt == 1
            else "tool_free_decision_repair"
        )
        stage_caches.append(
            {
                "attempt": outer_attempt,
                "role": role,
                "request_tool_scope": dict(cache.get("request_tool_scope") or {}),
                "tool_execution": dict(cache.get("tool_execution") or {}),
                "tool_execution_postcondition": dict(
                    cache.get("tool_execution_postcondition") or {}
                ),
                "model_attempt_usage": list(cache.get("model_attempt_usage") or []),
            }
        )
        for event in list(cache.get("model_attempt_usage") or []):
            if not isinstance(event, Mapping):
                continue
            model_attempt_index += 1
            model_usage.append(
                {
                    **dict(event),
                    "attempt_index": model_attempt_index,
                    "signal_outer_attempt": outer_attempt,
                    "signal_attempt_role": role,
                }
            )
    request_cache["model_attempt_usage"] = model_usage
    request_cache["signal_stage_request_caches"] = stage_caches
    return request_cache


def _aggregate_signal_usage(values: list[Mapping[str, Any]]) -> dict[str, Any]:
    return _aggregate_signal_accounting(
        values,
        additive_fields=(
            "requests",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
            "reasoning_output_tokens",
        ),
    )


def _aggregate_signal_cost(values: list[Mapping[str, Any]]) -> dict[str, Any]:
    return _aggregate_signal_accounting(values, additive_fields=("estimated_usd",))


def _aggregate_signal_accounting(
    values: list[Mapping[str, Any]],
    *,
    additive_fields: tuple[str, ...],
) -> dict[str, Any]:
    payloads = [dict(value or {}) for value in values]
    available = [payload for payload in payloads if payload.get("available") is True]
    aggregate = dict(payloads[-1] if payloads else {})
    aggregate["available"] = bool(available)
    aggregate["complete"] = len(available) == len(payloads)
    aggregate["attempt_count"] = len(payloads)
    for field in additive_fields:
        entries = [payload.get(field) for payload in available]
        if not entries or len(entries) != len(payloads) or any(
            not isinstance(value, int | float) or isinstance(value, bool)
            for value in entries
        ):
            aggregate[field] = None
            continue
        total = sum(entries)
        aggregate[field] = int(total) if all(isinstance(value, int) for value in entries) else total
    input_tokens = aggregate.get("input_tokens")
    cached_input_tokens = aggregate.get("cached_input_tokens")
    if isinstance(input_tokens, int) and input_tokens > 0 and isinstance(
        cached_input_tokens, int
    ):
        aggregate["cache_hit_rate"] = round(cached_input_tokens / input_tokens, 6)
    elif "input_tokens" in additive_fields:
        aggregate["cache_hit_rate"] = None
    return aggregate


def _combined_signal_receipts(
    results: list[TypedAgentRunResult[RssContextResult | PreprintsContextResult]],
) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    fingerprints: set[str] = set()
    for result in results:
        for receipt in list(result.tool_receipts or []):
            if not isinstance(receipt, Mapping):
                continue
            payload = dict(receipt)
            fingerprint = json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            )
            if fingerprint in fingerprints:
                continue
            fingerprints.add(fingerprint)
            receipts.append(payload)
    return receipts


def _attach_signal_repair_execution_failure(
    exc: BaseException,
    *,
    route: str,
    initial_result: TypedAgentRunResult[RssContextResult | PreprintsContextResult],
    evidence: Mapping[str, Any],
) -> None:
    """Augment a failed tool-free repair with the successful first-read evidence."""

    repair_failure = sdk_run_failure_metadata(exc)
    repair_usage = (
        dict(repair_failure.get("usage") or {})
        if isinstance(repair_failure.get("usage"), Mapping)
        else {}
    )
    repair_cost = (
        dict(repair_failure.get("cost") or {})
        if isinstance(repair_failure.get("cost"), Mapping)
        else {}
    )
    repair_receipts = [
        dict(receipt)
        for receipt in list(repair_failure.get("tool_receipts") or [])
        if isinstance(receipt, Mapping)
    ]
    request_cache = dict(initial_result.request_cache or {})
    request_cache["signal_decision_evidence"] = _sanitized_signal_decision_evidence(
        evidence
    )
    request_cache["signal_repair_failure"] = {
        "failure_kind": str(repair_failure.get("failure_kind") or type(exc).__name__),
        "request_cache": dict(repair_failure.get("request_cache") or {}),
        "tool_execution": dict(repair_failure.get("tool_execution") or {}),
    }
    metadata = {
        **repair_failure,
        "schema": "keystone.sdk_run_failure.v1",
        "agent_name": route,
        "failure_kind": str(repair_failure.get("failure_kind") or type(exc).__name__).lower(),
        "attempt_count": 2,
        "usage": _aggregate_signal_usage([initial_result.usage, repair_usage]),
        "cost": _aggregate_signal_cost([initial_result.cost, repair_cost]),
        "request_cache": request_cache,
        "tool_receipts": _combined_signal_receipts(
            [
                initial_result,
                replace(initial_result, tool_receipts=repair_receipts),
            ]
        ),
        "tool_execution": dict(repair_failure.get("tool_execution") or {}),
        "execution_telemetry": {
            "schema": "keystone.signal_recovery_execution.v1",
            "status": "failed",
            "attempt_count": 2,
            "attempts": [
                dict(initial_result.execution_telemetry or {}),
                dict(repair_failure.get("execution_telemetry") or {}),
            ],
        },
    }
    try:
        exc.keystone_sdk_run_failure = metadata  # type: ignore[attr-defined]
    except Exception:
        return


__all__ = [
    "SignalAgentDecisionError",
    "run_signal_context_sdk",
    "signal_decision_evidence",
    "signal_decision_telemetry",
    "validate_signal_agent_decision",
]
