"""Trace-safe evidence packets for backend agent decision evaluations.

This module is deliberately observational. It compiles evidence already emitted by
the SDK wrapper, fake-model harnesses, provider tools, and deterministic validators;
it does not run an agent, call a provider, repair an answer, or change execution.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from importlib import import_module
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from keystone_agents.agent_decision_policy import list_agent_decision_policies
from keystone_agents.agent_registry import list_agent_specs
from keystone_agents.receipts.normalization import identity_fingerprint
from keystone_agents.runtime.output_validation import sanitized_output_diagnostics
from keystone_agents.runtime.tool_execution import (
    ExternalWriteState,
    external_write_state_from_evidence,
)
from keystone_agents.usage_projection import (
    NUMERIC_TOKEN_USAGE_KEYS,
    nonnegative_usage_integer,
    project_request_usage_entries,
)

_PRIVATE_TEXT = re.compile(
    r"(?:[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|"
    r"\b(?:sk|rk|pk|xox[baprs]|AIza)[-_A-Za-z0-9]{12,}\b|"
    r"\b(?:msg|thread|rec|evt|event|file|doc|folder|item|coll)[-_A-Za-z0-9]{8,}\b|"
    r"\b[a-f0-9]{16,}\b)",
    re.IGNORECASE,
)
_SAFE_ARGUMENT_KEYS = frozenset(
    {
        "operation",
        "resource_type",
        "limit",
        "max_results",
        "max_messages",
        "read_only",
        "local_only",
        "send_enabled",
        "live",
        "dry_run",
    }
)
_SUCCESS_STATUSES = frozenset(
    {"accepted", "completed", "ok", "pass", "passed", "success", "succeeded", "verified"}
)


def stable_fingerprint(value: Any) -> str:
    """Return a deterministic SHA-256 digest without retaining the source value."""

    canonical = json.dumps(
        _jsonable(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ModelVisibleComponent:
    """One bounded component known to have been serialized into model input."""

    category: Literal[
        "typed_context_pack",
        "cross_provider_context",
        "preacquired_evidence",
        "verified_continuation_state",
        "mandatory_context",
        "other",
    ]
    value: Any
    source: str
    required_identity_values: tuple[str, ...] = ()
    verified: bool = False


@dataclass(frozen=True)
class ProviderAttemptEvidence:
    """Trace-safe provider-attempt metadata supplied by an existing caller."""

    provider: str
    operation: str
    status: str
    attempt: int = 1
    fallback_from: str = ""
    receipt_verified: bool = False
    latency_ms: float | None = None


@dataclass(frozen=True)
class HandoffEvidence:
    """Typed handoff and downstream-consumption proof."""

    source_agent: str
    downstream_agent: str
    evidence_values: tuple[str, ...] = ()
    provided: bool = True
    consumed: bool = False
    consumption_source: str = ""


@dataclass(frozen=True)
class DecisionAttemptEvidence:
    """One model decision plus its deterministic validator result."""

    attempt: int
    decision_owner: str
    decision_stage: str
    candidate_values: tuple[str, ...] = ()
    selected_values: tuple[str, ...] = ()
    assessments: tuple[tuple[str, str, str], ...] = ()
    reasoning: str = ""
    limitations: tuple[str, ...] = ()
    validator_status: str = "not_evaluated"
    validator_reason_code: str = ""
    repair_requested: bool = False


class InputComponentTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    source: str
    value_type: str
    fingerprint: str
    serialized_size: int = Field(ge=0)
    required_identity_fingerprints: list[str] = Field(default_factory=list)
    required_identities_visible: bool | None = None
    verified: bool = False


class ModelInputTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_index: int = Field(ge=1)
    fingerprint: str
    serialized_size: int = Field(ge=0)
    input_type: str


class AttachedToolTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    schema_fingerprint: str
    schema_source: str
    admission_tier: str = ""
    admission_rationale_fingerprint: str = ""
    forbidden: bool = False


class CandidateUniverseTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_fingerprint: str
    identity_kind: str
    source_tool: str
    call_order: int = Field(ge=1)
    candidate_ordinal: int = Field(ge=1)
    raw_output_fingerprint: str
    candidate_metadata_fingerprint: str


class ToolCallTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_order: int = Field(ge=1)
    name: str
    call_id_fingerprint: str
    argument_keys: list[str] = Field(default_factory=list)
    safe_arguments: dict[str, Any] = Field(default_factory=dict)
    arguments_fingerprint: str
    status: str
    output_observed: bool
    output_fingerprint: str = ""
    output_keys: list[str] = Field(default_factory=list)
    candidate_count: int = Field(default=0, ge=0)


class ReceiptTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str = ""
    provider: str = ""
    operation: str = ""
    status: str = ""
    verified: bool = False
    identity_fingerprints: list[str] = Field(default_factory=list)
    receipt_fingerprint: str


class ProviderAttemptTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    operation: str
    status: str
    attempt: int = Field(ge=1)
    fallback_from: str = ""
    receipt_verified: bool = False
    latency_ms: float | None = Field(default=None, ge=0)


class CandidateAssessmentTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_fingerprint: str
    disposition: str
    rationale: str = ""
    rationale_fingerprint: str = ""


class DecisionAttemptTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt: int = Field(ge=1)
    decision_owner: str
    decision_stage: str
    candidate_identity_fingerprints: list[str] = Field(default_factory=list)
    selected_identity_fingerprints: list[str] = Field(default_factory=list)
    assessments: list[CandidateAssessmentTrace] = Field(default_factory=list)
    reasoning: str = ""
    limitations: list[str] = Field(default_factory=list)
    reasoning_fingerprint: str = ""
    limitation_fingerprints: list[str] = Field(default_factory=list)
    validator_status: str
    validator_reason_code: str = ""
    repair_requested: bool = False


class HandoffTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_agent: str
    downstream_agent: str
    evidence_fingerprints: list[str] = Field(default_factory=list)
    provided: bool
    consumed: bool
    consumption_source: str = ""


class ConsumptionTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_run_count: int = Field(ge=0)
    model_request_count: int = Field(ge=0)
    model_request_count_source: str
    model_request_count_confirmed: bool
    usage: dict[str, Any] = Field(default_factory=dict)
    cost: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float | None = Field(default=None, ge=0)
    retries: int = Field(default=0, ge=0)


class BackendDecisionStageTrace(BaseModel):
    """One stage of a trace-safe backend scenario packet."""

    model_config = ConfigDict(extra="forbid")

    schema_name: str = "keystone.backend_decision_stage_trace.v1"
    stage_id: str
    agent: str
    stage: str
    provider: str
    model: str
    run_id: str
    trace_id: str
    trace_event_id: int | None = Field(default=None, ge=1)
    started_at: str
    ended_at: str
    request_cache: dict[str, Any] = Field(default_factory=dict)
    input_components: list[InputComponentTrace] = Field(default_factory=list)
    model_inputs: list[ModelInputTrace] = Field(default_factory=list)
    attached_tools: list[AttachedToolTrace] = Field(default_factory=list)
    tool_admission_fingerprint: str = ""
    forbidden_tools: list[str] = Field(default_factory=list)
    model_called_tools: list[ToolCallTrace] = Field(default_factory=list)
    workflow_called_tools: list[str] = Field(default_factory=list)
    workflow_called_helpers: list[str] = Field(default_factory=list)
    preacquired_context_tools: list[str] = Field(default_factory=list)
    intentional_tool_free_synthesis: bool = False
    preacquired_context_verified: bool = False
    provider_attempts: list[ProviderAttemptTrace] = Field(default_factory=list)
    provider_request_attempt_count: int | None = Field(default=None, ge=0)
    provider_request_success_count: int | None = Field(default=None, ge=0)
    provider_receipt_count: int | None = Field(default=None, ge=0)
    receipts: list[ReceiptTrace] = Field(default_factory=list)
    candidate_universe: list[CandidateUniverseTrace] = Field(default_factory=list)
    decision_attempts: list[DecisionAttemptTrace] = Field(default_factory=list)
    handoffs: list[HandoffTrace] = Field(default_factory=list)
    structured_output_obligations: list[str] = Field(default_factory=list)
    public_output_obligations: list[str] = Field(default_factory=list)
    consumption: ConsumptionTrace
    external_write_state: ExternalWriteState = "unknown"
    terminal_status: str
    failure_evidence: dict[str, Any] = Field(default_factory=dict)
    evaluation_status: Literal["pass", "fail", "partial"]
    findings: list[str] = Field(default_factory=list)
    unavailable_evidence: list[str] = Field(default_factory=list)
    raw_sensitive_bodies_retained: bool = False


class BackendDecisionScenarioTrace(BaseModel):
    """Complete multi-stage trace packet for one natural-language scenario."""

    model_config = ConfigDict(extra="forbid")

    schema_name: str = "keystone.backend_decision_scenario_trace.v1"
    scenario_id: str
    request_text: str = ""
    request_text_retained: bool
    request_fingerprint: str
    request_capture_note: str
    stages: list[BackendDecisionStageTrace]
    agent_run_count: int = Field(ge=0)
    model_request_count: int = Field(ge=0)
    evaluation_status: Literal["pass", "fail", "partial"]
    findings: list[str] = Field(default_factory=list)


class BackendScenarioPrediction(BaseModel):
    """Pre-registered expectations for one bounded backend scenario."""

    model_config = ConfigDict(extra="forbid")

    schema_name: str = "keystone.backend_scenario_prediction.v1"
    scenario_id: str
    route: str
    required_input_component_categories: list[str] = Field(default_factory=list)
    attached_tool_names: list[str] = Field(default_factory=list)
    attached_tool_match_mode: Literal["exact", "minimum"] = "exact"
    allow_additional_attached_tools: bool = False
    model_called_tool_names: list[str] = Field(default_factory=list)
    allowed_model_call_sequences: list[list[str]] = Field(default_factory=list)
    provider_attempt_sequence: list[ProviderAttemptTrace] = Field(default_factory=list)
    candidate_identity_fingerprints: list[str] = Field(default_factory=list)
    selected_identity_fingerprints: list[str] = Field(default_factory=list)
    excluded_identity_fingerprints: list[str] = Field(default_factory=list)
    decision_owner: str
    decision_stage: str
    validator_status: str
    repair_expected: bool = False
    max_decision_attempts: int | None = Field(default=None, ge=1)
    required_consumed_handoff_targets: list[str] = Field(default_factory=list)
    structured_output_obligations: list[str] = Field(default_factory=list)
    public_output_obligations: list[str] = Field(default_factory=list)
    external_write_state: ExternalWriteState | None = None
    max_model_requests: int | None = Field(default=None, ge=0)
    max_latency_ms: float | None = Field(default=None, ge=0)
    terminal_status: str


class BackendScenarioPredictionComparison(BaseModel):
    """Trace-safe comparison between pre-registered and observed execution."""

    model_config = ConfigDict(extra="forbid")

    schema_name: str = "keystone.backend_scenario_prediction_comparison.v1"
    scenario_id: str
    prediction_fingerprint: str
    actual_stage_id: str
    matched: bool
    mismatched_fields: list[str] = Field(default_factory=list)


class ProductionWrapperMatrixEntry(BaseModel):
    """Durable metadata for one real KBA agent wrapper family."""

    model_config = ConfigDict(extra="forbid")

    route: str
    agent_name: str
    builder: str
    production_wrapper: str
    wrapper_kind: Literal["direct_script", "work_item_runtime"]
    input_schema: str
    output_schema: str
    decision_stage: str
    expected_model_read_tools: list[str]
    verified_continuation_allowed: bool
    natural_language_probe: str

    def resolve_production_wrapper(self) -> Any:
        module_name, attribute = self.production_wrapper.split(":", maxsplit=1)
        return getattr(import_module(module_name), attribute)


def compare_backend_scenario_prediction(
    prediction: BackendScenarioPrediction,
    actual: BackendDecisionStageTrace,
) -> BackendScenarioPredictionComparison:
    """Compare one pre-registered trajectory with its sanitized trace packet."""

    actual_attached = [item.name for item in actual.attached_tools]
    expected_attached = list(dict.fromkeys(prediction.attached_tool_names))
    if (
        prediction.attached_tool_match_mode == "minimum"
        or prediction.allow_additional_attached_tools
    ):
        attached_match = set(expected_attached) <= set(actual_attached)
    else:
        attached_match = expected_attached == actual_attached
    actual_candidates = sorted(
        {item.identity_fingerprint for item in actual.candidate_universe}
    )
    expected_candidates = sorted(
        dict.fromkeys(prediction.candidate_identity_fingerprints)
    )
    final_decision = actual.decision_attempts[-1] if actual.decision_attempts else None
    actual_repair = len(actual.decision_attempts) > 1 or any(
        item.repair_requested for item in actual.decision_attempts
    )
    actual_call_sequence = [item.name for item in actual.model_called_tools]
    allowed_call_sequences = prediction.allowed_model_call_sequences or [
        prediction.model_called_tool_names
    ]
    actual_input_categories = {item.category for item in actual.input_components}
    expected_provider_attempts = [
        (
            item.provider,
            item.operation,
            item.status,
            item.attempt,
            item.fallback_from,
            item.receipt_verified,
        )
        for item in prediction.provider_attempt_sequence
    ]
    actual_provider_attempts = [
        (
            item.provider,
            item.operation,
            item.status,
            item.attempt,
            item.fallback_from,
            item.receipt_verified,
        )
        for item in actual.provider_attempts
    ]
    actual_selected = sorted(
        set(final_decision.selected_identity_fingerprints)
        if final_decision is not None
        else set()
    )
    actual_excluded = sorted(
        {
            item.identity_fingerprint
            for item in (final_decision.assessments if final_decision is not None else [])
            if item.disposition == "excluded"
        }
    )
    consumed_handoff_targets = {
        item.downstream_agent
        for item in actual.handoffs
        if item.provided and item.consumed
    }
    checks = {
        "route": prediction.route == actual.agent,
        "model_visible_context_categories": set(
            prediction.required_input_component_categories
        )
        <= actual_input_categories,
        "attached_tools": attached_match,
        "model_called_tools": actual_call_sequence in allowed_call_sequences,
        "provider_attempt_sequence": (
            not prediction.provider_attempt_sequence
            or expected_provider_attempts == actual_provider_attempts
        ),
        "candidate_universe": expected_candidates == actual_candidates,
        "selected_candidates": (
            not prediction.selected_identity_fingerprints
            or sorted(set(prediction.selected_identity_fingerprints)) == actual_selected
        ),
        "excluded_candidates": (
            not prediction.excluded_identity_fingerprints
            or sorted(set(prediction.excluded_identity_fingerprints)) == actual_excluded
        ),
        "decision_owner": bool(
            final_decision
            and prediction.decision_owner == final_decision.decision_owner
        ),
        "decision_stage": bool(
            final_decision
            and prediction.decision_stage == final_decision.decision_stage
        ),
        "validator_status": bool(
            final_decision
            and prediction.validator_status == final_decision.validator_status
        ),
        "repair": prediction.repair_expected == actual_repair,
        "decision_attempt_ceiling": (
            prediction.max_decision_attempts is None
            or len(actual.decision_attempts) <= prediction.max_decision_attempts
        ),
        "handoff_consumption": set(prediction.required_consumed_handoff_targets)
        <= consumed_handoff_targets,
        "structured_output_obligations": set(
            prediction.structured_output_obligations
        )
        <= set(actual.structured_output_obligations),
        "public_output_obligations": set(prediction.public_output_obligations)
        <= set(actual.public_output_obligations),
        "external_write_state": (
            prediction.external_write_state is None
            or prediction.external_write_state == actual.external_write_state
        ),
        "model_request_ceiling": (
            prediction.max_model_requests is None
            or actual.consumption.model_request_count <= prediction.max_model_requests
        ),
        "latency_ceiling": (
            prediction.max_latency_ms is None
            or actual.consumption.latency_ms is not None
            and actual.consumption.latency_ms <= prediction.max_latency_ms
        ),
        "terminal_status": prediction.terminal_status == actual.terminal_status,
    }
    mismatches = [field for field, matched in checks.items() if not matched]
    return BackendScenarioPredictionComparison(
        scenario_id=prediction.scenario_id,
        prediction_fingerprint=stable_fingerprint(
            prediction.model_dump(mode="json")
        ),
        actual_stage_id=actual.stage_id,
        matched=not mismatches,
        mismatched_fields=mismatches,
    )


_WRAPPER_BY_ROUTE = {
    "gmail_triage": (
        "scripts.run_gmail_triage:_run_agent_owned_live_gmail_synthesis",
        "direct_script",
    ),
    "business_research_analyst": (
        "scripts.run_company_research:_run_sdk_synthesis",
        "direct_script",
    ),
    "opportunity_scout": ("scripts.run_opportunity_scout:_run_sdk_synthesis", "direct_script"),
    "outreach_composer": ("scripts.run_outreach_draft:_run_sdk_synthesis", "direct_script"),
    "airtable_context_agent": (
        "keystone_agents.entrypoints.cli_impl:_run_ask_context_agent_live",
        "direct_script",
    ),
    "google_workspace_context_agent": (
        "keystone_agents.entrypoints.cli_impl:_run_ask_context_agent_live",
        "direct_script",
    ),
    "zotero_context_agent": (
        "keystone_agents.entrypoints.cli_impl:_run_ask_context_agent_live",
        "direct_script",
    ),
    "rss_context_agent": (
        "keystone_agents.workflow_runner:run_prepared_work_item_specialist",
        "work_item_runtime",
    ),
    "preprints_context_agent": (
        "keystone_agents.workflow_runner:run_prepared_work_item_specialist",
        "work_item_runtime",
    ),
    "rag_retrieval_specialist": (
        "keystone_agents.workflow_runner:run_prepared_work_item_specialist",
        "work_item_runtime",
    ),
    "orchestrator": ("scripts.run_orchestrator:main", "direct_script"),
    "chief_of_staff": ("scripts.run_chief_of_staff:main", "direct_script"),
}
_DECISION_STAGE_BY_ROUTE = {
    "gmail_triage": "gmail_candidate_selection",
    "business_research_analyst": "research_source_selection",
    "opportunity_scout": "opportunity_candidate_selection",
    "outreach_composer": "outreach_evidence_selection",
    "airtable_context_agent": "airtable_record_selection",
    "google_workspace_context_agent": "workspace_artifact_selection",
    "zotero_context_agent": "zotero_item_selection",
    "rss_context_agent": "signal_relevance_selection",
    "preprints_context_agent": "signal_relevance_selection",
    # RAG emits a typed retrieval result, not a standard decision record.
    "rag_retrieval_specialist": "rag_vector_store_retrieval",
    "orchestrator": "orchestrator_route_selection",
    "chief_of_staff": "chief_delegation_selection",
}
_NATURAL_PROBE_BY_ROUTE = {
    "gmail_triage": (
        "Which recent vendor thread actually needs my response, and what should I say here?"
    ),
    "business_research_analyst": (
        "What current evidence best explains Northstar Health's school-market traction?"
    ),
    "opportunity_scout": (
        "Which open partnership opportunity is the best fit for a small clinical-data company?"
    ),
    "outreach_composer": (
        "Using the approved context, write the most relevant short introduction for review."
    ),
    "airtable_context_agent": (
        "Which active partner record matches the organization discussed in yesterday's note?"
    ),
    "google_workspace_context_agent": (
        "Find the latest decision memo and tell me which version should guide the meeting."
    ),
    "zotero_context_agent": (
        "Which two collection items are most useful for the measurement discussion?"
    ),
    "rss_context_agent": "Which unreviewed feed signal is worth following up on this week?",
    "preprints_context_agent": (
        "Which recent preprint merits a closer evidence review for our clinical AI work?"
    ),
    "rag_retrieval_specialist": (
        "Use the RAG retrieval specialist to find corpus evidence about clinician trust."
    ),
    "orchestrator": (
        "I need a sourced brief and a review-only introduction; decide the safest sequence."
    ),
    "chief_of_staff": (
        "Across the blocked work, which safe next action should we take first and who owns it?"
    ),
}


def production_wrapper_matrix() -> tuple[ProductionWrapperMatrixEntry, ...]:
    """Return the registered-agent production-wrapper evaluation matrix."""

    specs = {spec.route_name: spec for spec in list_agent_specs()}
    policies = {policy.route: policy for policy in list_agent_decision_policies()}
    rows: list[ProductionWrapperMatrixEntry] = []
    for route, spec in specs.items():
        policy = policies[route]
        wrapper, wrapper_kind = _WRAPPER_BY_ROUTE[route]
        rows.append(
            ProductionWrapperMatrixEntry(
                route=route,
                agent_name=spec.agent_name,
                builder=spec.builder,
                production_wrapper=wrapper,
                wrapper_kind=wrapper_kind,
                input_schema=spec.input_contract_schema,
                output_schema=spec.output_schema,
                decision_stage=_DECISION_STAGE_BY_ROUTE[route],
                expected_model_read_tools=list(policy.expected_model_read_tools),
                verified_continuation_allowed=policy.verified_continuation_allowed,
                natural_language_probe=_NATURAL_PROBE_BY_ROUTE[route],
            )
        )
    return tuple(rows)


def build_backend_decision_stage_trace(
    *,
    stage_id: str,
    agent: str,
    stage: str,
    provider: str,
    model: str,
    run_id: str,
    trace_id: str,
    trace_event_id: int | None = None,
    model_visible_inputs: Sequence[Any],
    raw_results: Sequence[Any] = (),
    recorded_model_called_tools: Sequence[ToolCallTrace] = (),
    attached_tools: Sequence[Any] = (),
    input_components: Sequence[ModelVisibleComponent] = (),
    admission_by_tool: Mapping[str, Mapping[str, Any]] | None = None,
    tool_admission_fingerprint: str = "",
    forbidden_tools: Sequence[str] = (),
    candidate_identity_paths: Mapping[str, Sequence[str]] | None = None,
    preacquired_candidate_values: Sequence[str] = (),
    preacquired_candidate_source: str = "verified_preacquired_context",
    decision_attempts: Sequence[DecisionAttemptEvidence] = (),
    provider_attempts: Sequence[ProviderAttemptEvidence] = (),
    provider_request_attempt_count: int | None = None,
    provider_request_success_count: int | None = None,
    provider_receipt_count: int | None = None,
    receipts: Sequence[Mapping[str, Any]] = (),
    handoffs: Sequence[HandoffEvidence] = (),
    structured_output_obligations: Sequence[str] = (),
    public_output_obligations: Sequence[str] = (),
    workflow_called_tools: Sequence[str] = (),
    workflow_called_helpers: Sequence[str] = (),
    preacquired_context_tools: Sequence[str] = (),
    tool_invocations: Sequence[Mapping[str, Any]] = (),
    external_write_state: ExternalWriteState | None = None,
    preacquired_context_verified: bool = False,
    preacquired_context_trace_verified: bool = False,
    intentional_tool_free_synthesis: bool = False,
    provider_dependent: bool = False,
    usage: Mapping[str, Any] | None = None,
    cost: Mapping[str, Any] | None = None,
    latency_ms: float | None = None,
    retries: int = 0,
    agent_run_count: int = 1,
    terminal_status: str = "completed",
    failure_evidence: Mapping[str, Any] | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> BackendDecisionStageTrace:
    """Compile one stage from existing evidence without running any behavior."""

    started = started_at or datetime.now(UTC)
    ended = ended_at or started
    visible_serialized = [_serialize(value) for value in model_visible_inputs]
    combined_visible_text = "\n".join(visible_serialized)
    input_traces = [
        _input_component_trace(component, combined_visible_text) for component in input_components
    ]
    verified_preacquired_visible = bool(
        preacquired_context_trace_verified
        or preacquired_context_verified
        and any(
            component.verified
            and component.category
            in {
                "cross_provider_context",
                "preacquired_evidence",
                "verified_continuation_state",
                "mandatory_context",
            }
            and component.required_identities_visible is not False
            for component in input_traces
        )
    )
    model_input_traces = [
        ModelInputTrace(
            request_index=index,
            fingerprint=stable_fingerprint(value),
            serialized_size=len(_serialize(value).encode("utf-8")),
            input_type=type(value).__name__,
        )
        for index, value in enumerate(model_visible_inputs, start=1)
    ]
    tool_traces = _attached_tool_traces(
        attached_tools,
        admission_by_tool=admission_by_tool or {},
        forbidden_tools=forbidden_tools,
    )
    calls, universe = _tool_call_and_candidate_traces(
        raw_results,
        candidate_identity_paths=candidate_identity_paths or {},
    )
    if preacquired_candidate_values:
        source_fingerprint = stable_fingerprint(list(preacquired_candidate_values))
        universe.extend(
            CandidateUniverseTrace(
                identity_fingerprint=identity_fingerprint(value),
                identity_kind="preacquired_identity",
                source_tool=preacquired_candidate_source,
                call_order=1,
                candidate_ordinal=index,
                raw_output_fingerprint=source_fingerprint,
                candidate_metadata_fingerprint=stable_fingerprint(
                    {"source": preacquired_candidate_source, "ordinal": index}
                ),
            )
            for index, value in enumerate(preacquired_candidate_values, start=1)
            if str(value).strip()
        )
    if not calls and recorded_model_called_tools:
        calls = list(recorded_model_called_tools)
    receipt_traces = [_receipt_trace(receipt) for receipt in receipts]
    attempt_traces = [_decision_attempt_trace(item) for item in decision_attempts]
    handoff_traces = [_handoff_trace(item) for item in handoffs]

    usage_payload = _bounded_numeric_mapping(usage or {})
    cost_payload = _bounded_numeric_mapping(cost or {})
    observed_request_count = len(model_input_traces)
    usage_requests = _safe_nonnegative_int((usage or {}).get("requests"))
    if usage_requests is not None:
        model_request_count = usage_requests
        request_count_source = "sdk_usage"
        request_count_confirmed = bool(
            (usage or {}).get("available") is not False
            and (usage or {}).get("provider_request_count_confirmed", True)
        )
    else:
        model_request_count = observed_request_count
        request_count_source = "captured_model_inputs"
        request_count_confirmed = bool(model_input_traces)

    findings: list[str] = []
    unavailable: list[str] = []
    called_names = [call.name for call in calls]
    forbidden_called = sorted(set(called_names).intersection(forbidden_tools))
    if forbidden_called:
        findings.append("forbidden_model_tool_called:" + ",".join(forbidden_called))
    if provider_dependent and not called_names and not verified_preacquired_visible:
        findings.append(
            "provider_dependent_stage_has_no_model_tool_call_or_verified_preacquired_context"
        )
    if provider_dependent and model_request_count == 0 and not verified_preacquired_visible:
        findings.append("provider_dependent_stage_reached_terminal_state_with_zero_model_requests")
    universe_ids = {candidate.identity_fingerprint for candidate in universe}
    selected_ids = {
        selected
        for attempt in attempt_traces
        if attempt.validator_status == "accepted"
        for selected in attempt.selected_identity_fingerprints
    }
    if universe_ids and selected_ids - universe_ids:
        findings.append("agent_selection_not_bound_to_raw_provider_candidate_universe")
    if any(handoff.provided and not handoff.consumed for handoff in handoff_traces):
        findings.append("handoff_has_no_downstream_consumption_postcondition")
    if not usage_payload:
        unavailable.append("token_usage_not_recorded")
    if not cost_payload:
        unavailable.append("cost_evidence_not_recorded")
    if latency_ms is None:
        unavailable.append("stage_latency_not_recorded")
    if not run_id:
        unavailable.append("run_id_not_recorded")
    if not trace_id:
        unavailable.append("trace_id_not_recorded")
    if provider_attempts and not receipts:
        unavailable.append("provider_attempts_present_without_durable_receipts")
    if calls and not raw_results and not recorded_model_called_tools:
        unavailable.append("raw_sdk_result_not_available")
    if decision_attempts and not model_input_traces:
        unavailable.append("decision_present_without_captured_model_visible_input")
    resolved_external_write_state = external_write_state or external_write_state_from_evidence(
        receipts=receipts,
        tool_names=[
            *called_names,
            *workflow_called_tools,
            *workflow_called_helpers,
            *preacquired_context_tools,
        ],
        tool_invocations=tool_invocations,
        evidence_complete=bool(raw_results) or intentional_tool_free_synthesis,
    )
    if resolved_external_write_state == "unknown":
        unavailable.append("external_write_evidence_not_recorded")
    if str(terminal_status or "").strip().lower() in {
        "error",
        "failed",
        "failure",
    }:
        findings.append("terminal_stage_failed")

    evaluation = "fail" if findings else ("partial" if unavailable else "pass")
    return BackendDecisionStageTrace(
        stage_id=stage_id,
        agent=agent,
        stage=stage,
        provider=provider,
        model=model,
        run_id=run_id,
        trace_id=trace_id,
        trace_event_id=trace_event_id,
        started_at=started.isoformat(),
        ended_at=ended.isoformat(),
        input_components=input_traces,
        model_inputs=model_input_traces,
        attached_tools=tool_traces,
        tool_admission_fingerprint=tool_admission_fingerprint,
        forbidden_tools=sorted(set(forbidden_tools)),
        model_called_tools=calls,
        workflow_called_tools=list(dict.fromkeys(workflow_called_tools)),
        workflow_called_helpers=list(dict.fromkeys(workflow_called_helpers)),
        preacquired_context_tools=list(dict.fromkeys(preacquired_context_tools)),
        intentional_tool_free_synthesis=intentional_tool_free_synthesis,
        preacquired_context_verified=verified_preacquired_visible,
        provider_attempts=[ProviderAttemptTrace(**item.__dict__) for item in provider_attempts],
        provider_request_attempt_count=provider_request_attempt_count,
        provider_request_success_count=provider_request_success_count,
        provider_receipt_count=provider_receipt_count,
        receipts=receipt_traces,
        candidate_universe=universe,
        decision_attempts=attempt_traces,
        handoffs=handoff_traces,
        structured_output_obligations=list(
            dict.fromkeys(str(item) for item in structured_output_obligations if str(item))
        ),
        public_output_obligations=list(
            dict.fromkeys(str(item) for item in public_output_obligations if str(item))
        ),
        consumption=ConsumptionTrace(
            agent_run_count=agent_run_count,
            model_request_count=model_request_count,
            model_request_count_source=request_count_source,
            model_request_count_confirmed=request_count_confirmed,
            usage=usage_payload,
            cost=cost_payload,
            latency_ms=latency_ms,
            retries=retries,
        ),
        external_write_state=resolved_external_write_state,
        terminal_status=terminal_status,
        failure_evidence=dict(failure_evidence or {}),
        evaluation_status=evaluation,
        findings=findings,
        unavailable_evidence=unavailable,
    )


def build_backend_decision_stage_trace_from_runtime(
    *,
    stage_id: str,
    agent: str,
    stage: str,
    run_id: str = "",
    trace_id: str = "",
    provider: str = "",
    model: str = "",
    request_cache: Mapping[str, Any] | None = None,
    failure_metadata: Mapping[str, Any] | None = None,
    linked_output: Mapping[str, Any] | None = None,
    captured_model_inputs: Sequence[Any] = (),
    raw_results: Sequence[Any] = (),
    receipts: Sequence[Mapping[str, Any]] = (),
    provider_attempts: Sequence[ProviderAttemptEvidence] = (),
    terminal_status: str = "completed",
    latency_ms: float | None = None,
) -> BackendDecisionStageTrace:
    """Compile the production wrapper evidence that was actually recorded.

    The adapter deliberately reports missing model inputs, schemas, provider
    attempts, and decisions instead of reconstructing them from expected code.
    """

    output = dict(linked_output or {})
    nested_failure = _nested_mapping(output, "sdk_run_failure")
    metadata = {**nested_failure, **dict(failure_metadata or {})}
    cache: dict[str, Any] = {}
    for candidate in (
        output.get("_sdk_request_cache"),
        output.get("request_cache"),
        nested_failure.get("request_cache"),
        metadata.get("request_cache"),
        request_cache,
    ):
        if isinstance(candidate, Mapping):
            cache.update(dict(candidate))
    tool_execution = (
        metadata.get("tool_execution")
        or cache.get("tool_execution")
        or output.get("tool_execution")
        or {}
    )
    if not isinstance(tool_execution, Mapping):
        tool_execution = {}
    tool_scope = _nested_mapping(cache, "request_tool_scope")
    selected_names = list(
        dict.fromkeys(
            [
                *_string_sequence(tool_scope.get("selected_tool_names")),
                *_string_sequence(tool_execution.get("selected_tool_names")),
            ]
        )
    )
    tool_invocations = (
        metadata.get("tool_invocations")
        or cache.get("tool_invocations")
        or output.get("tool_invocations")
        or []
    )
    if not isinstance(tool_invocations, Sequence) or isinstance(
        tool_invocations, str | bytes | bytearray
    ):
        tool_invocations = []
    recorded_receipts = [
        dict(item)
    for item in [
            *receipts,
            *_mapping_list(output.get("tool_receipts")),
            *_mapping_list(output.get("preacquired_tool_receipts")),
            *_mapping_list(metadata.get("tool_receipts")),
            *_mapping_list(metadata.get("preacquired_tool_receipts")),
        ]
        if isinstance(item, Mapping)
    ]
    recorded_receipts = _dedupe_mappings(recorded_receipts)
    trace_summary = _nested_mapping(cache, "trace_summary")
    model_tool_calls = _runtime_tool_call_traces(
        trace_summary=trace_summary,
        tool_invocations=[item for item in tool_invocations if isinstance(item, Mapping)],
        tool_execution=tool_execution,
        receipts=recorded_receipts,
        run_id=run_id,
    )
    explicit_external_write = external_write_state_from_evidence(
        receipts=recorded_receipts,
        tool_names=[
            *_string_sequence(tool_execution.get("model_called_tool_names")),
            *_string_sequence(tool_execution.get("workflow_called_tool_names")),
            *_string_sequence(tool_execution.get("workflow_called_helper_names")),
        ],
        tool_invocations=[item for item in tool_invocations if isinstance(item, Mapping)],
        evidence_complete=bool(
            tool_execution.get("provider_receipt_count_available") is True
            and tool_execution.get("model_tool_call_count") is not None
            and tool_execution.get("workflow_tool_call_count") is not None
        ),
    )
    usage = _first_mapping(
        metadata.get("usage"),
        output.get("_sdk_usage"),
        output.get("usage"),
    )
    cost = _first_mapping(
        metadata.get("cost"),
        output.get("_sdk_cost"),
        output.get("cost"),
    )
    model_metadata = _first_mapping(output.get("_sdk_model"), output.get("model"))
    nested_executions = _runtime_nested_execution_records(cache)
    decision_attempts = [
        *_runtime_decision_attempts(cache.get("decision_ownership")),
        *_runtime_nested_decision_attempts(nested_executions),
    ]
    provider_attempt_records = list(provider_attempts) or _runtime_provider_attempts(
        output,
        metadata,
        cache,
    )
    handoffs = [
        *_runtime_handoffs(output, metadata, cache),
        *_runtime_nested_handoffs(nested_executions, parent_agent=agent),
    ]
    provider_attempt_count = _available_tool_count(
        tool_execution,
        "provider_request_attempt_count",
    )
    provider_success_count = _available_tool_count(
        tool_execution,
        "provider_request_success_count",
    )
    provider_receipt_count = (
        len(recorded_receipts)
        if recorded_receipts
        else _available_tool_count(tool_execution, "provider_receipt_count")
    )
    preacquired_tools = _string_sequence(
        tool_execution.get("preacquired_context_tool_names")
    )
    explicit_tool_free = str(tool_execution.get("mode") or "").strip() in {
        "tool_free",
        "verified_context_tool_free",
    }
    model_input_traces = _runtime_model_input_traces(cache)
    input_component_traces = _runtime_input_component_traces(cache)
    recorded_candidate_universe = _runtime_recorded_candidate_universe(cache)
    preacquired_verified = any(
        component.verified and component.required_identities_visible is not False
        for component in input_component_traces
    )
    failure_evidence = _runtime_failure_evidence(output, metadata, cache)
    trace = build_backend_decision_stage_trace(
        stage_id=stage_id,
        agent=agent,
        stage=stage,
        provider=provider
        or str(metadata.get("provider") or model_metadata.get("provider") or ""),
        model=model
        or str(
            metadata.get("model")
            or model_metadata.get("name")
            or model_metadata.get("model")
            or ""
        ),
        run_id=run_id,
        trace_id=trace_id or str(trace_summary.get("trace_id") or ""),
        trace_event_id=_safe_positive_int(trace_summary.get("event_id")),
        model_visible_inputs=captured_model_inputs,
        raw_results=raw_results,
        recorded_model_called_tools=model_tool_calls,
        attached_tools=_runtime_attached_tools(selected_names, tool_scope, cache),
        tool_admission_fingerprint=str(tool_scope.get("selection_fingerprint") or ""),
        decision_attempts=decision_attempts,
        provider_attempts=provider_attempt_records,
        provider_request_attempt_count=provider_attempt_count,
        provider_request_success_count=provider_success_count,
        provider_receipt_count=provider_receipt_count,
        receipts=recorded_receipts,
        handoffs=handoffs,
        workflow_called_tools=_string_sequence(
            tool_execution.get("workflow_called_tool_names")
        ),
        workflow_called_helpers=_string_sequence(
            tool_execution.get("workflow_called_helper_names")
        ),
        preacquired_context_tools=preacquired_tools,
        tool_invocations=[item for item in tool_invocations if isinstance(item, Mapping)],
        external_write_state=explicit_external_write,
        preacquired_context_verified=preacquired_verified,
        preacquired_context_trace_verified=preacquired_verified,
        intentional_tool_free_synthesis=explicit_tool_free,
        provider_dependent=bool(
            recorded_receipts
            or preacquired_tools
            or provider_attempt_records
            or (_recorded_model_tool_call_count(tool_execution) or 0) > 0
        ),
        usage=usage,
        cost=cost,
        latency_ms=latency_ms,
        retries=int(
            _safe_nonnegative_int(
                cache.get("structured_output_retries")
                or cache.get("rate_limit_retries")
                or cache.get("decision_repairs")
            )
            or 0
        ),
        agent_run_count=1 if run_id else 0,
        terminal_status=terminal_status,
        failure_evidence=failure_evidence,
    )
    trace = trace.model_copy(
        update={
            "model_inputs": model_input_traces or trace.model_inputs,
            "input_components": input_component_traces or trace.input_components,
            "model_called_tools": model_tool_calls or trace.model_called_tools,
            "candidate_universe": (
                recorded_candidate_universe or trace.candidate_universe
            ),
            "preacquired_context_verified": preacquired_verified,
            "request_cache": _runtime_request_cache_projection(cache),
        }
    )
    runtime_findings = list(trace.findings)
    if recorded_candidate_universe:
        universe_ids = {
            candidate.identity_fingerprint
            for candidate in recorded_candidate_universe
        }
        selected_ids = {
            selected
            for attempt in trace.decision_attempts
            if attempt.validator_status == "accepted"
            for selected in attempt.selected_identity_fingerprints
        }
        if selected_ids - universe_ids:
            runtime_findings.append(
                "agent_selection_not_bound_to_raw_provider_candidate_universe"
            )
    unavailable = list(trace.unavailable_evidence)
    if model_input_traces:
        unavailable = [
            item
            for item in unavailable
            if item != "decision_present_without_captured_model_visible_input"
        ]
    if not captured_model_inputs and not model_input_traces:
        unavailable.append("model_visible_input_not_captured")
    if selected_names and any(not item.schema_fingerprint for item in trace.attached_tools):
        unavailable.append("attached_tool_schemas_not_captured")
    if provider_attempt_count is None:
        unavailable.append("provider_request_attempt_count_not_recorded")
    if provider_success_count is None:
        unavailable.append("provider_request_success_count_not_recorded")
    if not decision_attempts:
        unavailable.append("decision_attempts_not_recorded")
    recorded_model_count = _recorded_model_tool_call_count(tool_execution)
    if recorded_model_count is None:
        unavailable.append("model_tool_call_count_not_recorded")
    elif recorded_model_count > len(trace.model_called_tools):
        unavailable.append("model_tool_call_records_incomplete")
    if provider_receipt_count is None:
        unavailable.append("provider_receipt_count_not_recorded")
    if handoffs == [] and _handoff_count_recorded(trace_summary, output) > 0:
        unavailable.append("handoff_consumption_not_recorded")
    unavailable = list(dict.fromkeys(unavailable))
    evaluation = (
        "fail"
        if runtime_findings
        else "partial"
        if unavailable
        else "pass"
    )
    return trace.model_copy(
        update={
            "findings": list(dict.fromkeys(runtime_findings)),
            "unavailable_evidence": unavailable,
            "evaluation_status": evaluation,
        }
    )


def build_accounting_usage_trace_groups(
    outputs: Sequence[Mapping[str, Any]],
) -> tuple[list[list[BackendDecisionStageTrace]], tuple[str, ...]]:
    """Recover nested model stages without counting copied evidence twice.

    Preflight is commonly embedded in each specialist result instead of having its
    own agent_runs row. Execution identities take precedence over content matching.
    For older identity-free events, retain repeated occurrences within one event
    list while collapsing the same list copied into later linked results.

    Instruction-following repair is another real model stage nested in the specialist
    row. It is recorded separately from the specialist's ``_sdk_usage`` and must be
    counted once. Trace identities distinguish equal-token real requests from copied
    aliases. Identity-free or conflicting repair evidence remains explicitly
    incomplete rather than being treated as confirmed accounting.
    """

    measurements = {
        identity: _usage_measurement_fingerprint(output)
        for output in outputs
        if (identity := _usage_execution_identity(output))
    }
    issues: list[str] = []
    unidentified_repair_fingerprints: set[str] = set()
    groups: list[list[BackendDecisionStageTrace]] = []
    for output in outputs:
        stages: list[BackendDecisionStageTrace] = []
        groups.append(stages)
        preflight = _nested_mapping(output, "orchestrator_preflight")
        occurrences: dict[str, int] = {}
        for event in _mapping_list(preflight.get("sdk_usage_events")):
            identity = _usage_execution_identity(event)
            if not identity:
                fingerprint = (
                    _recorded_fingerprint(event.get("preflight_event_fingerprint"))
                    or stable_fingerprint(event)
                )
                ordinal = occurrences.get(fingerprint, 0)
                occurrences[fingerprint] = ordinal + 1
                identity = f"legacy:{fingerprint}:{ordinal}"
            measurement = _usage_measurement_fingerprint(event)
            if identity in measurements:
                if measurements[identity] != measurement:
                    issues.append("conflicting_copied_preflight_usage")
                continue
            measurements[identity] = measurement
            stage = build_backend_decision_stage_trace_from_runtime(
                stage_id=f"preflight-{stable_fingerprint(identity)[:24]}",
                agent=_sanitize_explanation(str(event.get("agent_name") or "orchestrator")),
                stage=_sanitize_explanation(
                    str(event.get("run_stage") or "orchestrator_preflight")
                ),
                linked_output=event,
                terminal_status=str(event.get("status") or "recorded"),
            )
            stages.append(stage)
        for _alias, repair in _instruction_repair_records(output):
            usage = _nested_mapping(repair, "repair_usage")
            cost = _nested_mapping(repair, "repair_cost")
            cache = _nested_mapping(repair, "repair_request_cache")
            telemetry = _nested_mapping(repair, "repair_execution_telemetry")
            if not usage:
                if repair.get("repair_attempted") is True:
                    issues.append("instruction_repair_usage_missing")
                continue
            if (
                usage.get("available") is False
                or usage.get("complete") is False
                or nonnegative_usage_integer(usage.get("requests")) is None
            ):
                issues.append("instruction_repair_usage_incomplete")
            if not cost:
                issues.append("instruction_repair_cost_missing")
            event = {
                "agent_name": "instruction_following_repair",
                "run_stage": "instruction_following.output_repair",
                "usage": usage,
                "cost": cost,
                "request_cache": cache,
                "execution_telemetry": telemetry,
                "status": (
                    "completed" if repair.get("repair_succeeded") is True else "recorded"
                ),
            }
            identity = _usage_execution_identity(event)
            measurement = _usage_measurement_fingerprint(event)
            if not identity:
                issues.append("instruction_repair_usage_identity_missing")
                legacy = stable_fingerprint(
                    {"alias_independent_repair": repair, "measurement": measurement}
                )
                if legacy in unidentified_repair_fingerprints:
                    continue
                unidentified_repair_fingerprints.add(legacy)
                identity = f"legacy-repair:{legacy}"
            if identity in measurements:
                if measurements[identity] != measurement:
                    issues.append("instruction_repair_usage_identity_conflict")
                continue
            measurements[identity] = measurement
            stages.append(
                build_backend_decision_stage_trace_from_runtime(
                    stage_id=f"output-repair-{stable_fingerprint(identity)[:24]}",
                    agent="instruction_following_repair",
                    stage="instruction_following.output_repair",
                    linked_output=event,
                    terminal_status=str(event["status"]),
                    latency_ms=(
                        float(telemetry["total_duration_ms"])
                        if type(telemetry.get("total_duration_ms")) in (int, float)
                        else None
                    ),
                )
            )
    return groups, tuple(dict.fromkeys(issues))


def build_preflight_usage_trace_groups(
    outputs: Sequence[Mapping[str, Any]],
) -> tuple[list[list[BackendDecisionStageTrace]], bool]:
    """Compatibility wrapper for callers that only need an incomplete flag."""

    groups, issues = build_accounting_usage_trace_groups(outputs)
    return groups, bool(issues)


def _instruction_repair_records(
    output: Mapping[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Return canonical and mirrored repair records for identity-based deduplication."""

    records: list[tuple[str, dict[str, Any]]] = []
    for alias, container in (
        ("instruction_following", output),
        ("public_result.instruction_following", _nested_mapping(output, "public_result")),
        ("output.instruction_following", _nested_mapping(output, "output")),
    ):
        repair = container.get("instruction_following")
        if isinstance(repair, Mapping):
            records.append((alias, dict(repair)))
    return records


def _usage_measurement_fingerprint(output: Mapping[str, Any]) -> str:
    return stable_fingerprint({
        "usage": _first_mapping(output.get("_sdk_usage"), output.get("usage")),
        "cost": _first_mapping(output.get("_sdk_cost"), output.get("cost")),
    })


def _usage_execution_identity(output: Mapping[str, Any]) -> str:
    telemetry = _first_mapping(
        output.get("execution_telemetry"), output.get("_execution_telemetry")
    )
    if telemetry.get("run_id"):
        return "execution:" + stable_fingerprint(telemetry["run_id"])
    cache = _first_mapping(output.get("request_cache"), output.get("_sdk_request_cache"))
    summary = _nested_mapping(cache, "trace_summary")
    if summary.get("event_id") and summary.get("trace_id"):
        return "trace-event:" + stable_fingerprint(
            (summary["trace_id"], summary["event_id"])
        )
    return ""


def _nested_mapping(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    nested = value.get(key)
    return dict(nested) if isinstance(nested, Mapping) else {}


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _first_mapping(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _recorded_fingerprint(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if re.fullmatch(r"[a-f0-9]{64}", text) else ""


def _recorded_short_hash(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if re.fullmatch(r"[a-f0-9]{12,64}", text) else ""


def _runtime_model_input_traces(cache: Mapping[str, Any]) -> list[ModelInputTrace]:
    fingerprint = _recorded_fingerprint(cache.get("dynamic_prompt_sha256"))
    if not fingerprint:
        return []
    return [
        ModelInputTrace(
            request_index=1,
            fingerprint=fingerprint,
            serialized_size=_safe_nonnegative_int(cache.get("dynamic_prompt_chars")) or 0,
            input_type="recorded_dynamic_prompt_fingerprint",
        )
    ]


def _runtime_request_cache_projection(cache: Mapping[str, Any]) -> dict[str, Any]:
    fingerprints = {
        key: fingerprint
        for key in (
            "static_prefix_sha256",
            "instructions_sha256",
            "output_schema_sha256",
            "tool_names_sha256",
            "dynamic_prompt_sha256",
        )
        if (fingerprint := _recorded_fingerprint(cache.get(key)))
    }
    prompt_cache_key_hash = _recorded_short_hash(cache.get("prompt_cache_key_hash"))
    if prompt_cache_key_hash:
        fingerprints["prompt_cache_key_hash"] = prompt_cache_key_hash
    scope = _nested_mapping(cache, "request_tool_scope")
    projection: dict[str, Any] = {
        "schema": "keystone.request_cache_trace.v1",
        "fingerprints": fingerprints,
        "request_layout": str(cache.get("request_layout") or "")[:160],
        "repo_instruction_profile": str(cache.get("repo_instruction_profile") or "")[:160],
        "dynamic_prompt_chars": _safe_nonnegative_int(cache.get("dynamic_prompt_chars")),
        "tool_count": _safe_nonnegative_int(cache.get("tool_count")),
        "max_turns": _safe_nonnegative_int(cache.get("max_turns")),
        "max_turns_source": str(cache.get("max_turns_source") or "")[:160],
        "session": {
            "scope": str(cache.get("session_scope") or "")[:160],
            "source": str(cache.get("session_source") or "")[:160],
            "id_hash": _recorded_short_hash(cache.get("session_id_hash")),
            "history_mode": str(cache.get("session_history_mode") or "")[:160],
            "history_limit": _safe_nonnegative_int(cache.get("session_history_limit")),
            "truncation_configured": bool(cache.get("session_truncation_configured")),
        },
        "tool_scope": {
            "selected_tool_count": _safe_nonnegative_int(scope.get("selected_tool_count")),
            "candidate_tool_count": _safe_nonnegative_int(scope.get("candidate_tool_count")),
            "omitted_tool_count": _safe_nonnegative_int(scope.get("omitted_tool_count")),
            "effective_mode": str(scope.get("effective_mode") or "")[:160],
            "source": str(scope.get("source") or scope.get("scope_source") or "")[:160],
            "max_tool_tier": str(scope.get("max_tool_tier") or "")[:160],
            "selection_fingerprint": _recorded_fingerprint(
                scope.get("selection_fingerprint")
            ),
        },
        "retries": {
            key: _safe_nonnegative_int(cache.get(key))
            for key in (
                "structured_output_retries",
                "rate_limit_retries",
                "decision_repairs",
                "tool_corrections",
            )
            if cache.get(key) is not None
        },
        "nested_specialist_executions": _runtime_nested_execution_records(cache),
        "nested_live_read_enforcements": _runtime_nested_live_enforcements(cache),
    }
    validation_diagnostics = sanitized_output_diagnostics(cache.get("validation_diagnostics"))
    if validation_diagnostics.get("failures"):
        projection["validation_diagnostics"] = validation_diagnostics
    signal_evidence = _nested_mapping(cache, "signal_decision_evidence")
    if signal_evidence:
        projection["candidate_evidence"] = {
            "source": str(signal_evidence.get("source") or "")[:160],
            "tool_name": str(signal_evidence.get("tool_name") or "")[:160],
            "candidate_count": _safe_nonnegative_int(
                signal_evidence.get("candidate_count")
            ),
            "evidence_fingerprint": _recorded_fingerprint(
                signal_evidence.get("evidence_fingerprint")
            ),
            "raw_provider_payload_retained": bool(
                signal_evidence.get("raw_provider_payload_retained")
            ),
        }
    return projection


def _runtime_recorded_candidate_universe(
    cache: Mapping[str, Any],
) -> list[CandidateUniverseTrace]:
    """Hydrate a trace-safe universe captured by the signal decision runtime."""

    evidence = _nested_mapping(cache, "signal_decision_evidence")
    if not evidence:
        return []
    candidates = _mapping_list(evidence.get("candidates"))
    candidates_by_id = {
        str(candidate.get("candidate_id") or "").strip(): candidate
        for candidate in candidates
        if str(candidate.get("candidate_id") or "").strip()
    }
    candidate_ids = _string_sequence(evidence.get("candidate_ids"))
    if not candidate_ids:
        candidate_ids = list(candidates_by_id)
    source_tool = str(evidence.get("tool_name") or "signal_history")[:160]
    raw_output_fingerprint = _recorded_fingerprint(
        evidence.get("evidence_fingerprint")
    ) or stable_fingerprint(evidence)
    return [
        CandidateUniverseTrace(
            identity_fingerprint=identity_fingerprint(candidate_id),
            identity_kind="signal_candidate",
            source_tool=source_tool,
            call_order=1,
            candidate_ordinal=index,
            raw_output_fingerprint=raw_output_fingerprint,
            candidate_metadata_fingerprint=stable_fingerprint(
                candidates_by_id.get(candidate_id, {"candidate_id": candidate_id})
            ),
        )
        for index, candidate_id in enumerate(candidate_ids, start=1)
    ]


def _runtime_nested_execution_records(
    cache: Mapping[str, Any],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in _mapping_list(cache.get("nested_specialist_executions"))[:20]:
        attempts: list[dict[str, Any]] = []
        for attempt in _mapping_list(item.get("decision_attempts"))[:4]:
            attempts.append(
                {
                    "attempt": _safe_positive_int(attempt.get("attempt")) or 1,
                    "decision_owner": str(attempt.get("decision_owner") or "")[:160],
                    "decision_stage": str(attempt.get("decision_stage") or "")[:160],
                    "candidate_identity_fingerprints": _recorded_identity_fingerprints(
                        attempt.get("candidate_identity_fingerprints")
                    ),
                    "selected_identity_fingerprints": _recorded_identity_fingerprints(
                        attempt.get("selected_identity_fingerprints")
                    ),
                    "excluded_identity_fingerprints": _recorded_identity_fingerprints(
                        attempt.get("excluded_identity_fingerprints")
                    ),
                    "reasoning_fingerprint": _recorded_fingerprint(
                        attempt.get("reasoning_fingerprint")
                    ),
                    "limitation_fingerprints": _recorded_identity_fingerprints(
                        attempt.get("limitation_fingerprints")
                    ),
                    "validator_status": str(attempt.get("validator_status") or "")[:80],
                    "validator_reason_code": str(
                        attempt.get("validator_reason_code") or ""
                    )[:160],
                }
            )
        handoff = _nested_mapping(item, "handoff")
        records.append(
            {
                "schema": "keystone.nested_specialist_trace.v1",
                "origin": str(item.get("origin") or "")[:160],
                "custom_data_key": str(item.get("custom_data_key") or "")[:160],
                "route_name": str(item.get("route_name") or "")[:160],
                "tool_name": str(item.get("tool_name") or "")[:160],
                "tool_call_id_fingerprint": _recorded_fingerprint(
                    item.get("tool_call_id_fingerprint")
                ),
                "execution_state": str(item.get("execution_state") or "")[:80],
                "nested_execution_mode": str(
                    item.get("nested_execution_mode") or ""
                )[:80],
                "decision_owner": str(item.get("decision_owner") or "")[:160],
                "decision_stage": str(item.get("decision_stage") or "")[:160],
                "decision_attempts": attempts,
                "candidate_identity_fingerprints": _recorded_identity_fingerprints(
                    item.get("candidate_identity_fingerprints")
                ),
                "selected_identity_fingerprints": _recorded_identity_fingerprints(
                    item.get("selected_identity_fingerprints")
                ),
                "receipt_count": _safe_nonnegative_int(item.get("receipt_count")) or 0,
                "receipt_fingerprints": _recorded_identity_fingerprints(
                    item.get("receipt_fingerprints")
                ),
                "repair_attempts": _safe_nonnegative_int(item.get("repair_attempts")) or 0,
                "handoff": {
                    "state": str(handoff.get("state") or "")[:80],
                    "consumption_status": str(
                        handoff.get("consumption_status") or ""
                    )[:160],
                    "terminal_status": str(handoff.get("terminal_status") or "")[:80],
                },
                "provider_write_executed": bool(item.get("provider_write_executed")),
                "reason_code": str(item.get("reason_code") or "")[:160],
                "raw_sensitive_values_retained": False,
            }
        )
    return records


def _runtime_nested_live_enforcements(
    cache: Mapping[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "schema": "keystone.nested_live_read_enforcement_trace.v1",
            "origin": str(item.get("origin") or "")[:160],
            "tool_name": str(item.get("tool_name") or "")[:160],
            "requested_live": item.get("requested_live"),
            "effective_live": bool(item.get("effective_live")),
            "enforced": bool(item.get("enforced")),
            "read_only": bool(item.get("read_only")),
            "mutation_capability_enabled": False,
            "raw_sensitive_values_retained": False,
        }
        for item in _mapping_list(cache.get("nested_live_read_enforcements"))[:20]
    ]


def _runtime_input_component_traces(
    cache: Mapping[str, Any],
) -> list[InputComponentTrace]:
    traces: list[InputComponentTrace] = []
    for key in (
        "static_prefix_sha256",
        "instructions_sha256",
        "output_schema_sha256",
        "tool_names_sha256",
    ):
        fingerprint = _recorded_fingerprint(cache.get(key))
        if fingerprint:
            traces.append(
                InputComponentTrace(
                    category="other",
                    source=f"request_cache.{key}",
                    value_type="recorded_fingerprint",
                    fingerprint=fingerprint,
                    serialized_size=0,
                    verified=True,
                )
            )
    for component in _mapping_list(cache.get("model_visible_components")):
        fingerprint = _recorded_fingerprint(component.get("fingerprint"))
        if not fingerprint:
            continue
        traces.append(
            InputComponentTrace(
                category=str(component.get("category") or "other")[:80],
                source=str(component.get("source") or "recorded_component")[:160],
                value_type=str(component.get("value_type") or "recorded_fingerprint")[:80],
                fingerprint=fingerprint,
                serialized_size=(
                    _safe_nonnegative_int(component.get("serialized_size")) or 0
                ),
                required_identity_fingerprints=[
                    value
                    for value in _string_sequence(
                        component.get("required_identity_fingerprints")
                    )
                    if re.fullmatch(r"[a-f0-9]{64}", value)
                ][:100],
                required_identities_visible=(
                    bool(component.get("required_identities_visible"))
                    if component.get("required_identities_visible") is not None
                    else None
                ),
                verified=bool(component.get("verified")),
            )
        )
    pre_model = _nested_mapping(cache, "pre_model_decision_context")
    if pre_model:
        candidate_ids = _string_sequence(pre_model.get("candidate_ids"))
        visibility_checked = bool(pre_model.get("model_input_visibility_checked"))
        context_complete = bool(pre_model.get("context_complete"))
        traces.append(
            InputComponentTrace(
                category=(
                    "preacquired_evidence"
                    if str(pre_model.get("context_source") or "") != "model_tool_loop"
                    else "mandatory_context"
                ),
                source=str(pre_model.get("context_source") or "pre_model_decision_context")[
                    :160
                ],
                value_type="recorded_context_contract",
                fingerprint=stable_fingerprint(pre_model),
                serialized_size=0,
                required_identity_fingerprints=[
                    identity_fingerprint(value) for value in candidate_ids
                ][:100],
                required_identities_visible=(context_complete if visibility_checked else None),
                verified=bool(context_complete and visibility_checked),
            )
        )
    return traces


def _runtime_attached_tools(
    selected_names: Sequence[str],
    tool_scope: Mapping[str, Any],
    cache: Mapping[str, Any],
) -> list[dict[str, Any]]:
    fingerprint_by_name = _first_mapping(
        tool_scope.get("tool_schema_fingerprints"),
        cache.get("tool_schema_fingerprints"),
    )
    recorded_tools = {
        str(item.get("name") or ""): item
        for item in _mapping_list(tool_scope.get("attached_tools"))
        if str(item.get("name") or "").strip()
    }
    tools: list[dict[str, Any]] = []
    for name in selected_names:
        recorded = recorded_tools.get(name, {})
        fingerprint = _recorded_fingerprint(
            recorded.get("schema_fingerprint") or fingerprint_by_name.get(name)
        )
        tools.append(
            {
                "name": name,
                "schema_fingerprint": fingerprint,
                "schema_source": (
                    str(recorded.get("schema_source") or "recorded_fingerprint")
                    if fingerprint
                    else "not_recorded"
                ),
            }
        )
    return tools


def _runtime_tool_call_traces(
    *,
    trace_summary: Mapping[str, Any],
    tool_invocations: Sequence[Mapping[str, Any]],
    tool_execution: Mapping[str, Any],
    receipts: Sequence[Mapping[str, Any]],
    run_id: str,
) -> list[ToolCallTrace]:
    linked_calls = _mapping_list(trace_summary.get("model_tool_calls"))
    if linked_calls:
        return [
            ToolCallTrace(
                call_order=_safe_positive_int(item.get("call_order")) or index,
                name=str(item.get("name") or "")[:160],
                call_id_fingerprint=_recorded_fingerprint(
                    item.get("call_id_fingerprint")
                ),
                arguments_fingerprint="",
                status=str(item.get("status") or "observed")[:80],
                output_observed=bool(item.get("output_observed")),
            )
            for index, item in enumerate(linked_calls, start=1)
            if str(item.get("name") or "").strip()
        ]
    grouped: dict[int, list[Mapping[str, Any]]] = {}
    for item in tool_invocations:
        index = _safe_positive_int(item.get("invocation_index"))
        if index is not None and str(item.get("tool_name") or "").strip():
            grouped.setdefault(index, []).append(item)
    if grouped:
        traces: list[ToolCallTrace] = []
        for invocation_index, items in sorted(grouped.items()):
            started = items[0]
            terminal = next(
                (
                    item
                    for item in reversed(items)
                    if str(item.get("status") or "") != "started"
                ),
                None,
            )
            name = str(started.get("tool_name") or "")
            matching_receipts = [
                item for item in receipts if str(item.get("tool_name") or "") == name
            ]
            traces.append(
                ToolCallTrace(
                    call_order=invocation_index,
                    name=name,
                    call_id_fingerprint="",
                    arguments_fingerprint="",
                    status=str((terminal or started).get("status") or "attempted"),
                    output_observed=terminal is not None,
                    output_fingerprint=(
                        stable_fingerprint(matching_receipts[-1])
                        if matching_receipts
                        else ""
                    ),
                    output_keys=(
                        sorted(str(key) for key in matching_receipts[-1])[:30]
                        if matching_receipts
                        else []
                    ),
                )
            )
        return traces
    return [
        ToolCallTrace(
            call_order=index,
            name=name,
            call_id_fingerprint="",
            arguments_fingerprint="",
            status="recorded_name_only",
            output_observed=False,
        )
        for index, name in enumerate(
            _string_sequence(tool_execution.get("model_called_tool_names")),
            start=1,
        )
    ]


def _runtime_decision_attempts(value: Any) -> list[DecisionAttemptEvidence]:
    if not isinstance(value, Mapping):
        return []
    ownership = dict(value)
    attempts = _mapping_list(ownership.get("attempts"))
    if not attempts and ownership.get("validator_outcome"):
        attempts = [ownership]
    result: list[DecisionAttemptEvidence] = []
    for index, item in enumerate(attempts, start=1):
        validator = _nested_mapping(item, "validator_outcome")
        candidate_ids = _string_sequence(item.get("candidate_ids"))
        selected_ids = _string_sequence(
            item.get("selected_candidate_ids") or item.get("selected_candidate_id")
        )
        excluded_ids = _string_sequence(item.get("excluded_candidate_ids"))
        recorded_assessments = _mapping_list(item.get("candidate_assessments"))
        assessments = (
            tuple(
                (
                    str(assessment.get("candidate_id") or ""),
                    str(assessment.get("disposition") or ""),
                    str(assessment.get("rationale") or ""),
                )
                for assessment in recorded_assessments
                if str(assessment.get("candidate_id") or "").strip()
            )
            if recorded_assessments
            else tuple(
                [(candidate, "selected", "") for candidate in selected_ids]
                + [
                    (candidate, "excluded", "")
                    for candidate in excluded_ids
                    if candidate not in selected_ids
                ]
            )
        )
        status = str(validator.get("status") or "not_evaluated")
        result.append(
            DecisionAttemptEvidence(
                attempt=_safe_positive_int(item.get("attempt")) or index,
                decision_owner=str(
                    item.get("decision_owner")
                    or ownership.get("decision_owner")
                    or "specialist_agent"
                ),
                decision_stage=str(
                    item.get("decision_stage")
                    or ownership.get("decision_stage")
                    or "specialist_selection"
                ),
                candidate_values=tuple(candidate_ids),
                selected_values=tuple(selected_ids),
                assessments=assessments,
                reasoning=str(item.get("reasoning") or ""),
                limitations=tuple(_string_sequence(item.get("limitations"))),
                validator_status=status,
                validator_reason_code=str(validator.get("reason_code") or ""),
                repair_requested=bool(
                    status in {"repair_required", "rejected"}
                    and index < len(attempts)
                ),
            )
        )
    return result


def _runtime_nested_decision_attempts(
    records: Sequence[Mapping[str, Any]],
) -> list[DecisionAttemptEvidence]:
    result: list[DecisionAttemptEvidence] = []
    for record in records:
        owner = str(record.get("decision_owner") or "specialist_agent")
        stage = str(record.get("decision_stage") or "nested_specialist_selection")
        attempts = _mapping_list(record.get("decision_attempts"))
        for index, item in enumerate(attempts, start=1):
            candidate_values = tuple(
                f"sha256:{value}"
                for value in _recorded_identity_fingerprints(
                    item.get("candidate_identity_fingerprints")
                )
            )
            selected_values = tuple(
                f"sha256:{value}"
                for value in _recorded_identity_fingerprints(
                    item.get("selected_identity_fingerprints")
                )
            )
            excluded_values = tuple(
                f"sha256:{value}"
                for value in _recorded_identity_fingerprints(
                    item.get("excluded_identity_fingerprints")
                )
            )
            status = str(item.get("validator_status") or "not_evaluated")
            result.append(
                DecisionAttemptEvidence(
                    attempt=_safe_positive_int(item.get("attempt")) or index,
                    decision_owner=str(item.get("decision_owner") or owner),
                    decision_stage=str(item.get("decision_stage") or stage),
                    candidate_values=candidate_values,
                    selected_values=selected_values,
                    assessments=tuple(
                        [(value, "selected", "") for value in selected_values]
                        + [(value, "excluded", "") for value in excluded_values]
                    ),
                    reasoning=(
                        f"sha256:{fingerprint}"
                        if (
                            fingerprint := _recorded_fingerprint(
                                item.get("reasoning_fingerprint")
                            )
                        )
                        else ""
                    ),
                    limitations=tuple(
                        f"sha256:{value}"
                        for value in _recorded_identity_fingerprints(
                            item.get("limitation_fingerprints")
                        )
                    ),
                    validator_status=status,
                    validator_reason_code=str(
                        item.get("validator_reason_code") or ""
                    ),
                    repair_requested=bool(
                        status in {"repair_required", "rejected"}
                        and index < len(attempts)
                    ),
                )
            )
    return result


def _runtime_provider_attempts(
    *sources: Mapping[str, Any],
) -> list[ProviderAttemptEvidence]:
    values: list[dict[str, Any]] = []
    for source in sources:
        values.extend(_mapping_list(source.get("provider_attempts")))
        values.extend(
            _mapping_list(_nested_mapping(source, "retrieval_diagnostics").get("provider_attempts"))
        )
    attempts: list[ProviderAttemptEvidence] = []
    for index, item in enumerate(_dedupe_mappings(values), start=1):
        provider = str(item.get("provider") or item.get("provider_system") or "")
        if not provider:
            continue
        latency = item.get("latency_ms")
        try:
            latency_ms = max(0.0, float(latency)) if latency is not None else None
        except (TypeError, ValueError):
            latency_ms = None
        attempts.append(
            ProviderAttemptEvidence(
                provider=provider[:160],
                operation=str(item.get("operation") or "")[:160],
                status=str(item.get("status") or "attempted")[:80],
                attempt=_safe_positive_int(item.get("attempt")) or index,
                fallback_from=str(item.get("fallback_from") or "")[:160],
                receipt_verified=bool(
                    item.get("receipt_verified") or item.get("verified")
                ),
                latency_ms=latency_ms,
            )
        )
    return attempts


def _runtime_handoffs(*sources: Mapping[str, Any]) -> list[HandoffEvidence]:
    values: list[dict[str, Any]] = []
    for source in sources:
        for key in ("handoffs", "handoff_consumption", "handoff_consumptions"):
            values.extend(_mapping_list(source.get(key)))
    handoffs: list[HandoffEvidence] = []
    for item in _dedupe_mappings(values):
        if "consumed" not in item:
            continue
        source_agent = str(item.get("source_agent") or "")
        downstream_agent = str(
            item.get("downstream_agent") or item.get("target_agent") or ""
        )
        if not source_agent or not downstream_agent:
            continue
        handoffs.append(
            HandoffEvidence(
                source_agent=source_agent[:160],
                downstream_agent=downstream_agent[:160],
                evidence_values=tuple(
                    _string_sequence(
                        item.get("evidence_fingerprints")
                        or item.get("evidence_values")
                        or item.get("evidence_ids")
                    )
                ),
                provided=bool(item.get("provided", True)),
                consumed=bool(item.get("consumed")),
                consumption_source=str(item.get("consumption_source") or "")[:160],
            )
        )
    return handoffs


def _runtime_nested_handoffs(
    records: Sequence[Mapping[str, Any]],
    *,
    parent_agent: str,
) -> list[HandoffEvidence]:
    handoffs: list[HandoffEvidence] = []
    for record in records:
        route_name = str(record.get("route_name") or "")
        if not route_name or not parent_agent:
            continue
        handoff = _nested_mapping(record, "handoff")
        state = str(handoff.get("state") or "")
        consumption = str(handoff.get("consumption_status") or "")
        if not state and not consumption:
            continue
        handoffs.append(
            HandoffEvidence(
                source_agent=route_name[:160],
                downstream_agent=parent_agent[:160],
                evidence_values=tuple(
                    _recorded_identity_fingerprints(
                        record.get("selected_identity_fingerprints")
                    )
                ),
                provided=state not in {"", "not_executed"},
                consumed=consumption in {
                    "returned_to_chief_model",
                    "blocked_result_returned_to_chief_model",
                },
                consumption_source=str(record.get("origin") or "")[:160],
            )
        )
    return handoffs


def _recorded_identity_fingerprints(value: Any) -> list[str]:
    return [
        fingerprint
        for item in _string_sequence(value)
        if (fingerprint := _recorded_fingerprint(item))
    ][:100]


def _available_tool_count(tool_execution: Mapping[str, Any], key: str) -> int | None:
    if tool_execution.get(f"{key}_available") is not True:
        return None
    return _safe_nonnegative_int(tool_execution.get(key)) or 0


def _recorded_model_tool_call_count(tool_execution: Mapping[str, Any]) -> int | None:
    mode = str(tool_execution.get("mode") or "").strip()
    if not mode or "before_tool_evidence" in mode:
        return None
    value = tool_execution.get("model_tool_call_count")
    return _safe_nonnegative_int(value) if value is not None else None


def _safe_positive_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _runtime_failure_evidence(
    output: Mapping[str, Any],
    metadata: Mapping[str, Any],
    cache: Mapping[str, Any],
) -> dict[str, Any]:
    public_failure = _nested_mapping(output, "failure")
    kind = str(
        metadata.get("failure_kind")
        or public_failure.get("kind")
        or output.get("error_kind")
        or ""
    )[:160]
    evidence: dict[str, Any] = {}
    if kind:
        evidence["kind"] = kind
    for key in (
        "attempt_count",
        "structured_output_retries",
        "decision_repairs",
        "tool_corrections",
        "rate_limit_retries",
    ):
        value = metadata.get(key, cache.get(key))
        if value is not None:
            try:
                evidence[key] = max(0, int(value))
            except (TypeError, ValueError):
                continue
    error_fingerprint = _recorded_fingerprint(
        _nested_mapping(output, "internal_diagnostics").get("error_fingerprint")
    )
    if error_fingerprint:
        evidence["error_fingerprint"] = error_fingerprint
    if evidence:
        evidence["raw_error_retained"] = False
    return evidence


def _handoff_count_recorded(
    trace_summary: Mapping[str, Any],
    output: Mapping[str, Any],
) -> int:
    try:
        return max(
            0,
            int(
                trace_summary.get("handoff_count")
                or output.get("handoff_count")
                or 0
            ),
        )
    except (TypeError, ValueError):
        return 0


def assemble_backend_decision_scenario_trace(
    *,
    scenario_id: str,
    request_text: str,
    stages: Sequence[BackendDecisionStageTrace],
    retain_request_text_when_safe: bool = True,
) -> BackendDecisionScenarioTrace:
    """Assemble a continuous trace and verify cross-stage handoff consumption."""

    safe_to_retain = retain_request_text_when_safe and not _PRIVATE_TEXT.search(request_text)
    retained_request = request_text if safe_to_retain else ""
    findings = [finding for stage in stages for finding in stage.findings]
    for index, stage in enumerate(stages[:-1]):
        later_agents = {item.agent for item in stages[index + 1 :]}
        for handoff in stage.handoffs:
            if handoff.provided and handoff.downstream_agent not in later_agents:
                findings.append(f"handoff_target_has_no_later_stage:{handoff.downstream_agent}")
    if any(stage.evaluation_status == "fail" for stage in stages) or findings:
        evaluation = "fail"
    elif any(stage.evaluation_status == "partial" for stage in stages):
        evaluation = "partial"
    else:
        evaluation = "pass"
    return BackendDecisionScenarioTrace(
        scenario_id=scenario_id,
        request_text=retained_request,
        request_text_retained=safe_to_retain,
        request_fingerprint=stable_fingerprint(request_text),
        request_capture_note=(
            "exact_safe_natural_language_request"
            if safe_to_retain
            else "request_text_withheld_by_trace_privacy_guard"
        ),
        stages=list(stages),
        agent_run_count=sum(stage.consumption.agent_run_count for stage in stages),
        model_request_count=sum(stage.consumption.model_request_count for stage in stages),
        evaluation_status=evaluation,
        findings=list(dict.fromkeys(findings)),
    )


def assemble_backend_decision_scenario_trace_from_runtime(
    *,
    scenario_id: str,
    request_fingerprint: str,
    stages: Sequence[BackendDecisionStageTrace],
) -> BackendDecisionScenarioTrace:
    """Assemble linked production stages without rehydrating the private request."""

    findings = [finding for stage in stages for finding in stage.findings]
    for index, stage in enumerate(stages[:-1]):
        later_agents = {item.agent for item in stages[index + 1 :]}
        for handoff in stage.handoffs:
            if handoff.provided and handoff.downstream_agent not in later_agents:
                findings.append(
                    f"handoff_target_has_no_later_stage:{handoff.downstream_agent}"
                )
    if any(stage.evaluation_status == "fail" for stage in stages) or findings:
        evaluation = "fail"
    elif any(stage.evaluation_status == "partial" for stage in stages):
        evaluation = "partial"
    else:
        evaluation = "pass"
    return BackendDecisionScenarioTrace(
        scenario_id=scenario_id,
        request_text="",
        request_text_retained=False,
        request_fingerprint=request_fingerprint,
        request_capture_note="request_text_withheld_fingerprint_only",
        stages=list(stages),
        agent_run_count=sum(stage.consumption.agent_run_count for stage in stages),
        model_request_count=sum(stage.consumption.model_request_count for stage in stages),
        evaluation_status=evaluation,
        findings=list(dict.fromkeys(findings)),
    )


def _input_component_trace(
    component: ModelVisibleComponent,
    combined_visible_text: str,
) -> InputComponentTrace:
    serialized = _serialize(component.value)
    required = [
        identity_fingerprint(value)
        for value in component.required_identity_values
        if str(value).strip()
    ]
    visible = (
        all(str(value) in combined_visible_text for value in component.required_identity_values)
        if component.required_identity_values
        else None
    )
    return InputComponentTrace(
        category=component.category,
        source=component.source,
        value_type=type(component.value).__name__,
        fingerprint=stable_fingerprint(component.value),
        serialized_size=len(serialized.encode("utf-8")),
        required_identity_fingerprints=required,
        required_identities_visible=visible,
        verified=component.verified,
    )


def _attached_tool_traces(
    tools: Sequence[Any],
    *,
    admission_by_tool: Mapping[str, Mapping[str, Any]],
    forbidden_tools: Sequence[str],
) -> list[AttachedToolTrace]:
    forbidden = set(forbidden_tools)
    traces: list[AttachedToolTrace] = []
    for tool in tools:
        name = str(getattr(tool, "name", "") or "").strip()
        if not name and isinstance(tool, Mapping):
            name = str(tool.get("name") or "").strip()
        if not name:
            continue
        recorded_schema_fingerprint = (
            str(tool.get("schema_fingerprint") or "").strip()
            if isinstance(tool, Mapping)
            else ""
        )
        schema = _tool_schema(tool)
        admission = admission_by_tool.get(name) or {}
        rationale = str(admission.get("rationale") or "")
        traces.append(
            AttachedToolTrace(
                name=name,
                schema_fingerprint=(
                    recorded_schema_fingerprint
                    or (stable_fingerprint(schema) if schema is not None else "")
                ),
                schema_source=(
                    str(tool.get("schema_source") or "recorded_fingerprint")
                    if recorded_schema_fingerprint and isinstance(tool, Mapping)
                    else _tool_schema_source(tool)
                ),
                admission_tier=str(admission.get("tier") or ""),
                admission_rationale_fingerprint=(
                    stable_fingerprint(rationale) if rationale else ""
                ),
                forbidden=name in forbidden,
            )
        )
    return traces


def _tool_call_and_candidate_traces(
    raw_results: Sequence[Any],
    *,
    candidate_identity_paths: Mapping[str, Sequence[str]],
) -> tuple[list[ToolCallTrace], list[CandidateUniverseTrace]]:
    calls: list[ToolCallTrace] = []
    universe: list[CandidateUniverseTrace] = []
    global_order = 0
    for raw_result in raw_results:
        items = _result_items(raw_result)
        outputs: dict[str, Any] = {}
        for index, item in enumerate(items, start=1):
            if _item_kind(item) != "output":
                continue
            call_id = _item_call_id(item) or f"output-{index}"
            outputs[call_id] = _item_output(item)
        for _index, item in enumerate(items, start=1):
            if _item_kind(item) != "call":
                continue
            name = _item_tool_name(item)
            if not name:
                continue
            global_order += 1
            call_id = _item_call_id(item) or f"call-{global_order}"
            arguments = _item_arguments(item)
            output = outputs.get(call_id)
            parsed_output = _parse_json(output)
            output_observed = call_id in outputs
            output_fingerprint = stable_fingerprint(parsed_output) if output_observed else ""
            output_keys = (
                sorted(str(key) for key in parsed_output)[:30]
                if isinstance(parsed_output, Mapping)
                else []
            )
            candidates = _candidate_entries(
                name,
                parsed_output,
                candidate_identity_paths.get(name) or (),
                call_order=global_order,
                output_fingerprint=output_fingerprint,
            )
            universe.extend(candidates)
            calls.append(
                ToolCallTrace(
                    call_order=global_order,
                    name=name,
                    call_id_fingerprint=identity_fingerprint(call_id),
                    argument_keys=sorted(str(key) for key in arguments)[:30],
                    safe_arguments={
                        str(key): value
                        for key, value in arguments.items()
                        if key in _SAFE_ARGUMENT_KEYS
                        and (value is None or isinstance(value, bool | int | float | str))
                    },
                    arguments_fingerprint=stable_fingerprint(arguments),
                    status="completed" if output_observed else "attempted",
                    output_observed=output_observed,
                    output_fingerprint=output_fingerprint,
                    output_keys=output_keys,
                    candidate_count=len(candidates),
                )
            )
    deduped: dict[tuple[str, str, int], CandidateUniverseTrace] = {}
    for candidate in universe:
        key = (
            candidate.identity_fingerprint,
            candidate.source_tool,
            candidate.call_order,
        )
        deduped.setdefault(key, candidate)
    return calls, list(deduped.values())


def _candidate_entries(
    tool_name: str,
    output: Any,
    paths: Sequence[str],
    *,
    call_order: int,
    output_fingerprint: str,
) -> list[CandidateUniverseTrace]:
    entries: list[CandidateUniverseTrace] = []
    ordinal = 0
    for path in paths:
        for identity, parent in _values_at_path(output, path):
            if not str(identity or "").strip():
                continue
            ordinal += 1
            entries.append(
                CandidateUniverseTrace(
                    identity_fingerprint=identity_fingerprint(identity),
                    identity_kind=path.rsplit(".", maxsplit=1)[-1].replace("[]", ""),
                    source_tool=tool_name,
                    call_order=call_order,
                    candidate_ordinal=ordinal,
                    raw_output_fingerprint=output_fingerprint,
                    candidate_metadata_fingerprint=stable_fingerprint(parent),
                )
            )
    return entries


def _values_at_path(value: Any, path: str) -> list[tuple[Any, Any]]:
    nodes: list[tuple[Any, Any]] = [(value, value)]
    for token in path.split("."):
        is_many = token.endswith("[]")
        key = token[:-2] if is_many else token
        next_nodes: list[tuple[Any, Any]] = []
        for node, _parent in nodes:
            if not isinstance(node, Mapping) or key not in node:
                continue
            child = node[key]
            if (
                is_many
                and isinstance(child, Sequence)
                and not isinstance(child, str | bytes | bytearray)
            ):
                next_nodes.extend((item, item) for item in child)
            else:
                next_nodes.append((child, node))
        nodes = next_nodes
    return nodes


def _decision_attempt_trace(item: DecisionAttemptEvidence) -> DecisionAttemptTrace:
    return DecisionAttemptTrace(
        attempt=item.attempt,
        decision_owner=item.decision_owner,
        decision_stage=item.decision_stage,
        candidate_identity_fingerprints=[
            _trace_identity_fingerprint(value)
            for value in item.candidate_values
            if str(value).strip()
        ],
        selected_identity_fingerprints=[
            _trace_identity_fingerprint(value)
            for value in item.selected_values
            if str(value).strip()
        ],
        assessments=[
            CandidateAssessmentTrace(
                identity_fingerprint=_trace_identity_fingerprint(candidate),
                disposition=disposition,
                rationale_fingerprint=(
                    _trace_content_fingerprint(rationale) if str(rationale).strip() else ""
                ),
            )
            for candidate, disposition, rationale in item.assessments
        ],
        reasoning_fingerprint=(
            _trace_content_fingerprint(item.reasoning)
            if str(item.reasoning).strip()
            else ""
        ),
        limitation_fingerprints=[
            _trace_content_fingerprint(value)
            for value in item.limitations
            if str(value).strip()
        ],
        validator_status=item.validator_status,
        validator_reason_code=item.validator_reason_code,
        repair_requested=item.repair_requested,
    )


def _trace_identity_fingerprint(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("sha256:") and re.fullmatch(r"[a-f0-9]{64}", text[7:]):
        return text[7:]
    return identity_fingerprint(value)


def _trace_content_fingerprint(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("sha256:") and re.fullmatch(r"[a-f0-9]{64}", text[7:]):
        return text[7:]
    return stable_fingerprint(value)


def _handoff_trace(item: HandoffEvidence) -> HandoffTrace:
    evidence_fingerprints = [
        (
            str(value)
            if re.fullmatch(r"[a-f0-9]{64}", str(value))
            else identity_fingerprint(value)
        )
        for value in item.evidence_values
        if str(value).strip()
    ]
    return HandoffTrace(
        source_agent=item.source_agent,
        downstream_agent=item.downstream_agent,
        evidence_fingerprints=evidence_fingerprints,
        provided=item.provided,
        consumed=item.consumed,
        consumption_source=item.consumption_source,
    )


def _receipt_trace(receipt: Mapping[str, Any]) -> ReceiptTrace:
    identities: list[str] = []
    for key in (
        "identity_fingerprints",
        "event_id",
        "record_id",
        "message_id",
        "thread_id",
        "file_id",
        "document_id",
        "folder_id",
        "spreadsheet_id",
        "item_key",
        "collection_key",
    ):
        value = receipt.get(key)
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            identities.extend(str(item) for item in value if str(item).strip())
        elif value:
            identities.append(str(value))
    already_fingerprinted = [value for value in identities if re.fullmatch(r"[a-f0-9]{64}", value)]
    raw_identities = [value for value in identities if value not in already_fingerprinted]
    status = str(receipt.get("status") or "")
    verification = receipt.get("verification")
    return ReceiptTrace(
        tool_name=str(receipt.get("tool_name") or ""),
        provider=str(receipt.get("provider") or receipt.get("provider_system") or ""),
        operation=str(
            receipt.get("operation") or receipt.get("action") or receipt.get("operation_type") or ""
        ),
        status=status,
        verified=bool(
            verification is True
            or str(verification or "").lower() in _SUCCESS_STATUSES
            or str(receipt.get("verified") or "").lower() in {"1", "true", "yes"}
        ),
        identity_fingerprints=list(
            dict.fromkeys(
                [*already_fingerprinted, *(identity_fingerprint(value) for value in raw_identities)]
            )
        )[:100],
        receipt_fingerprint=stable_fingerprint(receipt),
    )


def _tool_schema(tool: Any) -> Any:
    if isinstance(tool, Mapping):
        return tool.get("params_json_schema") or tool.get("parameters")
    return (
        getattr(tool, "params_json_schema", None)
        or getattr(tool, "parameters", None)
        or getattr(tool, "input_json_schema", None)
    )


def _tool_schema_source(tool: Any) -> str:
    for name in ("params_json_schema", "parameters", "input_json_schema"):
        if (isinstance(tool, Mapping) and tool.get(name) is not None) or getattr(
            tool, name, None
        ) is not None:
            return name
    return "tool_name_only"


def _result_items(raw_result: Any) -> list[Any]:
    if isinstance(raw_result, Mapping):
        return list(raw_result.get("new_items") or raw_result.get("items") or [])
    return list(getattr(raw_result, "new_items", []) or [])


def _raw_item(item: Any) -> Any:
    if isinstance(item, Mapping):
        return item.get("raw_item") or item
    return getattr(item, "raw_item", None) or item


def _item_kind(item: Any) -> Literal["call", "output", "other"]:
    raw = _raw_item(item)
    type_name = str(
        (raw.get("type") if isinstance(raw, Mapping) else getattr(raw, "type", "")) or ""
    ).lower()
    class_name = f"{type(item).__name__} {type(raw).__name__}".lower()
    if (
        "tool_call_output" in type_name
        or "function_call_output" in type_name
        or "toolcalloutput" in class_name
    ):
        return "output"
    if "function_call" in type_name or "tool_call" in type_name or "toolcallitem" in class_name:
        return "call"
    return "other"


def _item_call_id(item: Any) -> str:
    raw = _raw_item(item)
    if isinstance(raw, Mapping):
        return str(raw.get("call_id") or raw.get("id") or "")
    return str(getattr(raw, "call_id", "") or getattr(raw, "id", "") or "")


def _item_tool_name(item: Any) -> str:
    raw = _raw_item(item)
    if isinstance(raw, Mapping):
        return str(raw.get("name") or raw.get("tool_name") or "")
    return str(getattr(raw, "name", "") or getattr(raw, "tool_name", "") or "")


def _item_arguments(item: Any) -> dict[str, Any]:
    raw = _raw_item(item)
    value = raw.get("arguments") if isinstance(raw, Mapping) else getattr(raw, "arguments", {})
    parsed = _parse_json(value)
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _item_output(item: Any) -> Any:
    raw = _raw_item(item)
    if isinstance(item, Mapping) and "output" in item:
        return item.get("output")
    if getattr(item, "output", None) is not None:
        return item.output
    if isinstance(raw, Mapping):
        return raw.get("output")
    return getattr(raw, "output", None)


def _parse_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _serialize(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(_jsonable(value), ensure_ascii=True, sort_keys=True, default=str)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if hasattr(value, "__dict__"):
        return {str(key): _jsonable(item) for key, item in vars(value).items()}
    return str(value)


def _bounded_numeric_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    bounded: dict[str, Any] = {}
    for key, item in list(value.items())[:30]:
        if str(key) in NUMERIC_TOKEN_USAGE_KEYS:
            bounded[str(key)] = nonnegative_usage_integer(item)
        elif str(key) == "request_usage_entries":
            bounded[str(key)] = project_request_usage_entries(item)
        elif item is None or isinstance(item, bool | int | float):
            bounded[str(key)] = item
        elif str(key) in {"source", "pricing_provider", "pricing_model", "note"}:
            bounded[str(key)] = _sanitize_explanation(str(item))[:500]
    return bounded


def _string_sequence(value: Any) -> list[str]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        values = [value]
    else:
        values = list(value)
    return list(
        dict.fromkeys(
            str(item or "").strip() for item in values if str(item or "").strip()
        )
    )


def _dedupe_mappings(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    fingerprints: set[str] = set()
    for value in values:
        fingerprint = stable_fingerprint(value)
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        deduped.append(dict(value))
    return deduped


def _safe_nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _sanitize_explanation(value: str) -> str:
    cleaned = " ".join(str(value or "").replace("\u2014", "-").split())
    cleaned = _PRIVATE_TEXT.sub("[redacted]", cleaned)
    return cleaned[:2_000]


__all__ = [
    "BackendDecisionScenarioTrace",
    "BackendDecisionStageTrace",
    "DecisionAttemptEvidence",
    "HandoffEvidence",
    "ModelVisibleComponent",
    "ProductionWrapperMatrixEntry",
    "ProviderAttemptEvidence",
    "assemble_backend_decision_scenario_trace",
    "assemble_backend_decision_scenario_trace_from_runtime",
    "build_accounting_usage_trace_groups",
    "build_backend_decision_stage_trace",
    "build_backend_decision_stage_trace_from_runtime",
    "build_preflight_usage_trace_groups",
    "production_wrapper_matrix",
    "stable_fingerprint",
]
