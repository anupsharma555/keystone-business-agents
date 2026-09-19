"""Dry-run execution helpers."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from json import JSONDecodeError, dumps, loads
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from keystone_agents.costing import (
    AgentRunBudgetExceededError,
    enforce_agent_run_budget,
    estimate_usage_cost,
    gemini_free_tier_usage_context,
)
from keystone_agents.execution_telemetry import (
    ExecutionTelemetryRecorder,
    compact_execution_telemetry,
)
from keystone_agents.local_file_inputs import local_file_input_bundle_from_operator_input
from keystone_agents.model_provider import (
    GEMINI_PROVIDER,
    MissingOpenAIAPIKeyError,
    ModelConfig,
    ModelProviderConfigurationError,
    TraceConfig,
    TraceMetadata,
    get_gemini_fallback_model_config,
    get_openai_fallback_model_config,
    get_runtime_agent_model_config,
)
from keystone_agents.models import AgentRunRequest, AgentRunResult, RunMode, TypedAgentRunResult
from keystone_agents.operator_failures import known_exception_to_operator_failure
from keystone_agents.provider_read import (
    ProviderReadPlan,
    ProviderReadPolicy,
    activate_provider_read_context,
)
from keystone_agents.provider_recovery import (
    ProviderPartialSuccessError,
    ProviderRecoveryStore,
    failure_stage_from_exception,
)
from keystone_agents.receipts.journal import (
    instrument_agent_tools,
    mutation_tool_names,
    reset_tool_receipt_journal,
    retry_receipt_context,
    tool_invocation_journal,
    tool_payload_fingerprint,
    tool_receipt_journal,
)
from keystone_agents.receipts.mutations import operation_is_mutation
from keystone_agents.receipts.normalization import identity_fingerprint
from keystone_agents.runtime.decision_validation import (
    AgentDecisionContract,
    AgentDecisionValidationError,
    SpecialistDecisionEvidence,
    bind_authoritative_tool_evidence,
    build_cumulative_decision_repair_evidence_replay,
    decision_contract_prompt,
    decision_repair_prompt,
    decision_validation_telemetry,
    is_provider_read_tool_name,
    pre_model_decision_context_telemetry,
    validate_specialist_decision,
    verified_candidate_fingerprints_from_receipts,
)
from keystone_agents.runtime.execution_deadline import (
    ExecutionDeadlineExceeded,
    current_execution_deadline,
    current_execution_deadline_snapshot,
)
from keystone_agents.runtime.output_validation import (
    OutputValidationDiagnostics,
    agent_with_output_diagnostics,
    structured_output_retry_feedback,
)
from keystone_agents.runtime.request_budget import (
    current_model_request_budget_snapshot,
)
from keystone_agents.runtime.response_terminal import (
    response_terminal_diagnostics,
    response_terminal_failure_kind,
)
from keystone_agents.runtime.tool_call_budget import (
    ToolCallBudgetContract,
    ToolCallBudgetLedger,
)
from keystone_agents.runtime.tool_execution import (
    ToolExecutionContract,
    ToolExecutionContractError,
    build_tool_execution_summary,
    evaluate_tool_execution_contract,
    sdk_tool_execution_records,
    tool_execution_correction_prompt,
)
from keystone_agents.sdk import (
    AgentLike,
    agent_with_isolated_tool_state,
    agent_with_retry_compatible_tool_choice,
    agent_with_stable_prompt_cache_key,
    instruction_profile_text,
    numeric_sdk_request_usage,
    prompt_cache_key_audit_metadata,
    repo_instruction_profile_id,
    run_typed_sdk_sync,
    sdk_numeric_usage_observations,
)
from keystone_agents.sdk_sessions import build_sdk_session_from_env, session_audit_metadata
from keystone_agents.tools.serper_tool import (
    consume_sdk_search_telemetry,
    reset_sdk_search_telemetry,
    sdk_search_diagnostics_from_telemetry,
    set_sdk_search_request_context,
)

TOutput = TypeVar("TOutput")
TRaw = TypeVar("TRaw")


class UnsafeMutationRetryError(RuntimeError):
    """A mutation may have run without a conclusive retry-safe boundary."""

    def __init__(self, tool_names: Sequence[str]) -> None:
        self.tool_names = tuple(
            dict.fromkeys(str(name).strip() for name in tool_names if str(name).strip())
        )
        super().__init__(
            "A mutation tool failed or has unknown completion state; automatic retry "
            "is blocked to prevent a duplicate write: " + ", ".join(self.tool_names)
        )


class ToolEvidenceReplayError(RuntimeError):
    """Completed read evidence could not be replayed safely for correction."""

    def __init__(self, *, reason_code: str, feedback: str) -> None:
        self.reason_code = str(reason_code or "tool_evidence_replay_unavailable")
        self.feedback = str(feedback or "Completed tool evidence could not be replayed.")
        super().__init__(f"{self.reason_code}: {self.feedback}")


@dataclass(frozen=True)
class _ToolRetryState:
    completed_read_tool_names: tuple[str, ...] = ()
    completed_mutation_tool_names: tuple[str, ...] = ()
    uncertain_mutation_tool_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class SDKSynthesisOutcome:
    """Result envelope for retrieve-normalize-synthesize SDK workflows."""

    agent_name: str
    raw_context: Any
    typed_input: Any
    result: TypedAgentRunResult[Any]
    storage: dict[str, Any] = field(default_factory=dict)
    audit_notes: tuple[str, ...] = ()
    model_provider: str = ""
    model_name: str = ""
    model_run_mode: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    budget_guard: dict[str, Any] = field(default_factory=dict)
    request_cache: dict[str, Any] = field(default_factory=dict)
    execution_telemetry: dict[str, Any] = field(default_factory=dict)
    provider_usage_context: dict[str, Any] = field(default_factory=dict)
    started_at_unix: float | None = None
    ended_at_unix: float | None = None

    @property
    def output(self) -> Any:
        return self.result.output

    @property
    def final_output(self) -> Any:
        return self.result.final_output

    @property
    def live(self) -> bool:
        return self.result.live


def run_agent_dry(agent: AgentLike, request: AgentRunRequest | None = None) -> AgentRunResult:
    """Return a deterministic result without invoking model or tool APIs."""

    request = request or AgentRunRequest()
    if request.mode != RunMode.DRY_RUN:
        raise RuntimeError("Live execution is not implemented in this scaffold.")

    return AgentRunResult(
        agent_name=agent.name,
        mode=RunMode.DRY_RUN,
        summary=f"{agent.name} constructed successfully in dry-run mode.",
        data={"payload": request.payload},
    )


def prompt_from_typed_input(value: Any) -> str:
    """Render typed SDK input objects to an agent prompt."""

    to_prompt = getattr(value, "to_prompt", None)
    if callable(to_prompt):
        return str(to_prompt())
    if isinstance(value, Mapping):
        return dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return dumps(value, ensure_ascii=True, default=str)
    return str(value)


def sdk_input_from_typed_input(
    value: Any,
    *,
    live: bool,
    provider: str,
    trusted_attachment_paths: Sequence[str] = (),
) -> str | list[dict[str, Any]]:
    """Render typed input, preserving explicit local PDFs/images for OpenAI live runs."""

    prompt = prompt_from_typed_input(value)
    if not live or provider != "openai":
        return prompt
    bundle = local_file_input_bundle_from_operator_input(
        value, attachment_paths=trusted_attachment_paths,
    )
    if not bundle.has_inputs:
        return prompt
    return bundle.response_input(prompt)


def _sdk_input_audit_text(value: str | list[dict[str, Any]]) -> str:
    if isinstance(value, str):
        return value
    text_parts: list[str] = []
    file_parts: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        for part in item.get("content", []) or []:
            if not isinstance(part, Mapping):
                continue
            if part.get("type") == "input_text":
                text_parts.append(str(part.get("text") or ""))
            elif part.get("type") in {"input_file", "input_image"}:
                file_parts.append(
                    dumps(
                        {
                            "type": part.get("type"),
                            "filename": part.get("filename") or "",
                            "detail": part.get("detail") or "",
                        },
                        ensure_ascii=True,
                        sort_keys=True,
                    )
                )
    return "\n".join([*text_parts, *file_parts])


def run_typed_sdk_agent(
    *,
    agent: AgentLike,
    typed_input: Any,
    output_type: type[TOutput],
    run_config: Any | None = None,
    live: bool = False,
    config: ModelConfig | None = None,
    session: Any | None = None,
    inherit_env_session: bool = True,
    workflow_name: str | None = None,
    group_id: str | None = None,
    trace_metadata: TraceMetadata | None = None,
    tracing_disabled: bool | None = None,
    trace_include_sensitive_data: bool | None = None,
    trace_config: TraceConfig | None = None,
    max_turns: int | None = None,
    tool_correction_max_turns: int | None = None,
    decision_repair_max_turns: int | None = None,
    recovery_store: ProviderRecoveryStore | None = None,
    trusted_attachment_paths: Sequence[str] = (),
    tool_execution_contract: ToolExecutionContract | None = None,
    tool_call_budget_contract: ToolCallBudgetContract | None = None,
    preacquired_tool_receipts: Sequence[Mapping[str, Any] | Any] = (),
    decision_contract: AgentDecisionContract | None = None,
    structured_retry_evidence_provider: (
        Callable[[], Sequence[Mapping[str, Any]]] | None
    ) = None,
) -> TypedAgentRunResult[TOutput]:
    """Run an SDK agent through a typed, credential-safe execution path."""

    execution_telemetry = ExecutionTelemetryRecorder()
    if run_config is not None:
        if config is not None:
            model_provider = config.provider
            model_name = config.model
        else:
            model_provider = "local"
            model_name = str(getattr(run_config, "model", "") or "sdk-local")
    else:
        model_config = config or get_runtime_agent_model_config(
            getattr(agent, "name", None),
            model_override=getattr(agent, "model", None),
        )
        model_provider = model_config.provider
        model_name = model_config.model
    agent = agent_with_stable_prompt_cache_key(
        agent,
        provider=model_provider,
        model_name=model_name,
    )
    agent = agent_with_isolated_tool_state(agent)
    resolved_session = (
        session
        if session is not None
        else build_sdk_session_from_env()
        if inherit_env_session
        else None
    )
    prompt = sdk_input_from_typed_input(
        typed_input, live=live, provider=model_provider,
        trusted_attachment_paths=trusted_attachment_paths,
    )
    pre_model_source_audit_prompt = _sdk_input_audit_text(prompt)
    if decision_contract is not None:
        prompt = _sdk_prompt_with_recovery_context(
            prompt,
            decision_contract_prompt(decision_contract),
        )
    if tool_call_budget_contract is not None:
        prompt = _sdk_prompt_with_recovery_context(
            prompt,
            tool_call_budget_contract.prompt_context(),
        )
    base_prompt = prompt
    audit_prompt = _sdk_input_audit_text(prompt)
    request_cache = _sdk_request_cache_metadata(
        agent=agent,
        prompt=audit_prompt,
        session=resolved_session,
        max_turns=max_turns,
    )
    validation_diagnostics = OutputValidationDiagnostics(output_type)
    agent = agent_with_output_diagnostics(agent, validation_diagnostics)
    request_cache["semantic_attempt_turn_limits"] = {
        "schema": "keystone.semantic_attempt_turn_limits.v1",
        "initial": max_turns,
        "tool_correction": tool_correction_max_turns or max_turns,
        "decision_repair": decision_repair_max_turns or max_turns,
    }
    selected_tool_names = list(
        dict.fromkeys(
            str(getattr(tool, "name", "") or "").strip()
            for tool in (getattr(agent, "tools", []) or [])
            if str(getattr(tool, "name", "") or "").strip()
        )
    )
    request_cache["request_tool_scope"] = {
        "schema": "keystone.request_tool_scope.v1",
        "scope_source": "sdk_agent_attached_tools",
        "selected_tool_count": len(selected_tool_names),
        "selected_tool_names": selected_tool_names,
        **(
            {
                "tool_call_limits": {
                    limit.tool_name: limit.max_calls
                    for limit in tool_call_budget_contract.limits
                },
                "max_total_tool_calls": tool_call_budget_contract.max_total_calls,
            }
            if tool_call_budget_contract is not None
            else {}
        ),
    }
    rate_limit_retry_count = 0
    structured_output_retry_count = 0
    decision_repair_count = 0
    tool_correction_count = 0
    decision_attempts: list[dict[str, Any]] = []
    decision_normalizations: list[dict[str, Any]] = []
    decision_repair_evidence: dict[str, Any] | None = None
    sdk_attempt_usage_events: list[dict[str, Any]] = []
    prior_attempted_tool_names: list[str] = []
    prior_completed_tool_names: list[str] = []
    structured_replay_context = ""
    attested_replay_entries: tuple[Mapping[str, Any], ...] = ()
    search_telemetry: list[dict[str, Any]] = []
    max_rate_limit_retries = _sdk_rate_limit_max_retries(live=live, run_config=run_config)
    max_structured_output_retries = _sdk_structured_output_max_retries(
        live=live,
        run_config=run_config,
    )
    active_session = resolved_session
    raw_result: Any | None = None
    decision_raw_results: list[Any] = []
    prior_receipts = recovery_store.receipts if recovery_store is not None else []
    reset_tool_receipt_journal(
        prior_receipts,
        receipt_sink=recovery_store.record_receipt if recovery_store is not None else None,
    )
    tool_call_budget = (
        ToolCallBudgetLedger(tool_call_budget_contract)
        if tool_call_budget_contract is not None
        else None
    )
    instrument_agent_tools(agent, tool_call_budget=tool_call_budget)
    temporarily_disabled_tools: dict[int, tuple[Any, Any]] = {}
    if prior_receipts:
        prompt = _sdk_prompt_with_recovery_context(
            base_prompt,
            retry_receipt_context(prior_receipts),
        )
        _disable_completed_mutation_tools(
            agent,
            prior_receipts,
            temporarily_disabled_tools,
        )
    started_at = time.time()
    model_run_mode = "live_sdk" if live else ("local_sdk" if run_config is not None else "sdk")
    if decision_contract is not None:
        context_telemetry, context_failure = pre_model_decision_context_telemetry(
            decision_contract,
            model_input_text=pre_model_source_audit_prompt,
        )
        request_cache["pre_model_decision_context"] = context_telemetry
        if context_failure is not None:
            _attach_current_request_budget_snapshot(request_cache)
            _attach_tool_call_budget_snapshot(request_cache, tool_call_budget)
            error = AgentDecisionValidationError(context_failure)
            try:
                with execution_telemetry.span(
                    "sdk.pre_model_decision_context",
                    attributes={
                        "agent_name": agent.name,
                        "route": decision_contract.route,
                        "decision_stage": decision_contract.decision_stage,
                        "status": "rejected",
                    },
                ):
                    raise error
            except AgentDecisionValidationError:
                pass
            failure_metadata = {
                "schema": "keystone.sdk_run_failure.v1",
                "agent_name": agent.name,
                "provider": model_provider,
                "model": model_name,
                "run_mode": model_run_mode,
                "failure_kind": _failure_kind(error),
                "attempt_count": 0,
                "usage": {
                    "available": True,
                    "requests": 0,
                    "model_attempts_started": 0,
                    "provider_request_count_confirmed": True,
                    "note": "Context validation failed before any model request.",
                },
                "cost": {
                    "available": True,
                    "estimated_usd": 0.0,
                    "note": "No model request was made.",
                },
                "request_cache": request_cache,
                "tool_receipts": [
                    dict(receipt)
                    for receipt in preacquired_tool_receipts
                    if isinstance(receipt, Mapping)
                ],
                "preacquired_tool_receipts": [
                    dict(receipt)
                    for receipt in preacquired_tool_receipts
                    if isinstance(receipt, Mapping)
                ],
                "tool_invocations": [],
                "execution_telemetry": execution_telemetry.snapshot(status="failed").model_dump(
                    mode="json", by_alias=True
                ),
            }
            failure_metadata["tool_execution"] = _tool_execution_summary_from_journals(
                selected_tool_names=selected_tool_names,
                invocations=[],
                tool_receipts=failure_metadata["tool_receipts"],
                preacquired_tool_receipts=preacquired_tool_receipts,
                postcondition=None,
                failed=True,
            )
            failure_metadata["request_cache"]["tool_execution"] = failure_metadata["tool_execution"]
            _attach_sdk_run_failure_metadata(error, failure_metadata)
            raise error
    while True:
        reset_sdk_search_telemetry()
        set_sdk_search_request_context(audit_prompt)
        attempt_index = (
            rate_limit_retry_count
            + structured_output_retry_count
            + decision_repair_count
            + tool_correction_count
            + 1
        )
        attempt_max_turns = (
            decision_repair_max_turns
            if decision_repair_count and decision_repair_max_turns is not None
            else tool_correction_max_turns
            if tool_correction_count and tool_correction_max_turns is not None
            else max_turns
        )
        disabled_tool_names = [
            str(getattr(tool, "name", "") or "").strip()
            for tool, _prior_is_enabled in temporarily_disabled_tools.values()
            if str(getattr(tool, "name", "") or "").strip()
            and getattr(tool, "is_enabled", True) is False
        ]
        attempt_agent, tool_choice_adjustment = (
            agent_with_retry_compatible_tool_choice(
                agent,
                disabled_tool_names=disabled_tool_names,
            )
        )
        if tool_choice_adjustment is not None:
            request_cache.setdefault("retry_tool_choice_adjustments", []).append(
                {
                    **tool_choice_adjustment,
                    "attempt_index": attempt_index,
                }
            )
        # Do not let a later failed attempt inherit a prior attempt's SDK items.
        raw_result = None
        active_deadline = current_execution_deadline()
        attempt_deadline_stage = f"{agent.name}:semantic_attempt:{attempt_index}"
        validation_diagnostics.attempt = attempt_index
        try:
            if active_deadline is not None:
                active_deadline.admit(
                    stage=attempt_deadline_stage,
                    boundary="semantic_attempt",
                )
            with execution_telemetry.span(
                "sdk.model_attempt",
                attempt_index=attempt_index,
                attributes={
                    "agent_name": agent.name,
                    "provider": model_provider,
                    "model_name": model_name,
                    "run_mode": model_run_mode,
                    "live": live,
                    "max_turns": attempt_max_turns,
                },
            ):
                with activate_provider_read_context(
                    _provider_read_plan_for_agent(
                        agent.name,
                        typed_input=typed_input,
                    )
                ):
                    raw_result, output = run_typed_sdk_sync(
                        attempt_agent,
                        prompt,
                        output_type,
                        run_config=run_config,
                        live=live,
                        config=config,
                        session=active_session,
                        workflow_name=workflow_name,
                        group_id=group_id,
                        trace_metadata=trace_metadata,
                        tracing_disabled=tracing_disabled,
                        trace_include_sensitive_data=trace_include_sensitive_data,
                        trace_config=trace_config,
                        max_turns=attempt_max_turns,
                    )
            if active_deadline is not None:
                active_deadline.checkpoint(
                    stage=attempt_deadline_stage,
                    boundary="semantic_attempt",
                    phase="model_completed",
                )
            if tool_call_budget is not None:
                tool_call_budget.observe_hosted_calls(
                    sdk_tool_execution_records(raw_result)
                )
            _attach_current_request_budget_snapshot(request_cache)
            _attach_tool_call_budget_snapshot(request_cache, tool_call_budget)
            decision_raw_results.append(raw_result)
            sdk_attempt_usage_events.append(
                {
                    "attempt_index": attempt_index,
                    "usage": extract_sdk_usage(raw_result),
                }
            )
            request_cache["model_attempt_usage"] = list(sdk_attempt_usage_events)
            if tool_execution_contract is not None:
                current_tool_outcome = evaluate_tool_execution_contract(
                    raw_result,
                    tool_execution_contract,
                    tool_receipts=tool_receipt_journal(),
                    preacquired_receipts=preacquired_tool_receipts,
                    prior_attempted_tool_names=prior_attempted_tool_names,
                    prior_completed_tool_names=prior_completed_tool_names,
                )
                prior_attempted_tool_names = list(
                    current_tool_outcome.attempted_tool_names
                )
                prior_completed_tool_names = list(
                    current_tool_outcome.completed_tool_names
                )
                if not current_tool_outcome.satisfied:
                    if (
                        current_tool_outcome.prohibited_tool_names
                        or tool_correction_count >= 1
                    ):
                        raise ToolExecutionContractError(current_tool_outcome)
                    tool_correction_count += 1
                    request_cache["tool_execution_correction"] = {
                        "schema": "keystone.tool_execution_correction.v1",
                        "attempted": True,
                        "attempt": tool_correction_count,
                        "postcondition": current_tool_outcome.receipt(),
                    }
                    captured_receipts = tool_receipt_journal()
                    captured_invocations = tool_invocation_journal()
                    retry_state, disabled_completed_tool_names = (
                        _prepare_tools_for_retry(
                            agent,
                            raw_result=raw_result,
                            invocations=captured_invocations,
                            receipts=captured_receipts,
                            disabled_tools=temporarily_disabled_tools,
                            disable_completed_reads=True,
                        )
                    )
                    request_cache["tool_execution_correction"].update(
                        {
                            "disabled_completed_tool_names": list(
                                disabled_completed_tool_names
                            ),
                            "completed_read_tool_names": list(
                                retry_state.completed_read_tool_names
                            ),
                            "completed_mutation_tool_names": list(
                                retry_state.completed_mutation_tool_names
                            ),
                        }
                    )
                    tool_evidence_replay = build_cumulative_decision_repair_evidence_replay(
                        decision_raw_results,
                        SpecialistDecisionEvidence.build(
                            (),
                            require_complete_assessments=False,
                        ),
                        attested_read_outputs=attested_replay_entries,
                    )
                    if retry_state.completed_read_tool_names:
                        if not tool_evidence_replay.ready:
                            raise ToolEvidenceReplayError(
                                reason_code=(
                                    tool_evidence_replay.reason_code
                                    or "tool_correction_evidence_replay_unavailable"
                                ),
                                feedback=(
                                    tool_evidence_replay.feedback
                                    or (
                                        "A completed read cannot be repeated and its "
                                        "bounded output was unavailable for safe replay."
                                    )
                                ),
                            )
                        request_cache["tool_execution_correction"][
                            "evidence_replay"
                        ] = tool_evidence_replay.telemetry(
                            disabled_read_tool_names=(
                                retry_state.completed_read_tool_names
                            )
                        )
                    prompt = _sdk_prompt_with_recovery_context(
                        base_prompt,
                        tool_execution_correction_prompt(
                            tool_execution_contract,
                            current_tool_outcome,
                            invocations=captured_invocations,
                        ),
                    )
                    if tool_evidence_replay.ready:
                        prompt = _sdk_prompt_with_recovery_context(
                            prompt,
                            tool_evidence_replay.prompt_context,
                        )
                    if captured_receipts:
                        prompt = _sdk_prompt_with_recovery_context(
                            prompt,
                            retry_receipt_context(captured_receipts),
                        )
                    active_session = None
                    search_telemetry.extend(consume_sdk_search_telemetry())
                    continue
            if decision_contract is not None:
                if decision_contract.output_normalizer is not None:
                    normalization = decision_contract.output_normalizer(output)
                    if normalization is not None:
                        decision_normalizations.append(
                            {
                                **dict(normalization),
                                "attempt_index": attempt_index,
                            }
                        )
                        request_cache["decision_normalizations"] = list(
                            decision_normalizations
                        )
                verified_decision_fingerprints = (
                    verified_candidate_fingerprints_from_receipts(
                        [*preacquired_tool_receipts, *tool_receipt_journal()]
                    )
                )
                output_decision_evidence = decision_contract.evidence_resolver(output)
                tool_decision_evidence = (
                    decision_contract.tool_evidence_resolver(decision_raw_results)
                    if decision_contract.tool_evidence_resolver is not None
                    else SpecialistDecisionEvidence.build(())
                )
                decision_evidence = bind_authoritative_tool_evidence(
                    output_decision_evidence,
                    tool_decision_evidence,
                )
                decision_outcome, decision_evidence = validate_specialist_decision(
                    output,
                    decision_contract,
                    verified_candidate_fingerprints=verified_decision_fingerprints,
                    evidence_override=decision_evidence,
                )
                decision_outcome = decision_outcome.model_copy(
                    update={"repair_attempted": decision_repair_count > 0}
                )
                decision_attempt = decision_validation_telemetry(
                    output,
                    decision_contract,
                    decision_evidence,
                    decision_outcome,
                    attempt=decision_repair_count + 1,
                    tool_mode=(
                        "verified_context_tool_free"
                        if decision_repair_count and decision_repair_evidence is not None
                        else "model_called"
                    ),
                )
                decision_attempts.append(decision_attempt)
                decision_attempt["candidate_universe_source"] = (
                    "model_tool_outputs"
                    if tool_decision_evidence.candidate_ids
                    else decision_contract.pre_model_context_source
                )
                request_cache["decision_ownership"] = {
                    "schema": "keystone.agent_decision_run.v1",
                    "route": decision_contract.route,
                    "decision_owner": decision_attempt["decision_owner"],
                    "decision_stage": decision_contract.decision_stage,
                    "attempt_count": len(decision_attempts),
                    "repair_attempted": decision_repair_count > 0,
                    "attempts": list(decision_attempts),
                    "candidate_ids": list(decision_attempt["candidate_ids"]),
                    "selected_candidate_ids": list(decision_attempt["selected_candidate_ids"]),
                    "excluded_candidate_ids": list(decision_attempt["excluded_candidate_ids"]),
                    "reasoning": str(decision_attempt.get("reasoning") or ""),
                    "limitations": list(decision_attempt.get("limitations") or []),
                    "events": [
                        event
                        for attempt_payload in decision_attempts
                        for event in list(attempt_payload.get("events") or [])
                    ],
                    "validator_outcome": decision_outcome.model_dump(mode="json"),
                    **(
                        {"repair_evidence": dict(decision_repair_evidence)}
                        if decision_repair_evidence is not None
                        else {}
                    ),
                }
                if decision_outcome.status != "accepted":
                    prior_reason_codes = {
                        str(
                            attempt_payload.get("validator_outcome", {}).get(
                                "reason_code"
                            )
                            or ""
                        )
                        for attempt_payload in decision_attempts[:-1]
                    }
                    repeated_reason = bool(
                        decision_repair_count
                        and decision_outcome.reason_code in prior_reason_codes
                    )
                    if (
                        decision_repair_count
                        < max(0, int(decision_contract.max_decision_repairs))
                        and not repeated_reason
                    ):
                        replay = build_cumulative_decision_repair_evidence_replay(
                            decision_raw_results,
                            decision_evidence,
                            verified_candidate_fingerprints=(
                                verified_decision_fingerprints
                            ),
                            attested_read_outputs=attested_replay_entries,
                        )
                        if replay.required and not replay.ready:
                            replay_telemetry = replay.telemetry()
                            request_cache["decision_repair_evidence"] = replay_telemetry
                            request_cache["decision_ownership"]["repair_evidence"] = (
                                replay_telemetry
                            )
                            blocked_outcome = decision_outcome.model_copy(
                                update={
                                    "status": "rejected",
                                    "reason_code": replay.reason_code,
                                    "feedback": replay.feedback,
                                }
                            )
                            request_cache["decision_ownership"]["validator_outcome"] = (
                                blocked_outcome.model_dump(mode="json")
                            )
                            raise AgentDecisionValidationError(blocked_outcome)
                        decision_repair_count += 1
                        request_cache["decision_ownership"]["repair_attempted"] = True
                        captured_receipts = tool_receipt_journal()
                        captured_invocations = tool_invocation_journal()
                        retry_state, disabled_retry_tool_names = (
                            _prepare_tools_for_retry(
                                agent,
                                raw_result=raw_result,
                                invocations=captured_invocations,
                                receipts=captured_receipts,
                                disabled_tools=temporarily_disabled_tools,
                                disable_completed_reads=False,
                            )
                        )
                        disabled_read_tool_names: tuple[str, ...] = ()
                        if replay.ready:
                            disabled_read_tool_names = _disable_decision_repair_tools(
                                agent,
                                temporarily_disabled_tools,
                            )
                            decision_repair_evidence = replay.telemetry(
                                disabled_read_tool_names=disabled_read_tool_names,
                            )
                            request_cache["decision_repair_evidence"] = dict(
                                decision_repair_evidence
                            )
                            request_cache["decision_ownership"]["repair_evidence"] = dict(
                                decision_repair_evidence
                            )
                        elif _has_verified_preacquired_decision_context(
                            decision_contract,
                            verified_candidate_fingerprints=(
                                verified_decision_fingerprints
                            ),
                        ):
                            disabled_read_tool_names = _disable_decision_repair_tools(
                                agent,
                                temporarily_disabled_tools,
                            )
                        else:
                            disabled_read_tool_names = _disable_decision_repair_tools(
                                agent,
                                temporarily_disabled_tools,
                            )
                            decision_repair_evidence = {
                                "schema": "keystone.decision_repair_evidence_replay.v1",
                                "status": "not_required",
                                "mode": "original_model_input_tool_free",
                                "source": "original_model_input",
                                "provider_calls_during_repair": 0,
                                "disabled_read_tool_names": list(
                                    disabled_read_tool_names
                                ),
                                "candidate_ids": list(
                                    decision_contract.pre_model_candidate_ids
                                ),
                            }
                            request_cache["decision_repair_evidence"] = dict(
                                decision_repair_evidence
                            )
                            request_cache["decision_ownership"]["repair_evidence"] = dict(
                                decision_repair_evidence
                            )
                        request_cache["decision_ownership"]["retry_safety"] = {
                            "disabled_completed_tool_names": list(
                                dict.fromkeys(
                                    [
                                        *disabled_retry_tool_names,
                                        *disabled_read_tool_names,
                                    ]
                                )
                            ),
                            "completed_mutation_tool_names": list(
                                retry_state.completed_mutation_tool_names
                            ),
                        }
                        prompt = _sdk_prompt_with_recovery_context(
                            base_prompt,
                            decision_repair_prompt(
                                decision_contract,
                                decision_evidence,
                                decision_outcome,
                                evidence_replay=replay,
                            ),
                        )
                        active_session = None
                        search_telemetry.extend(consume_sdk_search_telemetry())
                        continue
                    error = AgentDecisionValidationError(decision_outcome)
                    with execution_telemetry.span(
                        "sdk.decision_validation",
                        attempt_index=decision_repair_count + 1,
                        attributes={
                            "agent_name": agent.name,
                            "route": decision_contract.route,
                            "decision_stage": decision_contract.decision_stage,
                        },
                    ):
                        raise error
            search_telemetry.extend(consume_sdk_search_telemetry())
            break
        except Exception as exc:
            terminal_diagnostics = response_terminal_diagnostics(exc)
            if terminal_diagnostics:
                request_cache["response_terminal"] = terminal_diagnostics
            if isinstance(exc, ValidationError):
                try:
                    validation_diagnostics.capture(exc)
                except Exception:
                    pass
            if _is_sdk_structured_output_error(exc):
                validation_diagnostics.record_unobserved_schema_error()
            if validation_diagnostics.failures:
                request_cache["validation_diagnostics"] = validation_diagnostics.snapshot()
            if active_deadline is not None:
                active_deadline.checkpoint(
                    stage=attempt_deadline_stage,
                    boundary="semantic_attempt",
                    phase="failed",
                )
            _attach_current_request_budget_snapshot(request_cache)
            _attach_tool_call_budget_snapshot(request_cache, tool_call_budget)
            search_telemetry.extend(consume_sdk_search_telemetry())
            if not any(
                int(event.get("attempt_index") or 0) == attempt_index
                for event in sdk_attempt_usage_events
            ):
                sdk_attempt_usage_events.append(
                    {
                        "attempt_index": attempt_index,
                        "usage": _failed_sdk_attempt_usage(exc),
                    }
                )
                request_cache["model_attempt_usage"] = list(sdk_attempt_usage_events)
            structured_retry_eligible = bool(
                structured_output_retry_count < max_structured_output_retries
                and _is_sdk_structured_output_error(exc)
            )
            rate_limit_retry_eligible = bool(
                rate_limit_retry_count < max_rate_limit_retries
                and _is_sdk_rate_limit_error(exc)
            )
            rate_limit_retry_delay: float | None = None
            if rate_limit_retry_eligible:
                rate_limit_retry_delay = _sdk_rate_limit_retry_delay_seconds(
                    exc,
                    rate_limit_retry_count + 1,
                )
                if active_deadline is not None:
                    try:
                        rate_limit_retry_delay = active_deadline.admit_wait(
                            stage=f"{agent.name}:rate_limit_retry_wait",
                            boundary="retry_wait",
                            wait_seconds=rate_limit_retry_delay,
                        )
                    except ExecutionDeadlineExceeded as deadline_error:
                        exc = deadline_error
                        rate_limit_retry_eligible = False
            captured_retry_receipts = tool_receipt_journal()
            captured_retry_invocations = tool_invocation_journal()
            if structured_retry_eligible or rate_limit_retry_eligible:
                try:
                    retry_state, disabled_completed_tool_names = (
                        _prepare_tools_for_retry(
                            agent,
                            raw_result=raw_result,
                            invocations=captured_retry_invocations,
                            receipts=captured_retry_receipts,
                            disabled_tools=temporarily_disabled_tools,
                            disable_completed_reads=True,
                            allow_durable_mutation_continuation=(
                                structured_retry_eligible and recovery_store is None
                            ),
                        )
                    )
                except UnsafeMutationRetryError as retry_error:
                    exc = retry_error
                    structured_retry_eligible = False
                    rate_limit_retry_eligible = False
                else:
                    request_cache.setdefault("retry_safety", []).append(
                        {
                            "attempt_index": attempt_index,
                            "retry_kind": (
                                "structured_output"
                                if structured_retry_eligible
                                else "rate_limit"
                            ),
                            "disabled_completed_tool_names": list(
                                disabled_completed_tool_names
                            ),
                            "completed_read_tool_names": list(
                                retry_state.completed_read_tool_names
                            ),
                            "completed_mutation_tool_names": list(
                                retry_state.completed_mutation_tool_names
                            ),
                        }
                    )
            if structured_retry_eligible:
                structured_output_retry_count += 1
                # A failed structured turn may already have persisted its user
                # input without a valid assistant response. Retry from the same
                # bounded prompt without carrying that partial session forward.
                active_session = None
                prompt = base_prompt
                if structured_retry_evidence_provider is not None:
                    replay_entries = tuple(structured_retry_evidence_provider())
                    if replay_entries:
                        replay_context = _structured_output_retry_evidence_context(
                            replay_entries
                        )
                        structured_replay_context = replay_context
                        prompt = _sdk_prompt_with_recovery_context(
                            prompt,
                            replay_context,
                        )
                        attested_replay_entries = _verified_replayed_read_entries(
                            replay_entries, captured_retry_invocations,
                        )
                        replayed_read_names = tuple(dict.fromkeys(
                            str(entry["tool_name"]) for entry in attested_replay_entries
                        ))
                        prior_attempted_tool_names = list(dict.fromkeys([
                            *prior_attempted_tool_names, *replayed_read_names,
                        ]))
                        prior_completed_tool_names = list(dict.fromkeys([
                            *prior_completed_tool_names, *replayed_read_names,
                        ]))
                        replay_fingerprint = sha256(
                            dumps(
                                list(replay_entries),
                                ensure_ascii=True,
                                sort_keys=True,
                                default=str,
                            ).encode("utf-8")
                        ).hexdigest()
                        request_cache["structured_output_retry_evidence_replay"] = {
                            "schema": "keystone.structured_retry_evidence_replay.v1",
                            "status": "model_visible",
                            "entry_count": len(replay_entries),
                            "evidence_sha256": replay_fingerprint,
                            "provider_calls_during_replay": 0,
                            "verified_completed_read_tool_names": list(replayed_read_names),
                        }
                if captured_retry_receipts:
                    prompt = _sdk_prompt_with_recovery_context(
                        prompt,
                        retry_receipt_context(captured_retry_receipts),
                    )
                validation_feedback = structured_output_retry_feedback(
                    request_cache.get("validation_diagnostics"), attempt=attempt_index,
                )
                if validation_feedback:
                    prompt = _sdk_prompt_with_recovery_context(prompt, validation_feedback)
                continue
            if rate_limit_retry_eligible:
                rate_limit_retry_count += 1
                if captured_retry_receipts:
                    prompt = _sdk_prompt_with_recovery_context(
                        base_prompt,
                        retry_receipt_context(captured_retry_receipts),
                    )
                    if structured_replay_context:
                        prompt = _sdk_prompt_with_recovery_context(
                            prompt, structured_replay_context,
                        )
                    active_session = None
                time.sleep(float(rate_limit_retry_delay or 0.0))
                if active_deadline is not None:
                    active_deadline.checkpoint(
                        stage=f"{agent.name}:rate_limit_retry_wait",
                        boundary="retry_wait",
                        phase="completed",
                    )
                continue
            else:
                for tool, prior_is_enabled in temporarily_disabled_tools.values():
                    tool.is_enabled = prior_is_enabled
                captured_failure_receipts = tool_receipt_journal()
                failure_invocations = tool_invocation_journal()
                all_failure_receipts = [
                    *[
                        dict(receipt)
                        for receipt in preacquired_tool_receipts
                        if isinstance(receipt, Mapping)
                    ],
                    *captured_failure_receipts,
                ]
                # Model responses can be syntactically valid while a post-model
                # contract rejects their tool evidence or semantic decision.
                # Record that boundary explicitly so terminal failure telemetry
                # never misrepresents the completed model span as overall success.
                try:
                    with execution_telemetry.span(
                        "sdk.post_model_contract_failure",
                        attempt_index=attempt_index,
                        attributes={
                            "agent_name": agent.name,
                            "failure_kind": _failure_kind(exc),
                        },
                    ):
                        raise exc
                except Exception:
                    pass
                failure_telemetry = execution_telemetry.snapshot(status="failed").model_dump(
                    mode="json", by_alias=True
                )
                observed_attempt_usage = _usage_with_explicit_prompt_cache_metadata(
                    _aggregate_sdk_attempt_usage(sdk_attempt_usage_events),
                    request_cache=request_cache,
                )
                if observed_attempt_usage.get("available") is True:
                    failure_usage = {
                        **observed_attempt_usage,
                        "model_attempts_started": attempt_index,
                        "provider_request_count_confirmed": bool(
                            observed_attempt_usage.get("complete") is True
                            and observed_attempt_usage.get("requests") is not None
                        ),
                        "note": (
                            "Usage covers every observed model response in this "
                            "failed run."
                            if observed_attempt_usage.get("complete") is True
                            else (
                                "Usage is a lower bound from completed model attempts; "
                                "at least one failed provider attempt did not expose usage."
                            )
                        ),
                    }
                    failure_cost = estimate_usage_cost(
                        provider=model_provider,
                        model=model_name,
                        usage=failure_usage,
                    )
                    if observed_attempt_usage.get("complete") is not True:
                        failure_cost = {
                            **failure_cost,
                            "complete": False,
                            "note": (
                                "Any estimate covers completed attempts only; cost for "
                                "failed attempts without usage remains unknown."
                            ),
                        }
                else:
                    request_counts = [
                        event["usage"].get("requests")
                        for event in sdk_attempt_usage_events
                    ]
                    observed_requests = (
                        sum(request_counts)
                        if request_counts and all(type(value) is int for value in request_counts)
                        else None
                    )
                    failure_usage = {
                        "available": False,
                        "complete": False,
                        "requests": observed_requests,
                        "model_attempts_started": attempt_index,
                        "provider_request_count_confirmed": False,
                        "note": (
                            "Token usage is unavailable. Any request count comes from "
                            "observed SDK callbacks; unobserved provider consumption "
                            "remains unknown."
                        ),
                    }
                    failure_cost = {
                        "available": False,
                        "note": (
                            "Cost is unknown because token usage was unavailable for "
                            "the failed SDK run."
                        ),
                    }
                if rate_limit_retry_count:
                    request_cache["rate_limit_retries"] = rate_limit_retry_count
                if structured_output_retry_count:
                    request_cache["structured_output_retries"] = (
                        structured_output_retry_count
                    )
                    request_cache["structured_output_retry_session_reset"] = True
                if decision_repair_count:
                    request_cache["decision_repairs"] = decision_repair_count
                if tool_correction_count:
                    request_cache["tool_corrections"] = tool_correction_count
                failure_metadata = {
                    "schema": "keystone.sdk_run_failure.v1",
                    "agent_name": agent.name,
                    "provider": model_provider,
                    "model": model_name,
                    "run_mode": model_run_mode,
                    "failure_kind": _failure_kind(exc),
                    "attempt_count": attempt_index,
                    "usage": failure_usage,
                    "cost": failure_cost,
                    "request_cache": {
                        **request_cache,
                        "failed_model_attempts": attempt_index,
                    },
                    "tool_receipts": all_failure_receipts,
                    "preacquired_tool_receipts": [
                        dict(receipt)
                        for receipt in preacquired_tool_receipts
                        if isinstance(receipt, Mapping)
                    ],
                    "tool_invocations": failure_invocations,
                    "execution_telemetry": failure_telemetry,
                }
                if tool_execution_contract is not None:
                    failure_tool_outcome = evaluate_tool_execution_contract(
                        None,
                        tool_execution_contract,
                        tool_receipts=captured_failure_receipts,
                        preacquired_receipts=preacquired_tool_receipts,
                        prior_attempted_tool_names=prior_attempted_tool_names,
                        prior_completed_tool_names=prior_completed_tool_names,
                    )
                    failure_metadata["tool_execution_postcondition"] = (
                        failure_tool_outcome.receipt()
                    )
                    failure_metadata["request_cache"]["tool_execution_postcondition"] = (
                        failure_tool_outcome.receipt()
                    )
                failure_metadata["tool_execution"] = _tool_execution_summary_from_journals(
                    selected_tool_names=selected_tool_names,
                    invocations=failure_invocations,
                    tool_receipts=all_failure_receipts,
                    preacquired_tool_receipts=preacquired_tool_receipts,
                    postcondition=failure_metadata.get("tool_execution_postcondition"),
                    failed=True,
                    raw_result=raw_result,
                )
                failure_metadata["request_cache"]["tool_execution"] = failure_metadata[
                    "tool_execution"
                ]
                guardrail_diagnostics = _sdk_guardrail_failure_diagnostics(exc)
                if guardrail_diagnostics:
                    failure_metadata["guardrail"] = guardrail_diagnostics
                if isinstance(exc, ModuleNotFoundError):
                    missing_module = re.sub(
                        r"[^A-Za-z0-9_.-]+",
                        "",
                        str(getattr(exc, "name", "") or ""),
                    )[:160]
                    if missing_module:
                        failure_metadata["missing_module"] = missing_module
                _attach_sdk_run_failure_metadata(exc, failure_metadata)
                _record_sdk_run_summary_safely(
                    agent_name=agent.name,
                    model_provider=model_provider,
                    model_name=model_name,
                    model_run_mode=model_run_mode,
                    live=live,
                    request_cache=request_cache,
                    usage=failure_metadata["usage"],
                    cost=failure_metadata["cost"],
                    budget_guard={},
                    search_telemetry=search_telemetry,
                    raw_result=None,
                    trace_metadata=trace_metadata,
                    status="error",
                    failure_kind=_failure_kind(exc),
                    retry_count=(
                        rate_limit_retry_count
                        + structured_output_retry_count
                        + decision_repair_count
                        + tool_correction_count
                    ),
                    duration_ms=round((time.time() - started_at) * 1000, 3),
                    execution_telemetry=failure_telemetry,
                )
                if recovery_store is not None and recovery_store.receipts:
                    partial_success = recovery_store.mark_failed(
                        stage=failure_stage_from_exception(exc),
                        failure_code=_failure_kind(exc),
                        failure_summary=str(exc),
                    )
                    partial_error = ProviderPartialSuccessError(
                        partial_success,
                        cause=exc,
                    )
                    _attach_sdk_run_failure_metadata(
                        partial_error,
                        failure_metadata,
                    )
                    raise partial_error from exc
                raise exc
    for tool, prior_is_enabled in temporarily_disabled_tools.values():
        tool.is_enabled = prior_is_enabled
    captured_tool_receipts = tool_receipt_journal()
    captured_tool_invocations = tool_invocation_journal()
    output = _attach_retrieval_diagnostics(output, search_telemetry)
    search_diagnostics = sdk_search_diagnostics_from_telemetry(search_telemetry)
    tool_execution_outcome = (
        evaluate_tool_execution_contract(
            raw_result,
            tool_execution_contract,
            tool_receipts=captured_tool_receipts,
            preacquired_receipts=preacquired_tool_receipts,
            prior_attempted_tool_names=prior_attempted_tool_names,
            prior_completed_tool_names=prior_completed_tool_names,
        )
        if tool_execution_contract is not None
        else None
    )
    if tool_execution_outcome is not None:
        request_cache["tool_execution_postcondition"] = tool_execution_outcome.receipt()
    request_cache["tool_invocations"] = captured_tool_invocations
    request_cache["tool_execution"] = _tool_execution_summary_from_journals(
        selected_tool_names=selected_tool_names,
        invocations=captured_tool_invocations,
        tool_receipts=captured_tool_receipts,
        preacquired_tool_receipts=preacquired_tool_receipts,
        postcondition=(
            tool_execution_outcome.receipt() if tool_execution_outcome is not None else None
        ),
        failed=False,
        raw_result=raw_result,
    )
    usage = _usage_with_explicit_prompt_cache_metadata(
        _aggregate_sdk_attempt_usage(sdk_attempt_usage_events),
        request_cache=request_cache,
    )
    cost = estimate_usage_cost(provider=model_provider, model=model_name, usage=usage)
    if usage.get("complete") is not True:
        cost = {
            **cost,
            "complete": False,
            "note": (
                "Any estimate covers attempts with reported usage only; at least one "
                "failed SDK attempt did not expose token usage."
            ),
        }
    try:
        with execution_telemetry.span(
            "sdk.budget_check",
            attributes={
                "agent_name": agent.name,
                "provider": model_provider,
                "model_name": model_name,
                "live": live,
            },
        ):
            budget_guard = enforce_agent_run_budget(
                agent_name=agent.name,
                provider=model_provider,
                model=model_name,
                cost=cost,
                strict_unknown_cost=live and run_config is None,
            )
    except Exception as exc:
        failure_telemetry = execution_telemetry.snapshot(status="failed").model_dump(
            mode="json", by_alias=True
        )
        _attach_sdk_run_failure_metadata(
            exc,
            {
                "schema": "keystone.sdk_run_failure.v1",
                "agent_name": agent.name,
                "provider": model_provider,
                "model": model_name,
                "run_mode": model_run_mode,
                "failure_kind": _failure_kind(exc),
                "attempt_count": (
                    rate_limit_retry_count
                    + structured_output_retry_count
                    + decision_repair_count
                    + tool_correction_count
                    + 1
                ),
                "usage": usage,
                "cost": cost,
                "request_cache": request_cache,
                "tool_receipts": captured_tool_receipts,
                "preacquired_tool_receipts": [
                    dict(receipt)
                    for receipt in preacquired_tool_receipts
                    if isinstance(receipt, Mapping)
                ],
                "tool_invocations": captured_tool_invocations,
                "tool_execution": request_cache["tool_execution"],
                "execution_telemetry": failure_telemetry,
            },
        )
        _record_sdk_run_summary_safely(
            agent_name=agent.name,
            model_provider=model_provider,
            model_name=model_name,
            model_run_mode=model_run_mode,
            live=live,
            request_cache=request_cache,
            usage=usage,
            cost=cost,
            budget_guard={},
            search_telemetry=search_telemetry,
            raw_result=raw_result,
            trace_metadata=trace_metadata,
            status="error",
            failure_kind=_failure_kind(exc),
            retry_count=(
                rate_limit_retry_count
                + structured_output_retry_count
                + decision_repair_count
                + tool_correction_count
            ),
            duration_ms=round((time.time() - started_at) * 1000, 3),
            execution_telemetry=failure_telemetry,
        )
        raise
    if tool_execution_outcome is not None and not tool_execution_outcome.satisfied:
        failure_kind = "required_tool_execution_missing"
        if tool_execution_outcome.prohibited_tool_names:
            failure_kind = "forbidden_tool_execution_observed"
        error = ToolExecutionContractError(tool_execution_outcome)
        try:
            with execution_telemetry.span(
                "sdk.tool_execution_postcondition",
                attributes={
                    "agent_name": agent.name,
                    "stage": tool_execution_outcome.stage,
                    "failure_kind": failure_kind,
                },
            ):
                raise error
        except ToolExecutionContractError:
            pass
        failure_telemetry = execution_telemetry.snapshot(status="failed").model_dump(
            mode="json", by_alias=True
        )
        failure_metadata = {
            "schema": "keystone.sdk_run_failure.v1",
            "agent_name": agent.name,
            "provider": model_provider,
            "model": model_name,
            "run_mode": model_run_mode,
            "failure_kind": failure_kind,
            "attempt_count": (
                rate_limit_retry_count
                + structured_output_retry_count
                + decision_repair_count
                + tool_correction_count
                + 1
            ),
            "usage": usage,
            "cost": cost,
            "budget_guard": budget_guard,
            "request_cache": request_cache,
            "tool_execution_postcondition": tool_execution_outcome.receipt(),
            "tool_receipts": captured_tool_receipts,
            "preacquired_tool_receipts": [
                dict(receipt)
                for receipt in preacquired_tool_receipts
                if isinstance(receipt, Mapping)
            ],
            "tool_invocations": captured_tool_invocations,
            "tool_execution": request_cache["tool_execution"],
            "execution_telemetry": failure_telemetry,
        }
        _attach_sdk_run_failure_metadata(error, failure_metadata)
        _record_sdk_run_summary_safely(
            agent_name=agent.name,
            model_provider=model_provider,
            model_name=model_name,
            model_run_mode=model_run_mode,
            live=live,
            request_cache=request_cache,
            usage=usage,
            cost=cost,
            budget_guard=budget_guard,
            search_telemetry=search_telemetry,
            search_diagnostics=search_diagnostics,
            raw_result=raw_result,
            trace_metadata=trace_metadata,
            status="error",
            failure_kind=failure_kind,
            retry_count=(
                rate_limit_retry_count
                + structured_output_retry_count
                + decision_repair_count
                + tool_correction_count
            ),
            duration_ms=round((time.time() - started_at) * 1000, 3),
            execution_telemetry=failure_telemetry,
        )
        raise error
    execution_telemetry.mark_final_response()
    _attach_current_request_budget_snapshot(request_cache)
    _attach_tool_call_budget_snapshot(request_cache, tool_call_budget)
    execution_telemetry_payload = execution_telemetry.snapshot(status="completed").model_dump(
        mode="json", by_alias=True
    )
    _record_sdk_run_summary_safely(
        agent_name=agent.name,
        model_provider=model_provider,
        model_name=model_name,
        model_run_mode=model_run_mode,
        live=live,
        request_cache=request_cache,
        usage=usage,
        cost=cost,
        budget_guard=budget_guard,
        search_telemetry=search_telemetry,
        search_diagnostics=search_diagnostics,
        raw_result=raw_result,
        trace_metadata=trace_metadata,
        status="ok",
        retry_count=(
            rate_limit_retry_count
            + structured_output_retry_count
            + decision_repair_count
            + tool_correction_count
        ),
        duration_ms=round((time.time() - started_at) * 1000, 3),
        execution_telemetry=execution_telemetry_payload,
    )
    return TypedAgentRunResult(
        agent_name=agent.name,
        output=output,
        raw_result=raw_result,
        live=live,
        usage=usage,
        cost=cost,
        budget_guard=budget_guard,
        request_cache={
            **request_cache,
            **({"rate_limit_retries": rate_limit_retry_count} if rate_limit_retry_count else {}),
            **(
                {
                    "structured_output_retries": structured_output_retry_count,
                    "structured_output_retry_session_reset": True,
                }
                if structured_output_retry_count
                else {}
            ),
            **({"decision_repairs": decision_repair_count} if decision_repair_count else {}),
            **({"tool_corrections": tool_correction_count} if tool_correction_count else {}),
        },
        execution_telemetry=execution_telemetry_payload,
        tool_receipts=captured_tool_receipts,
    )


def _attach_current_request_budget_snapshot(request_cache: dict[str, Any]) -> None:
    """Expose active request-count and time ledgers without changing behavior."""

    snapshot = current_model_request_budget_snapshot()
    if snapshot is not None:
        request_cache["request_budget"] = snapshot
    deadline_snapshot = current_execution_deadline_snapshot()
    if deadline_snapshot is not None:
        request_cache["execution_deadline"] = deadline_snapshot


def _attach_tool_call_budget_snapshot(
    request_cache: dict[str, Any],
    budget: ToolCallBudgetLedger | None,
) -> None:
    """Expose pre-call tool admissions without provider inputs or outputs."""

    if budget is not None:
        request_cache["tool_call_budget"] = budget.snapshot()


def _tool_execution_summary_from_journals(
    *,
    selected_tool_names: Sequence[str],
    invocations: Sequence[Mapping[str, Any]],
    tool_receipts: Sequence[Mapping[str, Any] | Any],
    preacquired_tool_receipts: Sequence[Mapping[str, Any] | Any],
    postcondition: Mapping[str, Any] | None,
    failed: bool,
    raw_result: Any | None = None,
) -> dict[str, Any]:
    """Build one terminal tool envelope from call-boundary evidence."""

    journal_started = [
        str(item.get("tool_name") or "").strip()
        for item in invocations
        if str(item.get("status") or "") == "started" and str(item.get("tool_name") or "").strip()
    ]
    sdk_records = sdk_tool_execution_records(raw_result) if raw_result is not None else ()
    sdk_started = [record.tool_name for record in sdk_records]
    started = journal_started or sdk_started
    preacquired_names = [
        str(receipt.get("tool_name") or receipt.get("provider") or "").strip()
        for receipt in preacquired_tool_receipts
        if isinstance(receipt, Mapping)
        and str(receipt.get("tool_name") or receipt.get("provider") or "").strip()
    ]
    if started:
        mode = "llm_selected_function_tools"
    elif preacquired_names:
        mode = "preacquired_provider_context"
    elif selected_tool_names:
        mode = "model_tools_attached_no_call"
    else:
        mode = "tool_free"
    if failed:
        mode = f"{mode}_failed"
    durable_receipts: list[Mapping[str, Any]] = []
    seen_receipts: set[str] = set()
    for receipt in [*tool_receipts, *preacquired_tool_receipts]:
        if not isinstance(receipt, Mapping):
            continue
        key = dumps(dict(receipt), ensure_ascii=True, sort_keys=True, default=str)
        if key in seen_receipts:
            continue
        seen_receipts.add(key)
        durable_receipts.append(receipt)
    preacquired_receipts = [
        receipt for receipt in preacquired_tool_receipts if isinstance(receipt, Mapping)
    ]
    provider_attempt_counts = [
        int(receipt["provider_request_attempt_count"])
        for receipt in durable_receipts
        if isinstance(receipt.get("provider_request_attempt_count"), int)
    ]
    provider_success_counts = [
        int(receipt["provider_request_success_count"])
        for receipt in durable_receipts
        if isinstance(receipt.get("provider_request_success_count"), int)
    ]
    return build_tool_execution_summary(
        mode=mode,
        scope_source="shared_sdk_runner",
        selected_tool_names=selected_tool_names,
        model_called_tool_names=started,
        model_tool_call_count=len(started),
        tool_output_count=sum(
            1
            for item in invocations
            if str(item.get("status") or "")
            in {"completed", "reused_verified", "reused_verified_read"}
        )
        or sum(1 for record in sdk_records if record.succeeded),
        workflow_called_tool_names=(),
        workflow_tool_call_count=0,
        preacquired_context_tool_names=preacquired_names,
        preacquired_context_count=len(preacquired_receipts),
        preacquired_context_source=(
            "preacquired_provider_context" if preacquired_names else ""
        ),
        provider_receipt_count=len(durable_receipts),
        distinct_persisted_receipt_count=len(durable_receipts),
        receipt_observation_count=len(tool_receipts) + len(preacquired_tool_receipts),
        provider_request_attempt_count=(
            sum(provider_attempt_counts) if provider_attempt_counts else None
        ),
        provider_request_success_count=(
            sum(provider_success_counts) if provider_success_counts else None
        ),
        context_receipt_count=len(preacquired_receipts),
        context_receipt_source=("preacquired_provider_context" if preacquired_names else ""),
        postcondition=postcondition,
    )


def _provider_read_plan_for_agent(
    agent_name: str,
    *,
    typed_input: Any = None,
) -> ProviderReadPlan | None:
    """Return a bounded request-local read context for direct provider owners."""

    provider_by_agent = {
        "airtable_context_agent": "airtable",
        "gmail_triage": "gmail",
        "google_workspace_context_agent": "google_workspace",
        "zotero_context_agent": "zotero",
    }
    normalized_agent = str(agent_name or "").strip().lower()
    provider = provider_by_agent.get(normalized_agent)
    if provider is None and normalized_agent == "chief_of_staff":
        provider = _chief_single_provider_read(typed_input)
    if provider is None:
        return None
    return ProviderReadPlan(
        provider=provider,
        operation="read",
        resource="agent_request",
        policy=ProviderReadPolicy(
            max_items=500,
            max_pages=50,
            max_bytes=20 * 1024 * 1024,
            max_provider_calls=100,
            timeout_seconds=120.0,
            max_retries=1,
            max_concurrency=4,
        ),
    )


def _chief_single_provider_read(typed_input: Any) -> str | None:
    """Return one canonical read-only provider owned by a Chief request."""

    if not isinstance(typed_input, Mapping):
        return None
    raw_plan = typed_input.get("manual_request_plan")
    if hasattr(raw_plan, "model_dump"):
        raw_plan = raw_plan.model_dump(mode="python")
    if not isinstance(raw_plan, Mapping):
        return None
    provider = str(raw_plan.get("provider_system") or "").strip().lower()
    if provider not in {
        "airtable",
        "gmail",
        "google_calendar",
        "google_workspace",
        "zotero",
    }:
        return None
    operations = {
        str(value or "").strip().lower()
        for value in (raw_plan.get("provider_operations") or ())
        if str(value or "").strip()
    }
    if not operations or operations.intersection(
        {
            "create",
            "delete",
            "label",
            "modify",
            "post",
            "remove",
            "send",
            "trash",
            "update",
            "write",
        }
    ):
        return None
    return provider


def _disable_completed_mutation_tools(
    agent: AgentLike,
    receipts: list[dict[str, Any]],
    disabled_tools: dict[int, tuple[Any, Any]],
) -> None:
    """Disable mutation tools represented by an authoritative saved receipt."""

    disabled_names = mutation_tool_names(receipts)
    for tool in list(getattr(agent, "tools", []) or []):
        if str(getattr(tool, "name", "") or "") not in disabled_names:
            continue
        tool_id = id(tool)
        if tool_id not in disabled_tools:
            disabled_tools[tool_id] = (tool, getattr(tool, "is_enabled", True))
        tool.is_enabled = False


def _tool_retry_state(
    raw_result: Any,
    invocations: Sequence[Mapping[str, Any]],
) -> _ToolRetryState:
    """Classify completed reads and fail-closed mutation retry evidence."""

    completed_reads: set[str] = set()
    completed_mutations: set[str] = set()
    uncertain_mutations: set[str] = set()

    invocation_states: dict[tuple[int, str], set[str]] = {}
    for item in invocations:
        tool_name = str(item.get("tool_name") or "").strip()
        if not tool_name:
            continue
        try:
            invocation_index = int(item.get("invocation_index") or 0)
        except (TypeError, ValueError):
            invocation_index = 0
        invocation_states.setdefault((invocation_index, tool_name), set()).add(
            str(item.get("status") or "").strip().lower()
        )

    for (_invocation_index, tool_name), statuses in invocation_states.items():
        if operation_is_mutation(tool_name):
            if "failed" in statuses or "started" in statuses and "completed" not in statuses:
                uncertain_mutations.add(tool_name)
            elif "completed" in statuses:
                completed_mutations.add(tool_name)
        elif "completed" in statuses and is_provider_read_tool_name(tool_name):
            completed_reads.add(tool_name)

    for record in sdk_tool_execution_records(raw_result) if raw_result is not None else ():
        if operation_is_mutation(record.tool_name):
            if record.succeeded:
                completed_mutations.add(record.tool_name)
            else:
                uncertain_mutations.add(record.tool_name)
        elif record.succeeded and is_provider_read_tool_name(record.tool_name):
            completed_reads.add(record.tool_name)

    return _ToolRetryState(
        completed_read_tool_names=tuple(sorted(completed_reads)),
        completed_mutation_tool_names=tuple(sorted(completed_mutations)),
        uncertain_mutation_tool_names=tuple(sorted(uncertain_mutations)),
    )


def _disable_tools_by_name(
    agent: AgentLike,
    tool_names: Sequence[str],
    disabled_tools: dict[int, tuple[Any, Any]],
) -> tuple[str, ...]:
    names = {str(name).strip() for name in tool_names if str(name).strip()}
    disabled_names: list[str] = []
    for tool in list(getattr(agent, "tools", []) or []):
        tool_name = str(getattr(tool, "name", "") or "").strip()
        if tool_name not in names:
            continue
        tool_id = id(tool)
        if tool_id not in disabled_tools:
            disabled_tools[tool_id] = (tool, getattr(tool, "is_enabled", True))
        tool.is_enabled = False
        disabled_names.append(tool_name)
    return tuple(dict.fromkeys(disabled_names))


def _prepare_tools_for_retry(
    agent: AgentLike,
    *,
    raw_result: Any,
    invocations: Sequence[Mapping[str, Any]],
    receipts: Sequence[Mapping[str, Any]],
    disabled_tools: dict[int, tuple[Any, Any]],
    disable_completed_reads: bool,
    allow_durable_mutation_continuation: bool = False,
) -> tuple[_ToolRetryState, tuple[str, ...]]:
    """Disable completed operations and reject an uncertain mutation retry."""

    state = _tool_retry_state(raw_result, invocations)
    safe_mutation_names: set[str] = set()
    if allow_durable_mutation_continuation:
        from keystone_agents.runtime.durable_execution import current_execution

        execution = current_execution()
        if execution is not None:
            operations = execution.store.operations(execution.execution_id)
            completed_names = {
                operation["tool_name"] for operation in operations
                if operation["status"] in {"verified", "no_effect"}
            }
            unresolved_names = {
                operation["tool_name"] for operation in operations
                if operation["status"] not in {"verified", "no_effect"}
            }
            safe_mutation_names = completed_names - unresolved_names
    uncertain_names = set(state.uncertain_mutation_tool_names) - safe_mutation_names
    if uncertain_names:
        raise UnsafeMutationRetryError(tuple(sorted(uncertain_names)))
    if state.uncertain_mutation_tool_names:
        state = _ToolRetryState(
            completed_read_tool_names=state.completed_read_tool_names,
            completed_mutation_tool_names=state.completed_mutation_tool_names,
        )
    disabled_names = {
        *mutation_tool_names(receipts),
        *state.completed_mutation_tool_names,
    }
    # The journal replays verified operations, re-admits proven previews, and
    # gates distinct writes. Never re-enable an intentionally disabled tool.
    disabled_names.difference_update(safe_mutation_names)
    if disable_completed_reads:
        disabled_names.update(state.completed_read_tool_names)
    disabled = _disable_tools_by_name(agent, sorted(disabled_names), disabled_tools)
    return state, disabled


def _disable_decision_repair_tools(
    agent: AgentLike,
    disabled_tools: dict[int, tuple[Any, Any]],
) -> tuple[str, ...]:
    """Make an evidence-backed semantic repair strictly tool-free.

    The replay already contains the bounded evidence needed to repair the
    structured choice. Disabling every attached tool prevents new evidence,
    repeated reads, redundant deterministic scoring, and accidental mutation
    from widening what is intentionally a one-turn repair.
    """

    return _disable_tools_by_name(
        agent,
        [
            str(getattr(tool, "name", "") or "").strip()
            for tool in list(getattr(agent, "tools", []) or [])
        ],
        disabled_tools,
    )


def _disable_provider_read_tools(
    agent: AgentLike,
    disabled_tools: dict[int, tuple[Any, Any]],
    *,
    completed_tool_names: Sequence[str] = (),
) -> tuple[str, ...]:
    """Disable every admitted provider read while preserving deterministic helpers."""

    completed_names = set(completed_tool_names)
    disabled_names: list[str] = []
    for tool in list(getattr(agent, "tools", []) or []):
        tool_name = str(getattr(tool, "name", "") or "").strip()
        if not tool_name or (
            tool_name not in completed_names
            and not is_provider_read_tool_name(tool_name)
        ):
            continue
        tool_id = id(tool)
        if tool_id not in disabled_tools:
            disabled_tools[tool_id] = (tool, getattr(tool, "is_enabled", True))
        tool.is_enabled = False
        disabled_names.append(tool_name)
    return tuple(dict.fromkeys(disabled_names))


def _has_verified_preacquired_decision_context(
    contract: AgentDecisionContract,
    *,
    verified_candidate_fingerprints: Sequence[str],
) -> bool:
    """Return whether the supplied candidate universe is receipt-bound before repair."""

    candidate_ids = tuple(contract.pre_model_candidate_ids)
    if not candidate_ids:
        return False
    verified = set(verified_candidate_fingerprints)
    return all(identity_fingerprint(candidate_id) in verified for candidate_id in candidate_ids)


def _sdk_prompt_with_recovery_context(
    prompt: str | list[dict[str, Any]],
    recovery_context: str,
) -> str | list[dict[str, Any]]:
    """Append bounded retry evidence without dropping local file/image inputs."""

    if isinstance(prompt, str):
        return prompt + recovery_context
    return [
        *prompt,
        {
            "role": "user",
            "content": [{"type": "input_text", "text": recovery_context}],
        },
    ]


def _verified_replayed_read_tool_names(
    entries: Sequence[Mapping[str, Any]], invocations: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        str(entry["tool_name"]) for entry in _verified_replayed_read_entries(entries, invocations)
    ))


def _verified_replayed_read_entries(
    entries: Sequence[Mapping[str, Any]], invocations: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """Credit only exact completed read outputs actually restored to the model input."""
    starts: dict[tuple[str, int], str] = {}
    completed: set[tuple[str, str, str]] = set()
    for invocation in invocations:
        name = str(invocation.get("tool_name") or "")
        if operation_is_mutation(name) or not is_provider_read_tool_name(name):
            continue
        index = invocation.get("invocation_index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 1:
            continue
        key = (name, index)
        if invocation.get("status") == "started" and invocation.get("arguments_sha256"):
            starts[key] = str(invocation["arguments_sha256"])
        elif invocation.get("status") in {"completed", "reused_verified_read"}:
            if key in starts and invocation.get("output_sha256"):
                completed.add((name, starts[key], str(invocation["output_sha256"])))
    return tuple(
        entry for entry in entries
        if isinstance(entry, Mapping) and "arguments" in entry and "output" in entry
        and (
            str(entry.get("tool_name") or ""),
            tool_payload_fingerprint(entry["arguments"]),
            tool_payload_fingerprint(entry["output"]),
        ) in completed
    )


def _structured_output_retry_evidence_context(
    entries: Sequence[Mapping[str, Any]],
) -> str:
    """Replay bounded tool outputs after a malformed structured response.

    The caller owns sanitization and size bounds for its evidence provider. The
    shared runner keeps the payload model-visible but records only a fingerprint
    and count in telemetry.
    """

    payload = [dict(entry) for entry in entries]
    return (
        "\n\nThe previous model turn called tools successfully but did not return "
        "valid structured output. Those completed operations are disabled for this "
        "retry. Use the exact bounded evidence below to return only the required "
        "structured result; do not invent identities or claim additional provider "
        "reads.\nVerified model tool evidence:\n"
        + dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    )


def _sdk_rate_limit_max_retries(*, live: bool, run_config: Any | None) -> int:
    if not live or run_config is not None:
        return 0
    raw = os.getenv("KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES", "").strip()
    try:
        return max(0, min(3, int(raw) if raw else 1))
    except ValueError:
        return 1


def _sdk_structured_output_max_retries(
    *,
    live: bool,
    run_config: Any | None,
) -> int:
    if not live or run_config is not None:
        return 0
    raw = os.getenv("KEYSTONE_SDK_STRUCTURED_OUTPUT_MAX_RETRIES", "").strip()
    try:
        return max(0, min(1, int(raw) if raw else 1))
    except ValueError:
        return 1


def _is_sdk_structured_output_error(exc: BaseException) -> bool:
    if response_terminal_diagnostics(exc):
        return False
    error_type = type(exc).__name__.lower()
    text = f"{error_type} {exc}".lower()
    return bool(
        error_type in {"modelbehaviorerror", "validationerror"}
        or (
            "structured" in text
            and any(marker in text for marker in ("json", "output", "schema", "valid"))
        )
    )


def _is_sdk_rate_limit_error(exc: BaseException) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True
    if response_terminal_diagnostics(exc):
        return False
    text = f"{type(exc).__name__} {exc}".lower()
    return "rate limit" in text or "429" in text or "rate_limit_exceeded" in text


def _sdk_rate_limit_retry_delay_seconds(exc: BaseException, retry_count: int) -> float:
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is None:
        headers = getattr(exc, "headers", None)
        if isinstance(headers, Mapping):
            retry_after = headers.get("retry-after") or headers.get("Retry-After")
    try:
        delay = float(retry_after) if retry_after is not None else 0.0
    except (TypeError, ValueError):
        delay = 0.0
    if delay <= 0:
        match = re.search(r"try again in\s+([0-9]+(?:\.[0-9]+)?)s", str(exc), flags=re.I)
        if match:
            delay = float(match.group(1))
    if delay <= 0:
        delay = min(30.0, 4.0 * retry_count)
    return min(30.0, max(1.0, delay + 0.5))


def _attach_retrieval_diagnostics(
    output: TOutput, telemetry_packets: list[dict[str, Any]]
) -> TOutput:
    diagnostics = sdk_search_diagnostics_from_telemetry(telemetry_packets)
    if not diagnostics:
        return output
    model_fields = getattr(type(output), "model_fields", {})
    if isinstance(model_fields, dict) and "retrieval_diagnostics" in model_fields:
        model_copy = getattr(output, "model_copy", None)
        if callable(model_copy):
            return model_copy(update={"retrieval_diagnostics": diagnostics})
    if isinstance(output, dict):
        return {**output, "retrieval_diagnostics": diagnostics}  # type: ignore[return-value]
    return output


def sdk_synthesis_trace_metadata(
    *,
    agent_name: str,
    live: bool,
    save: bool,
    workflow: str | None = None,
    extra: TraceMetadata | None = None,
) -> dict[str, Any]:
    """Build trace metadata for live SDK synthesis without carrying prompt content."""

    metadata: dict[str, Any] = {
        "agent_name": agent_name,
        "run_kind": "sdk_synthesis",
        "live": bool(live),
        "save_requested": bool(save),
    }
    if workflow:
        metadata["workflow"] = workflow
    if extra:
        metadata.update(dict(extra))
    return dict(TraceConfig(trace_metadata=metadata).trace_metadata)


def _validate_synthesis_output(output: Any, output_type: type[TOutput]) -> TOutput:
    model_validate = getattr(output_type, "model_validate", None)
    if callable(model_validate):
        return model_validate(output)
    if isinstance(output, output_type):
        return output
    return output_type(output)


def _audit_payload(
    *,
    input_summary: str,
    input_audit_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if input_audit_payload is None:
        return {"input_summary": input_summary}
    return dict(input_audit_payload)


def _usage_attr(value: Any, name: str, default: int = 0) -> int:
    raw = value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return default


def _extract_token_detail(value: Any, field_name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, Mapping):
        raw = value.get(field_name, 0)
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0
    return _usage_attr(value, field_name)


_REQUEST_TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
    "cache_write_input_tokens", "reasoning_output_tokens",
)


def _sdk_request_usage_entries(raw_result: Any, usage: Any) -> list[dict[str, Any]]:
    observations = sdk_numeric_usage_observations(raw_result).get("responses") or []
    if observations:
        return [{key: row.get(key) for key in _REQUEST_TOKEN_FIELDS} for row in observations]
    responses = _first_attr(raw_result, "raw_responses") or []
    if responses:
        rows = [numeric_sdk_request_usage(
            _first_attr(response, "usage"), raw_usage=_first_attr(response, "raw_usage"),
        ) for response in responses]
    else:
        entries = _first_attr(usage, "request_usage_entries") or []
        if entries:
            rows = [numeric_sdk_request_usage(entry) for entry in entries]
        elif _first_usage_int(usage, ("requests", "num_model_requests"), default=1) == 1:
            rows = [numeric_sdk_request_usage(
                usage, raw_usage=usage if isinstance(usage, Mapping) else None,
            )]
        else:
            rows = []
    return [{key: row.get(key) for key in _REQUEST_TOKEN_FIELDS} for row in rows]


def extract_sdk_usage(raw_result: Any) -> dict[str, Any]:
    """Return safe request/token usage metadata when the SDK exposes it."""

    prompt_cache_metadata = _prompt_cache_metadata(raw_result)
    usage = getattr(raw_result, "usage", None)
    if usage is None and isinstance(raw_result, Mapping):
        usage = raw_result.get("usage")
    context_wrapper = getattr(raw_result, "context_wrapper", None)
    if usage is None and context_wrapper is not None:
        usage = getattr(context_wrapper, "usage", None)
    if usage is None:
        return {
            "available": False,
            "requests": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cached_input_tokens": None,
            "cache_write_input_tokens": None,
            "request_usage_entries": [],
            "reasoning_output_tokens": None,
            "cache_hit_rate": None,
            **prompt_cache_metadata,
        }

    input_details = _first_attr(
        usage,
        "input_tokens_details",
        "prompt_tokens_details",
    )
    output_details = _first_attr(
        usage,
        "output_tokens_details",
        "completion_tokens_details",
    )
    input_tokens = _first_usage_int(
        usage,
        ("input_tokens", "prompt_tokens", "prompt_token_count", "promptTokenCount"),
    )
    cached_input_tokens = max(
        _extract_token_detail(input_details, "cached_tokens"),
        _first_usage_int(
            usage,
            (
                "input_cached_tokens",
                "cached_input_tokens",
                "cached_content_token_count",
                "cachedContentTokenCount",
            ),
        ),
    )
    entries = _sdk_request_usage_entries(raw_result, usage)
    cache_write_counts = [row.get("cache_write_input_tokens") for row in entries]
    cache_write_tokens = (
        sum(cache_write_counts)
        if entries and all(type(value) is int for value in cache_write_counts) else None
    )
    return {
        "available": True,
        "requests": _first_usage_int(usage, ("requests", "num_model_requests"), default=1),
        "input_tokens": input_tokens,
        "output_tokens": _first_usage_int(
            usage,
            (
                "output_tokens",
                "completion_tokens",
                "candidates_token_count",
                "candidatesTokenCount",
            ),
        ),
        "total_tokens": _first_usage_int(
            usage,
            ("total_tokens", "total_token_count", "totalTokenCount"),
        ),
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": cache_write_tokens,
        "request_usage_entries": entries,
        "reasoning_output_tokens": max(
            _extract_token_detail(output_details, "reasoning_tokens"),
            _first_usage_int(usage, ("thoughts_token_count", "thoughtsTokenCount")),
        ),
        "cache_hit_rate": _cache_hit_rate(input_tokens, cached_input_tokens),
        **prompt_cache_metadata,
    }


_extract_sdk_usage = extract_sdk_usage


def _failed_sdk_attempt_usage(exc: BaseException) -> dict[str, Any]:
    """Project invocation-local numeric callbacks without reopening SDK model data."""

    snapshot = sdk_numeric_usage_observations(exc)
    responses = snapshot.get("responses") or []
    if snapshot and snapshot.get("requests_started") == 0 and not responses:
        return {
            "available": True,
            "complete": True,
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "request_usage_entries": [],
            "reasoning_output_tokens": 0,
            "cache_hit_rate": 0.0,
            "prompt_cache_key_present": False,
            "prompt_cache_key_hash": "",
            "note": "No SDK model request was admitted during this invocation.",
        }
    usages = [
        {**response, "available": True}
        for response in responses
        if all(type(response.get(field)) is int for field in (
            "requests", "input_tokens", "output_tokens", "total_tokens",
        ))
    ]
    usage = _aggregate_sdk_attempt_usage([{"usage": item} for item in usages])
    usage.pop("attempt_count", None)
    usage["complete"] = bool(
        responses
        and len(usages) == len(responses) == snapshot.get("requests_started")
    )
    if not usages:
        usage["requests"] = len(responses) if responses else None
    usage["note"] = (
        "Numeric usage captured before SDK output validation; no model content retained."
        if usages
        else "The failed SDK invocation did not expose numeric token usage."
    )
    return usage


def _aggregate_sdk_attempt_usage(
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate completed outer model attempts without hiding missing usage."""

    usages = [
        dict(event.get("usage") or {})
        for event in events
        if isinstance(event.get("usage"), Mapping)
    ]
    if not usages:
        return {
            "available": False,
            "complete": False,
            "attempt_count": len(events),
            "requests": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cached_input_tokens": None,
            "cache_write_input_tokens": None,
            "request_usage_entries": [],
            "reasoning_output_tokens": None,
            "cache_hit_rate": None,
            "prompt_cache_key_present": False,
            "prompt_cache_key_hash": "",
        }
    available = [usage for usage in usages if usage.get("available") is True]
    complete = len(available) == len(events) and all(
        usage.get("complete") is not False for usage in available
    )

    def total(field: str) -> int | None:
        values = [usage.get(field) for usage in available]
        if not values or any(value is None for value in values):
            return None
        return sum(_usage_attr({field: value}, field) for value in values)

    input_tokens = total("input_tokens")
    cached_input_tokens = total("cached_input_tokens")
    request_entries = []
    for usage in available:
        entries = usage.get("request_usage_entries")
        if isinstance(entries, list) and entries:
            request_entries.extend(
                {key: entry.get(key) for key in _REQUEST_TOKEN_FIELDS}
                for entry in entries if isinstance(entry, Mapping)
            )
        elif usage.get("requests") == 1:
            request_entries.append({key: usage.get(key) for key in _REQUEST_TOKEN_FIELDS})
    final_usage = usages[-1]
    return {
        "available": bool(available),
        "complete": complete,
        "attempt_count": len(events),
        "requests": total("requests"),
        "input_tokens": input_tokens,
        "output_tokens": total("output_tokens"),
        "total_tokens": total("total_tokens"),
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": total("cache_write_input_tokens"),
        "request_usage_entries": request_entries,
        "reasoning_output_tokens": total("reasoning_output_tokens"),
        "cache_hit_rate": _cache_hit_rate(input_tokens, cached_input_tokens),
        "prompt_cache_key_present": any(
            usage.get("prompt_cache_key_present") is True for usage in usages
        ),
        "prompt_cache_key_hash": str(
            final_usage.get("prompt_cache_key_hash") or ""
        ),
        **(
            {
                "prompt_cache_key_source": str(
                    final_usage.get("prompt_cache_key_source") or ""
                )
            }
            if final_usage.get("prompt_cache_key_source")
            else {}
        ),
    }


def _cache_hit_rate(input_tokens: int | None, cached_input_tokens: int | None) -> float | None:
    try:
        total = int(input_tokens or 0)
        cached = int(cached_input_tokens or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return 0.0
    return round(min(total, max(0, cached)) / total, 4)


def _prompt_cache_metadata(raw_result: Any) -> dict[str, Any]:
    key = getattr(raw_result, "_generated_prompt_cache_key", None)
    if not key and isinstance(raw_result, Mapping):
        key = raw_result.get("generated_prompt_cache_key")
    if not key:
        state = getattr(raw_result, "state", None)
        key = getattr(state, "_generated_prompt_cache_key", None)
    if not key:
        return {
            "prompt_cache_key_present": False,
            "prompt_cache_key_hash": "",
        }
    digest = sha256(str(key).encode("utf-8")).hexdigest()[:12]
    return {
        "prompt_cache_key_present": True,
        "prompt_cache_key_hash": digest,
        "prompt_cache_key_source": "agents_sdk_generated",
    }


def _usage_with_explicit_prompt_cache_metadata(
    usage: dict[str, Any],
    *,
    request_cache: Mapping[str, Any],
) -> dict[str, Any]:
    if not request_cache.get("prompt_cache_key_present") or usage.get("prompt_cache_key_present"):
        return usage
    return {
        **usage,
        "prompt_cache_key_present": True,
        "prompt_cache_key_hash": str(request_cache.get("prompt_cache_key_hash") or ""),
        "prompt_cache_key_source": str(
            request_cache.get("prompt_cache_key_source") or "explicit_static_profile"
        ),
    }


def _sdk_request_cache_metadata(
    *,
    agent: AgentLike,
    prompt: str,
    session: Any | None,
    max_turns: int | None,
) -> dict[str, Any]:
    """Return audit-safe fingerprints for cache-sensitive SDK request layout."""

    instruction_projection = instruction_profile_text(agent)
    instructions = instruction_projection or ""
    instruction_status = (
        "static_base_and_current_catalog"
        if instruction_projection is not None
        else "unresolved_dynamic_callable"
    )
    tool_names = _ordered_tool_names(getattr(agent, "tools", []) or [])
    output_schema = _output_schema_payload(getattr(agent, "output_type", None))
    static_payload = {
        "instructions_sha256": _sha256(instructions) if instruction_projection is not None else "",
        "instruction_profile_status": instruction_status,
        "tool_names": tool_names,
        "output_schema_sha256": _sha256_dumps(output_schema),
    }
    session_metadata = _session_audit_metadata(session)
    return {
        "request_layout": "static_agent_prefix_then_dynamic_typed_input",
        "repo_instruction_profile": repo_instruction_profile_id(),
        "static_prefix_sha256": _sha256_dumps(static_payload),
        "instructions_sha256": static_payload["instructions_sha256"],
        "instruction_profile_status": instruction_status,
        "tool_names_sha256": _sha256_dumps(tool_names),
        "tool_count": len(tool_names),
        "output_schema_sha256": static_payload["output_schema_sha256"],
        "dynamic_prompt_sha256": _sha256(prompt),
        "dynamic_prompt_chars": len(prompt),
        "max_turns": max_turns,
        "max_turns_source": "caller" if max_turns is not None else "sdk_default",
        "session_attached": session is not None,
        **prompt_cache_key_audit_metadata(agent),
        **session_metadata,
        "note": (
            "Instruction fingerprints cover the static base and current catalog, excluding "
            "dynamic replay and callback enablement. Unknown callables are unresolved. Raw "
            "instructions, tool schemas, session ids, and prompt text are not stored here."
        ),
    }


def _record_sdk_run_summary_safely(
    *,
    agent_name: str,
    model_provider: str,
    model_name: str,
    model_run_mode: str,
    live: bool,
    request_cache: dict[str, Any],
    usage: dict[str, Any],
    cost: dict[str, Any],
    budget_guard: dict[str, Any],
    search_telemetry: list[dict[str, Any]],
    raw_result: Any,
    trace_metadata: TraceMetadata | None,
    status: str,
    failure_kind: str = "",
    retry_count: int = 0,
    duration_ms: float | None = None,
    search_diagnostics: dict[str, Any] | None = None,
    execution_telemetry: Mapping[str, Any] | None = None,
) -> None:
    try:
        from keystone_agents.trace_processor import (
            record_sdk_run_summary_trace_event,
            sdk_tool_custom_trace_evidence,
        )

        diagnostics = search_diagnostics or sdk_search_diagnostics_from_telemetry(search_telemetry)
        metadata = dict(trace_metadata or {})
        custom_evidence = sdk_tool_custom_trace_evidence(raw_result)
        for key in (
            "nested_specialist_executions",
            "nested_live_read_enforcements",
        ):
            values = custom_evidence.get(key) or []
            if values:
                request_cache[key] = list(values)
        record_sdk_run_summary_trace_event(
            agent_name=agent_name,
            route=str(metadata.get("route") or ""),
            stage=str(metadata.get("stage") or "sdk_agent_run"),
            live=live,
            run_mode=model_run_mode,
            model_provider=model_provider,
            model_name=model_name,
            request_cache=request_cache,
            usage=usage,
            cost=cost,
            budget_guard=budget_guard,
            search_diagnostics=diagnostics,
            orchestrator_diagnostics=_orchestrator_diagnostics_from_trace_metadata(metadata),
            raw_result=raw_result,
            trace_metadata=metadata,
            status=status,
            failure_kind=failure_kind,
            retry_count=retry_count,
            duration_ms=duration_ms,
            execution_telemetry=dict(execution_telemetry or {}),
        )
    except Exception:
        return None


def _orchestrator_diagnostics_from_trace_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Extract chartable orchestrator diagnostics from already-sanitized trace metadata."""

    return {
        "has_preflight": bool(metadata.get("orchestrator_has_preflight")),
        "has_review": bool(metadata.get("orchestrator_has_review")),
        "selected_route": str(
            metadata.get("orchestrator_selected_route") or metadata.get("route") or ""
        ).strip(),
        "route_confidence": metadata.get("orchestrator_route_confidence"),
        "feedback_count": metadata.get("orchestrator_feedback_count"),
        "blocker_count": metadata.get("orchestrator_blocker_count"),
        "review_status": str(metadata.get("orchestrator_review_status") or "").strip(),
    }


def _failure_kind(exc: BaseException) -> str:
    terminal_kind = response_terminal_failure_kind(exc)
    if terminal_kind:
        return terminal_kind
    if type(exc).__name__ == "ExecutionDeadlineExceeded":
        return "execution_soft_deadline_exceeded"
    if isinstance(exc, ToolEvidenceReplayError):
        return "tool_correction_evidence_replay_unavailable"
    if isinstance(exc, UnsafeMutationRetryError):
        return "mutation_retry_state_unknown"
    if isinstance(exc, ToolExecutionContractError):
        return (
            "forbidden_tool_execution_observed"
            if exc.outcome.prohibited_tool_names
            else "required_tool_execution_missing"
        )
    if _is_sdk_structured_output_error(exc):
        return "structured_output_invalid"
    return re.sub(r"[^a-z0-9_]+", "_", type(exc).__name__.strip().lower()).strip("_")


def _sdk_guardrail_failure_diagnostics(exc: BaseException) -> dict[str, list[str]]:
    """Extract bounded, audit-safe reasons from an SDK guardrail exception."""

    guardrail_result = getattr(exc, "guardrail_result", None)
    output = getattr(guardrail_result, "output", None)
    output_info = getattr(output, "output_info", None)
    if not isinstance(output_info, Mapping):
        return {}

    diagnostics: dict[str, list[str]] = {}
    for key, max_length in (("risk_flags", 80), ("reasons", 240)):
        raw_values = output_info.get(key)
        if not isinstance(raw_values, Sequence) or isinstance(
            raw_values,
            str | bytes | bytearray,
        ):
            continue
        values = [
            re.sub(r"\s+", " ", str(value)).strip()[:max_length]
            for value in raw_values[:12]
            if str(value).strip()
        ]
        if values:
            diagnostics[key] = list(dict.fromkeys(values))
    return diagnostics


def _attach_sdk_run_failure_metadata(
    exc: BaseException,
    metadata: Mapping[str, Any],
) -> None:
    """Attach audit-safe failed-attempt evidence without changing exception types."""

    try:
        exc.keystone_sdk_run_failure = dict(metadata)  # type: ignore[attr-defined]
    except Exception:
        return None


def sdk_run_failure_metadata(exc: BaseException) -> dict[str, Any]:
    """Return audit-safe failed-attempt evidence attached by the shared runner."""

    metadata = getattr(exc, "keystone_sdk_run_failure", None)
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def _session_audit_metadata(session: Any | None) -> dict[str, Any]:
    if session is None:
        return {
            "session_scope": "",
            "session_source": "",
            "session_id_hash": "",
            "session_history_mode": "",
            "session_history_limit": 0,
            "session_truncation_configured": False,
        }
    metadata = session_audit_metadata(session)
    return {
        "session_scope": str(metadata.get("scope") or ""),
        "session_source": str(metadata.get("source") or ""),
        "session_id_hash": str(metadata.get("session_id_hash") or ""),
        "session_history_mode": str(metadata.get("session_history_mode") or ""),
        "session_history_limit": int(metadata.get("session_history_limit") or 0),
        "session_truncation_configured": bool(metadata.get("session_truncation_configured")),
    }


def _ordered_tool_names(tools: Sequence[Any]) -> list[str]:
    return [
        str(getattr(tool, "name", getattr(tool, "__name__", type(tool).__name__)) or "")
        for tool in tools
    ]


def _output_schema_payload(output_type: Any) -> Any:
    if hasattr(output_type, "model_json_schema"):
        try:
            return output_type.model_json_schema()
        except Exception:
            return str(output_type)
    return str(output_type or "")


def _sha256(value: str) -> str:
    return sha256(str(value or "").encode("utf-8")).hexdigest()


def _sha256_dumps(value: Any) -> str:
    return sha256(
        dumps(value, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _first_attr(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        item = getattr(value, name, None)
        if item is not None:
            return item
    return None


def _first_usage_int(
    usage: Any,
    names: tuple[str, ...],
    *,
    default: int = 0,
) -> int:
    for name in names:
        if isinstance(usage, Mapping):
            raw = usage.get(name)
        else:
            raw = getattr(usage, name, None)
        if raw is None:
            continue
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            continue
    return default


def _sdk_audit_output(
    *,
    output: Any,
    model_provider: str,
    model_name: str,
    model_run_mode: str,
    usage: dict[str, Any],
    cost: dict[str, Any],
    budget_guard: dict[str, Any],
    request_cache: dict[str, Any],
    execution_telemetry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "result": output,
        "_sdk_model": {
            "provider": model_provider,
            "name": model_name,
            "run_mode": model_run_mode,
        },
        "_sdk_usage": usage,
        "_sdk_cost": cost,
        "_sdk_budget_guard": budget_guard,
        "_sdk_request_cache": request_cache,
    }
    telemetry = compact_execution_telemetry(execution_telemetry)
    if telemetry:
        payload["_execution_telemetry"] = telemetry
    return payload


def _today_et() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def _observed_daily_gemini_requests(
    storage: Any,
    *,
    model_provider: str,
    model_name: str,
) -> int | None:
    if model_provider != GEMINI_PROVIDER:
        return None
    try:
        if hasattr(storage, "list_records_by_created_date_et"):
            rows = storage.list_records_by_created_date_et("agent_runs", _today_et())
        elif hasattr(storage, "list_records"):
            rows = storage.list_records("agent_runs")
        else:
            return None
    except Exception:
        return None

    total = 0
    matched = False
    for row in rows:
        output_json = row.get("output_json") if isinstance(row, Mapping) else None
        if not output_json:
            continue
        try:
            output_payload = loads(str(output_json))
        except (JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(output_payload, Mapping):
            continue
        sdk_model = output_payload.get("_sdk_model")
        sdk_usage = output_payload.get("_sdk_usage")
        if not isinstance(sdk_model, Mapping) or not isinstance(sdk_usage, Mapping):
            continue
        if sdk_model.get("provider") != GEMINI_PROVIDER:
            continue
        if sdk_model.get("name") != model_name:
            continue
        matched = True
        total += _usage_attr(sdk_usage, "requests", default=0)
    return total if matched else None


def run_retrieved_sdk_synthesis(
    *,
    agent: AgentLike,
    output_type: type[TOutput],
    retrieve: Callable[[], TRaw],
    normalize: Callable[[TRaw], Any],
    finalize_output: Callable[[TRaw, TOutput], Any] | None = None,
    input_summary: str,
    input_audit_payload: Mapping[str, Any] | None = None,
    run_config: Any | None = None,
    live: bool = False,
    config: ModelConfig | None = None,
    session: Any | None = None,
    workflow_name: str | None = None,
    group_id: str | None = None,
    trace_metadata: TraceMetadata | None = None,
    tracing_disabled: bool | None = None,
    trace_include_sensitive_data: bool | None = None,
    trace_config: TraceConfig | None = None,
    save: bool = False,
    storage: Any | None = None,
    persist_output: Callable[[TOutput], Mapping[str, Any] | None] | None = None,
    model_label: str | None = None,
    decision_contract: (
        AgentDecisionContract | Callable[[TRaw, Any], AgentDecisionContract] | None
    ) = None,
) -> SDKSynthesisOutcome:
    """
    Retrieve and normalize context in Python, synthesize with an SDK agent, then audit.

    The helper keeps live model execution behind the existing SDK credential gate. It
    never invokes outbound integrations itself; callers may only pass local storage
    persistence for explicitly requested `save` runs.
    """

    if save and storage is None:
        raise RuntimeError("storage is required when save=True for SDK synthesis.")
    if trace_include_sensitive_data is True or (
        trace_config is not None and trace_config.trace_include_sensitive_data
    ):
        raise RuntimeError("SDK synthesis traces must not include sensitive data.")

    if run_config is not None:
        if config is not None:
            resolved_model_provider = config.provider
            resolved_model_name = config.model
        else:
            resolved_model_provider = "local"
            resolved_model_name = str(
                getattr(run_config, "model", "") or model_label or "sdk-local"
            )
        resolved_model_run_mode = "live_sdk" if live else "local_sdk"
    else:
        resolved_model_config = config or get_runtime_agent_model_config(
            getattr(agent, "name", None),
            model_override=getattr(agent, "model", None),
        )
        resolved_model_provider = resolved_model_config.provider
        resolved_model_name = resolved_model_config.model
        resolved_model_run_mode = "live_sdk" if live else "sdk"
    fallback_model_configs = (
        [
            fallback_config
            for fallback_config in (
                get_openai_fallback_model_config(
                    getattr(agent, "name", None),
                    model_override=None,
                ),
                get_gemini_fallback_model_config(
                    getattr(agent, "name", None),
                    model_override=None,
                ),
            )
            if fallback_config is not None
        ]
        if live and run_config is None and config is None
        else []
    )

    workflow = workflow_name or f"Keystone {agent.name} SDK synthesis"
    # Imported lazily to keep the generic execution harness independent from
    # agent builders while still making every supplied-context synthesis run
    # emit the same searchable scope evidence as direct agent wrappers.
    from keystone_agents.capabilities.tool_scope import (
        tool_scope_receipt_for_agent,
        tool_scope_trace_metadata_for_agent,
    )

    scope_receipt = tool_scope_receipt_for_agent(agent)
    scope_trace = tool_scope_trace_metadata_for_agent(agent)
    resolved_trace_metadata = sdk_synthesis_trace_metadata(
        agent_name=agent.name,
        live=live and run_config is None,
        save=save,
        workflow=workflow,
        extra={**dict(trace_metadata or {}), **scope_trace},
    )
    # Retrieval performed outside the model loop is still part of this run's
    # evidence boundary. Isolate its receipts so model-selected identities can
    # be validated against the exact provider results rather than against stale
    # process-global journal state.
    reset_tool_receipt_journal()
    raw_context = retrieve()
    preacquired_tool_receipts = tuple(tool_receipt_journal())
    typed_input = normalize(raw_context)
    resolved_decision_contract = (
        decision_contract(raw_context, typed_input)
        if callable(decision_contract)
        else decision_contract
    )
    audit_payload = _audit_payload(
        input_summary=input_summary,
        input_audit_payload=input_audit_payload,
    )
    storage_results: dict[str, Any] = {}
    started_at = time.time()
    audit_notes = [
        "Retrieved and normalized context in Python before SDK execution.",
        "Validated structured output before optional persistence.",
        "No outbound side effects were invoked by the synthesis harness.",
    ]

    try:
        live_attempts: list[tuple[str, ModelConfig | None]] = [(resolved_model_provider, config)]
        for fallback_model_config in fallback_model_configs:
            live_attempts.append((fallback_model_config.provider, fallback_model_config))

        last_exc: Exception | None = None
        for attempt_index, (attempt_provider, attempt_config) in enumerate(live_attempts):
            try:
                typed_result = run_typed_sdk_agent(
                    agent=agent,
                    typed_input=typed_input,
                    output_type=output_type,
                    run_config=run_config,
                    live=live,
                    config=attempt_config,
                    session=session,
                    workflow_name=workflow,
                    group_id=group_id,
                    trace_metadata=resolved_trace_metadata,
                    tracing_disabled=tracing_disabled,
                    trace_include_sensitive_data=trace_include_sensitive_data,
                    trace_config=trace_config,
                    preacquired_tool_receipts=preacquired_tool_receipts,
                    decision_contract=resolved_decision_contract,
                )
                if attempt_config is not None:
                    resolved_model_provider = attempt_config.provider
                    resolved_model_name = attempt_config.model
                    resolved_model_run_mode = "live_sdk" if live else "sdk"
                break
            except Exception as exc:
                last_exc = exc
                if isinstance(
                    exc,
                    AgentRunBudgetExceededError | ModelProviderConfigurationError,
                ):
                    raise
                if isinstance(exc, MissingOpenAIAPIKeyError) and not fallback_model_configs:
                    raise
                if attempt_index >= len(live_attempts) - 1:
                    raise
                fallback_provider = live_attempts[attempt_index + 1][0]
                fallback_label = {"openai": "OpenAI", "gemini": "Gemini"}.get(
                    fallback_provider,
                    fallback_provider,
                )
                audit_notes.append(
                    "Primary live SDK provider "
                    f"{attempt_provider} failed with {type(exc).__name__}; "
                    f"retried once with {fallback_label} fallback."
                )
        else:
            raise RuntimeError("SDK synthesis did not execute a model attempt.") from last_exc

        validated_output = _validate_synthesis_output(typed_result.output, output_type)
        finalized_output = (
            finalize_output(raw_context, validated_output)
            if finalize_output is not None
            else validated_output
        )
        request_cache = dict(typed_result.request_cache or {})
        if scope_receipt:
            request_cache["request_tool_scope"] = scope_receipt
        combined_tool_receipts = tuple(
            [
                *preacquired_tool_receipts,
                *tuple(typed_result.tool_receipts or ()),
            ]
        )
        typed_result = TypedAgentRunResult(
            agent_name=typed_result.agent_name,
            output=finalized_output,
            raw_result=typed_result.raw_result,
            live=typed_result.live,
            usage=typed_result.usage,
            cost=typed_result.cost,
            budget_guard=typed_result.budget_guard,
            request_cache=request_cache,
            execution_telemetry=typed_result.execution_telemetry,
            tool_receipts=combined_tool_receipts,
        )
        usage = typed_result.usage or _extract_sdk_usage(typed_result.raw_result)
        cost = typed_result.cost or estimate_usage_cost(
            provider=resolved_model_provider,
            model=resolved_model_name,
            usage=usage,
        )
        provider_usage_context = gemini_free_tier_usage_context(
            provider=resolved_model_provider,
            model=resolved_model_name,
            usage=usage,
        )

        if save:
            if persist_output is not None:
                persisted = persist_output(finalized_output)
                if persisted:
                    storage_results["artifact"] = dict(persisted)
            audit_output = _sdk_audit_output(
                output=finalized_output,
                model_provider=resolved_model_provider,
                model_name=resolved_model_name,
                model_run_mode=resolved_model_run_mode,
                usage=usage,
                cost=cost,
                budget_guard=typed_result.budget_guard,
                request_cache=typed_result.request_cache,
                execution_telemetry=typed_result.execution_telemetry,
            )
            storage_results["agent_run"] = storage.save_agent_run(
                agent_name=agent.name,
                input_payload=audit_payload,
                input_summary=input_summary,
                output=audit_output,
                model=model_label or ("sdk-live" if typed_result.live else "sdk-local"),
                dry_run=not typed_result.live,
                status="success",
            )
            observed_daily_requests = _observed_daily_gemini_requests(
                storage,
                model_provider=resolved_model_provider,
                model_name=resolved_model_name,
            )
            if observed_daily_requests is not None:
                provider_usage_context = gemini_free_tier_usage_context(
                    provider=resolved_model_provider,
                    model=resolved_model_name,
                    usage=usage,
                    observed_daily_requests=observed_daily_requests,
                    daily_usage_source="local_sqlite_agent_runs_current_et_day",
                )
    except Exception as exc:
        operator_failure = known_exception_to_operator_failure(
            exc,
            context=f"{agent.name} SDK run",
        )
        failure_metadata = sdk_run_failure_metadata(exc)
        if save and storage is not None:
            failure_output = {
                "failure": operator_failure.to_dict(),
                "summary": operator_failure.summary,
                "next_step": operator_failure.next_step,
                "send_enabled": False,
            }
            if failure_metadata:
                failure_output["sdk_run_failure"] = failure_metadata
            storage_results["agent_run"] = storage.save_agent_run(
                agent_name=agent.name,
                input_payload=audit_payload,
                input_summary=input_summary,
                output=failure_output,
                model=model_label or ("sdk-live" if live and run_config is None else "sdk-local"),
                dry_run=not (live and run_config is None),
                status="error",
                error=type(exc).__name__,
            )
        raise

    return SDKSynthesisOutcome(
        agent_name=agent.name,
        raw_context=raw_context,
        typed_input=typed_input,
        result=typed_result,
        storage=storage_results,
        audit_notes=tuple(audit_notes),
        model_provider=resolved_model_provider,
        model_name=resolved_model_name,
        model_run_mode=resolved_model_run_mode,
        usage=usage,
        cost=cost,
        budget_guard=typed_result.budget_guard,
        request_cache=typed_result.request_cache,
        execution_telemetry=typed_result.execution_telemetry,
        provider_usage_context=provider_usage_context,
        started_at_unix=started_at,
        ended_at_unix=time.time(),
    )
