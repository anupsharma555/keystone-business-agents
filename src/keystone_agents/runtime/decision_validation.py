"""Shared validation and trace contracts for agent-owned semantic choices."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from keystone_agents.receipts.normalization import identity_fingerprint
from keystone_agents.schemas.decision_ownership import (
    AgentDecisionRecord,
    DecisionTelemetryEvent,
    DecisionValidatorOutcome,
)


def _ids(values: Iterable[object]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            " ".join(str(value or "").split())[:200] for value in values if str(value or "").strip()
        )
    )


def _candidate_identity_integrity_errors(
    candidate_ids: Iterable[object],
    provider_identities_by_candidate: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...]:
    """Reject ambiguous aliases without exposing raw provider identities."""

    candidates = _ids(candidate_ids)
    candidate_set = set(candidates)
    owners: dict[str, set[str]] = {candidate_id: {candidate_id} for candidate_id in candidates}
    errors: list[str] = []
    for candidate_id, identities in provider_identities_by_candidate.items():
        if candidate_id not in candidate_set:
            errors.append(f"provider_identity_owner_missing:{candidate_id}")
            continue
        for identity in identities:
            owners.setdefault(identity, set()).add(candidate_id)
    for identity, identity_owners in owners.items():
        if len(identity_owners) <= 1:
            continue
        errors.append(
            "provider_identity_alias_collision:"
            + identity_fingerprint(identity)[:16]
            + ":"
            + ",".join(sorted(identity_owners))
        )
    return _ids(errors)


@dataclass(frozen=True)
class SpecialistDecisionEvidence:
    """Bounded identities against which Python validates one model decision."""

    candidate_ids: tuple[str, ...]
    required_selected_ids: tuple[str, ...] = ()
    mandatory_selected_ids: tuple[str, ...] = ()
    selection_required: bool = False
    require_complete_assessments: bool = True
    exact_required_selection: bool = False
    provider_identities_by_candidate: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    identity_integrity_errors: tuple[str, ...] = ()

    @classmethod
    def build(
        cls,
        candidate_ids: Iterable[object],
        *,
        required_selected_ids: Iterable[object] = (),
        mandatory_selected_ids: Iterable[object] = (),
        selection_required: bool = False,
        require_complete_assessments: bool = True,
        exact_required_selection: bool = False,
        provider_identities_by_candidate: Mapping[object, Any] | None = None,
        identity_integrity_errors: Iterable[object] = (),
    ) -> SpecialistDecisionEvidence:
        normalized_provider_identities: dict[str, tuple[str, ...]] = {}
        for raw_candidate_id, raw_provider_identities in (
            provider_identities_by_candidate or {}
        ).items():
            candidate_ids_for_key = _ids((raw_candidate_id,))
            if not candidate_ids_for_key:
                continue
            if isinstance(raw_provider_identities, str | bytes | bytearray):
                provider_identities = (raw_provider_identities,)
            elif isinstance(raw_provider_identities, Iterable):
                provider_identities = raw_provider_identities
            else:
                provider_identities = (raw_provider_identities,)
            normalized_provider_identities[candidate_ids_for_key[0]] = _ids(provider_identities)
        normalized_candidate_ids = _ids(candidate_ids)
        detected_integrity_errors = _candidate_identity_integrity_errors(
            normalized_candidate_ids,
            normalized_provider_identities,
        )
        return cls(
            candidate_ids=normalized_candidate_ids,
            required_selected_ids=_ids(required_selected_ids),
            mandatory_selected_ids=_ids(mandatory_selected_ids),
            selection_required=selection_required,
            require_complete_assessments=require_complete_assessments,
            exact_required_selection=exact_required_selection,
            provider_identities_by_candidate=normalized_provider_identities,
            identity_integrity_errors=_ids(
                (*identity_integrity_errors, *detected_integrity_errors)
            ),
        )


@dataclass(frozen=True)
class AgentDecisionContract:
    """Route-specific evidence resolver plus the shared ownership invariant."""

    route: str
    decision_stage: str
    evidence_resolver: Callable[[Any], SpecialistDecisionEvidence]
    tool_evidence_resolver: (
        Callable[[Iterable[Any]], SpecialistDecisionEvidence] | None
    ) = None
    decision_owner: str = "specialist_agent"
    max_selected: int = 20
    pre_model_candidate_ids: tuple[str, ...] = ()
    mandatory_pre_model_context_ids: tuple[str, ...] = ()
    pre_model_context_source: str = "model_tool_loop"
    pre_model_source_visibility_required: bool = True
    output_consistency_validator: Callable[[Any], tuple[str, str] | None] | None = None
    output_normalizer: Callable[[Any], Mapping[str, Any] | None] | None = None
    allow_plausible_alternatives_after_selection: bool = False
    max_decision_repairs: int = 1


def bind_authoritative_tool_evidence(
    output_evidence: SpecialistDecisionEvidence,
    tool_evidence: SpecialistDecisionEvidence,
) -> SpecialistDecisionEvidence:
    """Bind semantic output claims to the candidates actually returned by tools.

    Tool evidence owns the selectable universe. The specialist output still owns
    which identities it used, whether a selection is required, and its semantic
    decision. This prevents a model repair from silently shrinking or replacing the
    candidate set while keeping Python out of candidate selection.
    """

    if not tool_evidence.candidate_ids:
        return output_evidence
    provider_identities: dict[str, tuple[str, ...]] = {
        candidate_id: tuple(identities)
        for candidate_id, identities in tool_evidence.provider_identities_by_candidate.items()
    }
    tool_candidate_set = set(tool_evidence.candidate_ids)
    identity_integrity_errors = list(tool_evidence.identity_integrity_errors)
    for candidate_id, identities in output_evidence.provider_identities_by_candidate.items():
        if candidate_id not in tool_candidate_set:
            continue
        authoritative_identities = set(provider_identities.get(candidate_id, ()))
        for identity in identities:
            if identity in authoritative_identities:
                continue
            identity_integrity_errors.append(
                "output_provider_identity_mismatch:"
                + candidate_id
                + ":"
                + identity_fingerprint(identity)[:16]
            )
    return SpecialistDecisionEvidence.build(
        tool_evidence.candidate_ids,
        required_selected_ids=output_evidence.required_selected_ids,
        mandatory_selected_ids=output_evidence.mandatory_selected_ids,
        selection_required=output_evidence.selection_required,
        require_complete_assessments=tool_evidence.require_complete_assessments,
        exact_required_selection=output_evidence.exact_required_selection,
        provider_identities_by_candidate=provider_identities,
        identity_integrity_errors=identity_integrity_errors,
    )


@dataclass(frozen=True)
class DecisionRepairEvidenceReplay:
    """One bounded, provider-read-free evidence packet for decision repair."""

    status: str
    prompt_context: str = ""
    replayed_tool_names: tuple[str, ...] = ()
    evidence_fingerprints: tuple[str, ...] = ()
    aggregate_fingerprint: str = ""
    candidate_ids: tuple[str, ...] = ()
    reason_code: str = ""
    feedback: str = ""
    source_output_count: int = 0
    replayed_output_count: int = 0
    source_serialized_chars: int = 0
    replay_serialized_chars: int = 0
    candidate_record_count: int = 0
    compaction_applied: bool = False

    @property
    def required(self) -> bool:
        return self.status != "not_required"

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    def telemetry(
        self,
        *,
        disabled_read_tool_names: Iterable[object] = (),
    ) -> dict[str, Any]:
        """Return fingerprints and counts without exposing replayed provider content."""

        return {
            "schema": "keystone.decision_repair_evidence_replay.v1",
            "status": self.status,
            "mode": (
                "sanitized_tool_evidence_replay"
                if self.ready
                else "provider_read_replay_unavailable"
            ),
            "source": "initial_model_tool_outputs",
            "provider_calls_during_repair": 0,
            "source_tool_call_count": self.source_output_count,
            "replayed_output_count": self.replayed_output_count,
            "replayed_tool_names": list(self.replayed_tool_names),
            "disabled_read_tool_names": list(_ids(disabled_read_tool_names)),
            "candidate_ids": list(self.candidate_ids),
            "evidence_fingerprints": list(self.evidence_fingerprints),
            "aggregate_evidence_fingerprint": self.aggregate_fingerprint,
            "reason_code": self.reason_code,
            "source_serialized_chars": self.source_serialized_chars,
            "replay_serialized_chars": self.replay_serialized_chars,
            "candidate_record_count": self.candidate_record_count,
            "compaction_applied": self.compaction_applied,
        }


class AgentDecisionValidationError(RuntimeError):
    """A model decision failed validation; Python did not substitute a choice."""

    def __init__(self, outcome: DecisionValidatorOutcome) -> None:
        self.outcome = outcome
        super().__init__(
            "Agent-owned decision validation failed for "
            f"{outcome.decision_stage}: {outcome.reason_code or outcome.status}. "
            f"{outcome.feedback}"
        )


def pre_model_decision_context_telemetry(
    contract: AgentDecisionContract,
    *,
    model_input_text: str | None = None,
) -> tuple[dict[str, Any], DecisionValidatorOutcome | None]:
    """Describe and validate context made available before semantic selection.

    Tool-loop agents may discover candidates after the first model turn. Retrieved-
    context synthesis paths instead declare the exact bounded identities already placed
    in the typed model input. A required identity missing from that packet is a harness
    defect, not something the model can repair after the fact.
    """

    candidate_ids = _ids(contract.pre_model_candidate_ids)
    mandatory_ids = _ids(contract.mandatory_pre_model_context_ids)
    missing = sorted(set(mandatory_ids) - set(candidate_ids))
    visibility_check_required = bool(
        candidate_ids and contract.pre_model_context_source != "model_tool_loop"
        and contract.pre_model_source_visibility_required
    )
    model_visible_candidate_ids = (
        tuple(candidate_id for candidate_id in candidate_ids if candidate_id in model_input_text)
        if model_input_text is not None and visibility_check_required
        else ()
    )
    missing_from_model_input = (
        sorted(set(candidate_ids) - set(model_visible_candidate_ids))
        if model_input_text is not None and visibility_check_required
        else []
    )
    telemetry = {
        "schema": "keystone.pre_model_decision_context.v1",
        "route": contract.route,
        "decision_stage": contract.decision_stage,
        "context_source": contract.pre_model_context_source,
        "candidate_ids": list(candidate_ids),
        "mandatory_context_ids": list(mandatory_ids),
        "model_input_visibility_check_required": visibility_check_required,
        "source_visibility_required": contract.pre_model_source_visibility_required,
        "model_input_visibility_checked": model_input_text is not None,
        "model_visible_candidate_ids": list(model_visible_candidate_ids),
        "missing_model_visible_candidate_ids": missing_from_model_input,
        "context_complete": not missing and not missing_from_model_input,
        "missing_mandatory_context_ids": missing,
    }
    if not missing and not missing_from_model_input:
        return telemetry, None
    if missing_from_model_input:
        return (
            telemetry,
            DecisionValidatorOutcome(
                status="rejected",
                decision_stage=contract.decision_stage,
                candidate_count=min(len(candidate_ids), 100),
                selected_identity_in_candidate_set=None,
                reason_code="essential_context_not_model_visible",
                feedback=(
                    "Declared pre-model decision evidence was absent from the actual "
                    "model input: "
                    + ", ".join(missing_from_model_input)
                    + ". Fix typed context assembly before retrying the agent."
                ),
            ),
        )
    return (
        telemetry,
        DecisionValidatorOutcome(
            status="rejected",
            decision_stage=contract.decision_stage,
            candidate_count=min(len(candidate_ids), 100),
            selected_identity_in_candidate_set=None,
            reason_code="essential_context_not_supplied_to_model",
            feedback=(
                "Required decision context was absent from the bounded pre-model packet: "
                + ", ".join(missing)
                + ". Fix context assembly before retrying the agent."
            ),
        ),
    )


def validate_specialist_decision(
    output: Any,
    contract: AgentDecisionContract,
    *,
    verified_candidate_fingerprints: Iterable[object] = (),
    evidence_override: SpecialistDecisionEvidence | None = None,
) -> tuple[DecisionValidatorOutcome, SpecialistDecisionEvidence]:
    """Validate identity and assessment shape without choosing for the agent."""

    evidence = evidence_override or contract.evidence_resolver(output)
    decision = getattr(output, "decision", None)
    if not isinstance(decision, AgentDecisionRecord):
        return (
            _repair(
                contract,
                evidence,
                reason_code="decision_record_missing",
                feedback="Return the typed decision record for this specialist stage.",
            ),
            evidence,
        )
    explicitly_returned = "decision" in set(getattr(output, "model_fields_set", set()) or set())
    if not explicitly_returned:
        return (
            _repair(
                contract,
                evidence,
                decision=decision,
                reason_code="decision_record_defaulted",
                feedback=(
                    "The decision field was omitted and defaulted. The specialist must "
                    "return its own selection, assessments, reasoning, and limitations."
                ),
            ),
            evidence,
        )
    if decision.decision_owner != contract.decision_owner:
        return (
            _repair(
                contract,
                evidence,
                decision=decision,
                reason_code="decision_owner_mismatch",
                feedback=f"decision_owner must be {contract.decision_owner!r}.",
            ),
            evidence,
        )
    if decision.decision_stage != contract.decision_stage:
        return (
            _repair(
                contract,
                evidence,
                decision=decision,
                reason_code="decision_stage_mismatch",
                feedback=f"decision_stage must be {contract.decision_stage!r}.",
            ),
            evidence,
        )
    if contract.output_consistency_validator is not None:
        consistency_issue = contract.output_consistency_validator(output)
        if consistency_issue is not None:
            reason_code, feedback = consistency_issue
            return (
                _repair(
                    contract,
                    evidence,
                    decision=decision,
                    reason_code=reason_code,
                    feedback=feedback,
                ),
                evidence,
            )
    if evidence.identity_integrity_errors:
        return (
            _repair(
                contract,
                evidence,
                decision=decision,
                reason_code="candidate_identity_integrity_error",
                feedback=(
                    "Candidate IDs and provider aliases were ambiguous or mismatched. "
                    "Use each candidate's exact returned ID with only its own provider "
                    "identity; do not combine identities across candidates."
                ),
            ),
            evidence,
        )
    selected_ids = tuple(decision.selected_candidate_ids)
    candidate_aliases = _candidate_identity_aliases(evidence)
    canonical_selected_ids, unknown_selected_ids = _canonicalize_decision_identities(
        selected_ids,
        candidate_aliases,
    )
    candidate_set = set(evidence.candidate_ids)
    selected_set = set(canonical_selected_ids)
    required_set = {
        candidate_aliases.get(candidate_id, candidate_id)
        for candidate_id in evidence.required_selected_ids
    }
    mandatory_set = {
        candidate_aliases.get(candidate_id, candidate_id)
        for candidate_id in evidence.mandatory_selected_ids
    }
    unknown = sorted(unknown_selected_ids)
    if unknown:
        return (
            _repair(
                contract,
                evidence,
                decision=decision,
                reason_code="selected_identity_not_in_candidate_set",
                feedback=(
                    "Select only identities returned in the bounded candidate set. "
                    f"Unknown identities: {', '.join(unknown)}."
                ),
            ),
            evidence,
        )
    verified_fingerprints = set(_ids(verified_candidate_fingerprints))
    if selected_ids and verified_fingerprints:
        unverified = sorted(
            candidate_id
            for candidate_id in canonical_selected_ids
            if any(
                identity_fingerprint(provider_identity) not in verified_fingerprints
                for provider_identity in (
                    evidence.provider_identities_by_candidate.get(candidate_id) or (candidate_id,)
                )
            )
        )
        if unverified:
            return (
                _repair(
                    contract,
                    evidence,
                    decision=decision,
                    reason_code="selected_identity_not_in_provider_receipt",
                    feedback=(
                        "The selected identity was not returned by the verified provider "
                        "read receipt. Re-read bounded context or select only a returned ID."
                    ),
                ),
                evidence,
            )
    if len(selected_ids) > contract.max_selected:
        return (
            _repair(
                contract,
                evidence,
                decision=decision,
                reason_code="selected_identity_limit_exceeded",
                feedback=f"Select no more than {contract.max_selected} candidates.",
            ),
            evidence,
        )
    missing_mandatory_output = sorted(mandatory_set - required_set)
    if missing_mandatory_output:
        return (
            _repair(
                contract,
                evidence,
                decision=decision,
                reason_code="essential_context_not_declared_in_output",
                feedback=(
                    "Essential context must be included in the structured evidence IDs "
                    "used by the answer. Missing: " + ", ".join(missing_mandatory_output)
                ),
            ),
            evidence,
        )
    missing_mandatory_decision = sorted(mandatory_set - selected_set)
    if missing_mandatory_decision:
        return (
            _repair(
                contract,
                evidence,
                decision=decision,
                reason_code="essential_context_not_selected",
                feedback=(
                    "Essential context must be explicitly selected by the agent decision. "
                    "Missing: " + ", ".join(missing_mandatory_decision)
                ),
            ),
            evidence,
        )
    assessments, unknown_assessments, conflicting_alias_assessments = (
        _canonical_candidate_assessments(decision, candidate_aliases)
    )
    if unknown_assessments:
        return (
            _repair(
                contract,
                evidence,
                decision=decision,
                reason_code="candidate_assessment_identity_not_in_candidate_set",
                feedback=(
                    "Assess only identities returned in the bounded candidate set. "
                    "Unknown assessment identities: "
                    + ", ".join(unknown_assessments)
                    + "."
                ),
            ),
            evidence,
        )
    if conflicting_alias_assessments:
        return (
            _repair(
                contract,
                evidence,
                decision=decision,
                reason_code="candidate_identity_alias_assessment_conflict",
                feedback=(
                    "A candidate and one of its provider identity aliases received "
                    "conflicting dispositions. Assess the canonical candidate ID once: "
                    + ", ".join(conflicting_alias_assessments)
                    + "."
                ),
            ),
            evidence,
        )
    if decision.needs_more_context:
        if selected_ids or required_set:
            return (
                _repair(
                    contract,
                    evidence,
                    decision=decision,
                    reason_code="decision_conflicts_with_returned_selection",
                    feedback=(
                        "The decision or structured output already claims selected "
                        "identities, so it cannot also claim that more context is needed."
                    ),
                ),
                evidence,
            )
        if evidence.require_complete_assessments and len(evidence.candidate_ids) <= 20:
            missing = sorted(candidate_set - set(assessments))
            if missing:
                return (
                    _repair(
                        contract,
                        evidence,
                        decision=decision,
                        reason_code="candidate_assessments_incomplete",
                        feedback=(
                            "The bounded provider evidence must be assessed before "
                            "requesting more context. Assess every candidate and explain "
                            f"why it is insufficient. Missing: {', '.join(missing)}."
                        ),
                    ),
                    evidence,
                )
        selected_assessments = sorted(
            candidate_id
            for candidate_id, disposition in assessments.items()
            if disposition == "selected"
        )
        if selected_assessments:
            return (
                _repair(
                    contract,
                    evidence,
                    decision=decision,
                    reason_code="needs_more_context_with_selected_assessment",
                    feedback=(
                        "A decision that requests more context cannot mark a bounded "
                        "candidate as selected. Either select it in the decision or "
                        "explain why it remains plausible or excluded."
                    ),
                ),
                evidence,
            )
        if not decision.reasoning and not decision.limitations:
            return (
                _repair(
                    contract,
                    evidence,
                    decision=decision,
                    reason_code="unresolved_decision_without_explanation",
                    feedback="Explain why the bounded evidence is insufficient.",
                ),
                evidence,
            )
        return (_accepted(contract, evidence, decision), evidence)
    if evidence.selection_required and not selected_ids:
        return (
            _repair(
                contract,
                evidence,
                decision=decision,
                reason_code="required_selection_missing",
                feedback=(
                    "Select at least one bounded candidate or explicitly request more context."
                ),
            ),
            evidence,
        )
    if required_set and not required_set.issubset(selected_set):
        return (
            _repair(
                contract,
                evidence,
                decision=decision,
                reason_code="output_identity_not_owned_by_decision",
                feedback=(
                    "Every identity used by the structured answer must also be selected "
                    "in the agent decision."
                ),
            ),
            evidence,
        )
    if evidence.exact_required_selection and required_set != selected_set:
        return (
            _repair(
                contract,
                evidence,
                decision=decision,
                reason_code="decision_output_identity_mismatch",
                feedback=(
                    "The selected identities must exactly match identities used by the answer."
                ),
            ),
            evidence,
        )
    if evidence.require_complete_assessments and len(evidence.candidate_ids) <= 20:
        missing = sorted(candidate_set - set(assessments))
        if missing:
            return (
                _repair(
                    contract,
                    evidence,
                    decision=decision,
                    reason_code="candidate_assessments_incomplete",
                    feedback=(
                        "Assess every bounded candidate and explain excluded alternatives. "
                        f"Missing: {', '.join(missing)}."
                    ),
                ),
                evidence,
            )
    wrong_selected = sorted(
        candidate_id for candidate_id in selected_set if assessments.get(candidate_id) != "selected"
    )
    if wrong_selected:
        return (
            _repair(
                contract,
                evidence,
                decision=decision,
                reason_code="selected_candidate_assessment_mismatch",
                feedback="Every selected identity must have disposition='selected'.",
            ),
            evidence,
        )
    unresolved_alternatives = sorted(
        candidate_id
        for candidate_id, disposition in assessments.items()
        if candidate_id not in selected_set and disposition != "excluded"
    )
    if (
        selected_ids
        and unresolved_alternatives
        and not contract.allow_plausible_alternatives_after_selection
    ):
        return (
            _repair(
                contract,
                evidence,
                decision=decision,
                reason_code="excluded_alternatives_unresolved",
                feedback=(
                    "Once a terminal selection is made, every assessed alternative must "
                    "be marked excluded with a rationale."
                ),
            ),
            evidence,
        )
    if not decision.reasoning:
        return (
            _repair(
                contract,
                evidence,
                decision=decision,
                reason_code="decision_reasoning_missing",
                feedback="Explain why the selected candidates best satisfy the request.",
            ),
            evidence,
        )
    return (_accepted(contract, evidence, decision), evidence)


def decision_validation_telemetry(
    output: Any,
    contract: AgentDecisionContract,
    evidence: SpecialistDecisionEvidence,
    outcome: DecisionValidatorOutcome,
    *,
    attempt: int,
    tool_mode: str = "model_called",
) -> dict[str, Any]:
    """Return trace-safe proposed and validator events for one attempt."""

    decision = getattr(output, "decision", None)
    selected = (
        list(decision.selected_candidate_ids) if isinstance(decision, AgentDecisionRecord) else []
    )
    excluded = (
        [
            item.candidate_id
            for item in decision.candidate_assessments
            if item.disposition == "excluded"
        ]
        if isinstance(decision, AgentDecisionRecord)
        else []
    )
    candidate_assessments = (
        [item.model_dump(mode="json") for item in decision.candidate_assessments]
        if isinstance(decision, AgentDecisionRecord)
        else []
    )
    owner = (
        decision.decision_owner
        if isinstance(decision, AgentDecisionRecord)
        else contract.decision_owner
    )
    reasoning = decision.reasoning if isinstance(decision, AgentDecisionRecord) else ""
    limitations = list(decision.limitations) if isinstance(decision, AgentDecisionRecord) else []
    candidate_aliases = _candidate_identity_aliases(evidence)
    normalized_selected, _unknown_selected = _canonicalize_decision_identities(
        selected,
        candidate_aliases,
    )
    candidate_universe_payload = {
        "candidate_ids": list(evidence.candidate_ids),
        "provider_identities_by_candidate": {
            candidate_id: list(identities)
            for candidate_id, identities in sorted(
                evidence.provider_identities_by_candidate.items()
            )
        },
    }
    events = [
        DecisionTelemetryEvent(
            event_type="proposed" if attempt == 1 else "repair_proposed",
            decision_owner=owner,
            decision_stage=contract.decision_stage,
            attempt=attempt,
            candidate_ids=list(evidence.candidate_ids),
            selected_candidate_ids=selected,
            excluded_candidate_ids=excluded,
            validator_status="not_evaluated",
            tool_mode=tool_mode,
            reasoning=reasoning,
            limitations=limitations,
        ),
        DecisionTelemetryEvent(
            event_type="validator_result",
            decision_owner=owner,
            decision_stage=contract.decision_stage,
            attempt=attempt,
            candidate_ids=list(evidence.candidate_ids),
            selected_candidate_ids=selected,
            excluded_candidate_ids=excluded,
            validator_status=outcome.status,
            reason_code=outcome.reason_code,
            tool_mode=tool_mode,
            reasoning=reasoning,
            limitations=limitations,
        ),
    ]
    return {
        "schema": "keystone.agent_decision_telemetry.v1",
        "route": contract.route,
        "decision_owner": owner,
        "decision_stage": contract.decision_stage,
        "attempt": attempt,
        "tool_mode": tool_mode,
        "candidate_count": len(evidence.candidate_ids),
        "candidate_ids": list(evidence.candidate_ids),
        "candidate_universe_fingerprint": sha256(
            json.dumps(
                candidate_universe_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "selected_candidate_ids": selected,
        "normalized_selected_candidate_ids": list(normalized_selected),
        "candidate_identity_aliases_applied": selected != list(normalized_selected),
        "candidate_identity_integrity_errors": list(
            evidence.identity_integrity_errors
        ),
        "excluded_candidate_ids": excluded,
        "candidate_assessments": candidate_assessments,
        "reasoning": reasoning,
        "limitations": limitations,
        "validator_outcome": outcome.model_dump(mode="json"),
        "events": [event.model_dump(mode="json") for event in events],
    }


_PROVIDER_READ_TOOL_PREFIXES = (
    "capture_",
    "extract_",
    "fetch_",
    "get_",
    "inspect_",
    "list_",
    "load_",
    "query_",
    "read_",
    "render_",
    "resolve_",
    "retrieve_",
    "search_",
)
_REPLAY_DENIED_KEY_PARTS = (
    "access_token",
    "api_key",
    "attachment_bytes",
    "authorization",
    "base64",
    "binary",
    "cookie",
    "credential",
    "html_body",
    "mime_body",
    "oauth",
    "password",
    "raw_body",
    "raw_headers",
    "raw_message",
    "raw_mime",
    "refresh_token",
    "secret",
)
_REPLAY_IDENTITY_KEYS = frozenset(
    {
        "candidate_id",
        "candidate_ids",
        "collection_key",
        "document_id",
        "event_id",
        "file_id",
        "folder_id",
        "id",
        "item_key",
        "message_id",
        "record_id",
        "resource_id",
        "source_id",
        "spreadsheet_id",
        "thread_id",
        "threadid",
        "url",
    }
)
_REPLAY_CONTROL_KEYS = frozenset(
    {
        "attempt_count",
        "cache_hit",
        "completeness",
        "dry_run",
        "identity_fingerprints",
        "item_count",
        "live",
        "operation",
        "page_count",
        "provider",
        "provider_read",
        "provider_read_performed",
        "provider_system",
        "provider_write",
        "provider_write_performed",
        "requested_max_results",
        "schema",
        "schema_name",
        "send_enabled",
        "status",
        "success",
        "tool_name",
        "verification",
    }
)
_REPLAY_MAX_OUTPUTS = 12
_REPLAY_MAX_SERIALIZED_CHARS = 48_000
_REPLAY_COMPACTION_TIERS = (
    # Keep a useful candidate summary while removing repeated provider diagnostics.
    {"max_mapping_items": 30, "max_list_items": 16, "max_string_chars": 360},
    # A second, tighter projection remains preferable to abandoning a safe repair.
    {"max_mapping_items": 24, "max_list_items": 12, "max_string_chars": 180},
    {"max_mapping_items": 18, "max_list_items": 8, "max_string_chars": 120},
)


def is_provider_read_tool_name(tool_name: object) -> bool:
    """Return whether a tool name conventionally denotes a bounded read."""

    normalized = str(tool_name or "").strip().lower()
    return bool(
        normalized
        and (
            normalized.startswith(_PROVIDER_READ_TOOL_PREFIXES)
            or any(f"_{prefix}" in normalized for prefix in _PROVIDER_READ_TOOL_PREFIXES)
        )
    )


def build_decision_repair_evidence_replay(
    raw_result: Any,
    evidence: SpecialistDecisionEvidence,
    *,
    verified_candidate_fingerprints: Iterable[object] = (),
) -> DecisionRepairEvidenceReplay:
    """Build sanitized evidence from one completed model attempt for one repair."""

    return build_cumulative_decision_repair_evidence_replay(
        (raw_result,),
        evidence,
        verified_candidate_fingerprints=verified_candidate_fingerprints,
    )


def build_cumulative_decision_repair_evidence_replay(
    raw_results: Iterable[Any],
    evidence: SpecialistDecisionEvidence,
    *,
    verified_candidate_fingerprints: Iterable[object] = (),
    attested_read_outputs: Iterable[Mapping[str, Any]] = (),
) -> DecisionRepairEvidenceReplay:
    """Build one bounded replay from all completed provider/read attempts.

    The model already saw these tool outputs across its bounded attempts. Replaying only their
    bounded, credential-free projection lets a fresh repair attempt retain
    the cumulative candidate semantics without repeating provider reads. Duplicate
    call/output pairs are collapsed before applying the shared replay limits. If that projection
    cannot preserve every candidate plus descriptive evidence, repair fails
    closed instead of falling back to an ID-only prompt.
    """

    from keystone_agents.receipts.mutations import operation_is_mutation
    from keystone_agents.runtime.tool_execution import (
        sdk_tool_execution_records,
        tool_result_succeeded,
    )

    read_records: list[tuple[str, str, Any]] = []
    seen_read_outputs: set[tuple[str, str, str]] = set()
    for raw_result in raw_results:
        records = tuple(
            record
            for record in sdk_tool_execution_records(raw_result)
            if record.succeeded
        )
        output_by_call_id = _tool_outputs_by_call_id(raw_result)
        for record in records:
            output = output_by_call_id.get(record.call_id)
            if output is None:
                continue
            if not (
                is_provider_read_tool_name(record.tool_name)
                or _output_reports_provider_read(output)
            ):
                continue
            parsed = _parse_replay_output(output)
            output_fingerprint = sha256(
                json.dumps(
                    parsed,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            dedupe_key = (record.call_id, record.tool_name, output_fingerprint)
            if dedupe_key in seen_read_outputs:
                continue
            seen_read_outputs.add(dedupe_key)
            read_records.append((record.call_id, record.tool_name, output))
    # The runner may attest exact call arguments and returned data even when an
    # SDK final-output error prevented a RunResult. Keep these as tool evidence,
    # never as fabricated SDK results or additional model/provider usage.
    for entry in attested_read_outputs:
        tool_name = str(entry.get("tool_name") or "")
        output = entry.get("output")
        if (
            operation_is_mutation(tool_name) or not is_provider_read_tool_name(tool_name)
            or not tool_result_succeeded(output)
        ):
            continue
        fingerprint = sha256(json.dumps(
            entry, ensure_ascii=True, sort_keys=True, default=str,
        ).encode("utf-8")).hexdigest()
        key = ("attested", tool_name, fingerprint)
        if key not in seen_read_outputs:
            seen_read_outputs.add(key)
            read_records.append((f"attested:{fingerprint}", tool_name, output))
    if not read_records:
        return DecisionRepairEvidenceReplay(status="not_required")

    verified_fingerprints = set(_ids(verified_candidate_fingerprints))
    replay_candidate_ids = tuple(
        candidate_id
        for candidate_id in evidence.candidate_ids
        if any(
            identity_fingerprint(identity) in verified_fingerprints
            for identity in (
                candidate_id,
                *evidence.provider_identities_by_candidate.get(candidate_id, ()),
            )
        )
    )
    replay_protected_identities = _ids(
        identity
        for candidate_id in replay_candidate_ids
        for identity in (
            candidate_id,
            *evidence.provider_identities_by_candidate.get(candidate_id, ()),
        )
    )
    if evidence.candidate_ids and not replay_candidate_ids:
        return _unsafe_replay(
            evidence,
            replayed_names=[],
            reason_code="decision_repair_verified_candidate_universe_unavailable",
            feedback=(
                "No model candidate identity could be bound to the completed provider "
                "receipts, so a safe agent-owned repair cannot continue."
            ),
        )

    replay_entries: list[dict[str, Any]] = []
    fingerprints: list[str] = []
    replayed_names: list[str] = []
    for _call_id, tool_name, output in read_records[:_REPLAY_MAX_OUTPUTS]:
        parsed = _parse_replay_output(output)
        if not isinstance(parsed, Mapping | list | tuple):
            return _unsafe_replay(
                evidence,
                replayed_names=[*replayed_names, tool_name],
                reason_code="decision_repair_evidence_not_structured",
                feedback=(
                    "A completed provider/read tool returned evidence that could not be "
                    "safely replayed as bounded structured data."
                ),
            )
        sanitized = _sanitize_replay_value(
            parsed,
            protected_strings=replay_protected_identities,
        )
        if not isinstance(sanitized, dict | list) or not sanitized:
            return _unsafe_replay(
                evidence,
                replayed_names=[*replayed_names, tool_name],
                reason_code="decision_repair_evidence_empty_after_sanitization",
                feedback=(
                    "Provider/read evidence contained no safe bounded fields after "
                    "credential and raw-content removal."
                ),
            )
        canonical_output = json.dumps(
            sanitized,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprints.append(sha256(canonical_output.encode("utf-8")).hexdigest())
        replayed_names.append(tool_name)
        replay_entries.append({"tool_name": tool_name, "output": sanitized})

    if len(read_records) > _REPLAY_MAX_OUTPUTS:
        return _unsafe_replay(
            evidence,
            replayed_names=replayed_names,
            reason_code="decision_repair_evidence_output_limit_exceeded",
            feedback="Provider/read evidence exceeded the bounded replay output limit.",
        )

    serialized = json.dumps(
        replay_entries,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    source_serialized_chars = len(serialized)
    source_output_count = len(replay_entries)
    candidate_record_count = sum(
        len(_lowest_candidate_mappings(entry.get("output"), replay_protected_identities))
        for entry in replay_entries
    )
    compaction_applied = source_serialized_chars > _REPLAY_MAX_SERIALIZED_CHARS
    if compaction_applied:
        replay_entries, serialized = _compact_replay_entries(
            replay_entries,
            protected_strings=replay_protected_identities,
        )
    if len(serialized) > _REPLAY_MAX_SERIALIZED_CHARS:
        return _unsafe_replay(
            evidence,
            replayed_names=replayed_names,
            reason_code="decision_repair_evidence_size_limit_exceeded",
            feedback="Provider/read evidence exceeded the bounded replay size limit.",
            source_output_count=source_output_count,
            replayed_output_count=len(replay_entries),
            source_serialized_chars=source_serialized_chars,
            replay_serialized_chars=len(serialized),
            candidate_record_count=candidate_record_count,
            compaction_applied=compaction_applied,
        )
    fingerprints = [
        sha256(
            json.dumps(
                entry.get("output"),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        for entry in replay_entries
    ]
    missing_candidates = [
        candidate_id
        for candidate_id in replay_candidate_ids
        if not any(
            identity in serialized
            for identity in (
                candidate_id,
                *evidence.provider_identities_by_candidate.get(candidate_id, ()),
            )
        )
    ]
    if missing_candidates:
        return _unsafe_replay(
            evidence,
            replayed_names=replayed_names,
            reason_code="decision_repair_candidate_evidence_incomplete",
            feedback=(
                "The safe replay projection did not preserve every bounded candidate "
                "identity required for agent-owned repair."
            ),
        )
    if not _has_descriptive_replay_evidence(replay_entries):
        return _unsafe_replay(
            evidence,
            replayed_names=replayed_names,
            reason_code="decision_repair_evidence_not_descriptive",
            feedback=(
                "The safe replay projection contained identities or control metadata only. "
                "Agent-owned repair requires candidate evidence, not an ID-only prompt."
            ),
        )
    candidates_without_descriptive_evidence = [
        candidate_id
        for candidate_id in replay_candidate_ids
        if not _has_descriptive_candidate_evidence(
            replay_entries,
            identities=(
                candidate_id,
                *evidence.provider_identities_by_candidate.get(candidate_id, ()),
            ),
        )
    ]
    if candidates_without_descriptive_evidence:
        return _unsafe_replay(
            evidence,
            replayed_names=replayed_names,
            reason_code="decision_repair_candidate_evidence_not_descriptive",
            feedback=(
                "At least one candidate in the safe replay had an identity but no "
                "candidate-specific descriptive evidence."
            ),
        )
    aggregate = sha256("|".join(fingerprints).encode("utf-8")).hexdigest()
    prompt_context = (
        "\n\nSanitized decision evidence replay (provider-read-free):\n"
        "The successful provider/read tool outputs below were already returned to the "
        "model during the first attempt. Treat this bounded replay as authoritative. "
        "Do not repeat provider reads. Reassess the candidates from their evidence and "
        "correct your own decision.\n"
        f"Replayed tool evidence: {serialized}"
    )
    return DecisionRepairEvidenceReplay(
        status="ready",
        prompt_context=prompt_context,
        replayed_tool_names=_ids(replayed_names),
        evidence_fingerprints=tuple(fingerprints),
        aggregate_fingerprint=aggregate,
        candidate_ids=replay_candidate_ids,
        source_output_count=source_output_count,
        replayed_output_count=len(replay_entries),
        source_serialized_chars=source_serialized_chars,
        replay_serialized_chars=len(serialized),
        candidate_record_count=candidate_record_count,
        compaction_applied=compaction_applied,
    )


def decision_repair_prompt(
    contract: AgentDecisionContract,
    evidence: SpecialistDecisionEvidence,
    outcome: DecisionValidatorOutcome,
    *,
    evidence_replay: DecisionRepairEvidenceReplay | None = None,
) -> str:
    """Bounded feedback for one model repair; it never supplies a replacement choice."""

    alternative_check = (
        "after a terminal selection, non-selected assessed candidates may remain "
        "disposition='plausible' only when the reasoning explains why the selected "
        "route is still the best owner; otherwise mark them disposition='excluded'"
        if contract.allow_plausible_alternatives_after_selection
        else "after a terminal selection give every assessed non-selected candidate "
        "disposition='excluded'"
    )
    orchestrator_workflow_check = ""
    unresolved_choice_check = (
        "; otherwise return no selection and set needs_more_context=true; "
        "include reasoning and limitations."
        if evidence.selection_required
        else "; a supported no-action decision may return no selection with "
        "needs_more_context=false and evidence-backed exclusions and rationale. "
        "Set needs_more_context=true only when missing required evidence prevents "
        "a responsible decision; identify that gap and include limitations."
    )
    if contract.route == "orchestrator":
        unresolved_choice_check = (
            "; the main decision always has needs_more_context=false. If operator "
            "input is missing, select clarification, use workflow=[], and put the "
            "question in clarification_request. Preserve genuine questions and "
            "include reasoning and limitations."
        )
        selected_routes = ", ".join(evidence.required_selected_ids) or "none"
        orchestrator_workflow_check = (
            "\n- orchestrator_workflow_contract: workflow is an ordered list of "
            "registered agent route IDs, never task actions, tool names, or prose. "
            f"The currently selected registered route IDs are: {selected_routes}. "
            "For one selected owner, return workflow=[] or workflow=[that exact route "
            "ID]; do not add query, compare, read, draft, summarize, or other action "
            "steps."
        )
    prompt = (
        "\n\nDeterministic decision validator feedback (bounded repair attempt):\n"
        f"- route: {contract.route}\n"
        f"- decision_stage: {contract.decision_stage}\n"
        f"- reason_code: {outcome.reason_code}\n"
        f"- feedback: {outcome.feedback}\n"
        "- permitted_candidate_ids: "
        + (", ".join(evidence.candidate_ids) if evidence.candidate_ids else "none")
        + orchestrator_workflow_check
        + "\n- complete_decision_checklist: keep route, target, workflow, and decision "
        "identities consistent; select only permitted candidate IDs; give every selected "
        "candidate disposition='selected'; "
        + alternative_check
        + unresolved_choice_check
        + " Provider identities such as source URLs are evidence aliases, not separate "
        "candidates; when a permitted canonical candidate ID is present, assess that ID "
        "once and do not add a second assessment for its URL."
        "\nReturn the complete structured output again. Preserve the original request, "
        "evidence, permissions, and no-send/write boundaries. Correct your own decision; "
        "the validator will not select a candidate for you."
    )
    if evidence_replay is not None and evidence_replay.ready:
        prompt += evidence_replay.prompt_context
    return prompt


def model_tool_output_payloads(
    raw_results: Iterable[Any],
    *,
    tool_names: Iterable[object],
) -> tuple[Any, ...]:
    """Return parsed outputs for successful named model-called tools in call order."""

    from keystone_agents.runtime.tool_execution import sdk_tool_execution_records

    permitted = set(_ids(tool_names))
    payloads: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for raw_result in raw_results:
        output_by_call_id = _tool_outputs_by_call_id(raw_result)
        for record in sdk_tool_execution_records(raw_result):
            if not record.succeeded or record.tool_name not in permitted:
                continue
            output = output_by_call_id.get(record.call_id)
            if output is None:
                continue
            parsed = _parse_replay_output(output)
            if parsed is None:
                continue
            fingerprint = sha256(
                json.dumps(
                    parsed,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            key = (record.tool_name, fingerprint)
            if key in seen:
                continue
            seen.add(key)
            payloads.append(parsed)
    return tuple(payloads)


def decision_contract_prompt(contract: AgentDecisionContract) -> str:
    """State the validated decision owner and stage before the first model turn."""

    ownership_note = (
        "Do not relabel this specialist decision as Orchestrator or Chief of Staff."
        if contract.decision_owner == "specialist_agent"
        else f"This decision belongs to {contract.decision_owner}; do not use another owner."
    )
    candidate_note = (
        " The complete permitted candidate universe for this run is: "
        + ", ".join(contract.pre_model_candidate_ids)
        + ". Assess only these identities."
        if contract.pre_model_candidate_ids
        else ""
    )
    return (
        "\n\nAgent-owned decision contract (applies to this run):\n"
        f"- decision_owner: {contract.decision_owner}\n"
        f"- decision_stage: {contract.decision_stage}\n"
        "Return those exact values in the structured decision record. "
        f"{ownership_note} Select and assess "
        "only identities visible in supplied context or returned by this run's tools; "
        "Python validates the decision but will not choose a replacement."
        + candidate_note
    )


def _unsafe_replay(
    evidence: SpecialistDecisionEvidence,
    *,
    replayed_names: Iterable[object],
    reason_code: str,
    feedback: str,
    source_output_count: int = 0,
    replayed_output_count: int = 0,
    source_serialized_chars: int = 0,
    replay_serialized_chars: int = 0,
    candidate_record_count: int = 0,
    compaction_applied: bool = False,
) -> DecisionRepairEvidenceReplay:
    return DecisionRepairEvidenceReplay(
        status="unsafe",
        replayed_tool_names=_ids(replayed_names),
        candidate_ids=evidence.candidate_ids,
        reason_code=reason_code,
        feedback=feedback,
        source_output_count=source_output_count,
        replayed_output_count=replayed_output_count,
        source_serialized_chars=source_serialized_chars,
        replay_serialized_chars=replay_serialized_chars,
        candidate_record_count=candidate_record_count,
        compaction_applied=compaction_applied,
    )


def _tool_outputs_by_call_id(raw_result: Any) -> dict[str, Any]:
    items = list(getattr(raw_result, "new_items", []) or [])
    if isinstance(raw_result, Mapping):
        items = list(raw_result.get("new_items") or raw_result.get("items") or [])
    outputs: dict[str, Any] = {}
    for item in items:
        item_type = _run_item_value(item, "type", "item_type").lower()
        if "output" not in item_type or not (
            "tool" in item_type or "function" in item_type
        ):
            continue
        call_id = _run_item_value(item, "call_id", "id", nested=True)
        if call_id:
            outputs[call_id] = (
                item.get("output")
                if isinstance(item, Mapping)
                else getattr(item, "output", None)
            )
    return outputs


def _run_item_value(item: Any, *names: str, nested: bool = False) -> str:
    if isinstance(item, Mapping):
        for name in names:
            if item.get(name):
                return str(item[name])
        raw_item = item.get("raw_item") if nested else None
        if isinstance(raw_item, Mapping):
            for name in names:
                if raw_item.get(name):
                    return str(raw_item[name])
        return ""
    for name in names:
        value = getattr(item, name, None)
        if value:
            return str(value)
    raw_item = getattr(item, "raw_item", None) if nested else None
    for name in names:
        value = getattr(raw_item, name, None)
        if value:
            return str(value)
    return ""


def _parse_replay_output(output: Any) -> Any:
    if hasattr(output, "model_dump"):
        return output.model_dump(mode="json")
    if not isinstance(output, str):
        return output
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


def _output_reports_provider_read(output: Any) -> bool:
    parsed = _parse_replay_output(output)
    if isinstance(parsed, Mapping):
        if parsed.get("provider_read") is True or parsed.get("provider_read_performed") is True:
            return True
        operation = str(
            parsed.get("operation") or parsed.get("action") or ""
        ).strip().lower()
        if operation in {"fetch", "get", "inspect", "list", "query", "read", "search"}:
            return True
    return False


def _sanitize_replay_value(
    value: Any,
    *,
    depth: int = 0,
    max_mapping_items: int = 40,
    max_list_items: int = 25,
    max_string_chars: int = 1_500,
    protected_strings: Iterable[object] = (),
) -> Any:
    if depth >= 6:
        return "[bounded]"
    protected = _ids(protected_strings)
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        allowed_items = [
            (raw_key, item)
            for raw_key, item in value.items()
            if not any(
                part
                in str(raw_key).lower().replace("-", "_")
                for part in _REPLAY_DENIED_KEY_PARTS
            )
        ]
        items = _bounded_replay_items(
            allowed_items,
            limit=max_mapping_items,
            protected_strings=protected,
            value_getter=lambda pair: pair,
        )
        for raw_key, item in items:
            key = str(raw_key)[:120]
            sanitized[key] = _sanitize_replay_value(
                item,
                depth=depth + 1,
                max_mapping_items=max_mapping_items,
                max_list_items=max_list_items,
                max_string_chars=max_string_chars,
                protected_strings=protected,
            )
        return sanitized
    if isinstance(value, list | tuple):
        items = _bounded_replay_items(
            list(value),
            limit=max_list_items,
            protected_strings=protected,
        )
        return [
            _sanitize_replay_value(
                item,
                depth=depth + 1,
                max_mapping_items=max_mapping_items,
                max_list_items=max_list_items,
                max_string_chars=max_string_chars,
                protected_strings=protected,
            )
            for item in items
        ]
    if isinstance(value, str):
        cleaned = value.replace("\x00", " ")
        if cleaned in protected:
            return cleaned[:500]
        return cleaned[:max_string_chars]
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)[:500]


def _compact_replay_entries(
    replay_entries: list[dict[str, Any]],
    *,
    protected_strings: Iterable[object],
) -> tuple[list[dict[str, Any]], str]:
    """Shrink repeated provider evidence without dropping candidate-bearing items."""

    protected = _ids(protected_strings)
    candidate_entries = _candidate_focused_replay_entries(
        replay_entries,
        protected_strings=protected,
    )
    compacted = candidate_entries or replay_entries
    serialized = json.dumps(
        compacted,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    for tier in _REPLAY_COMPACTION_TIERS:
        projected = _sanitize_replay_value(
            compacted,
            max_mapping_items=tier["max_mapping_items"],
            max_list_items=tier["max_list_items"],
            max_string_chars=tier["max_string_chars"],
            protected_strings=protected,
        )
        if not isinstance(projected, list):
            break
        compacted = projected
        serialized = json.dumps(
            compacted,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(serialized) <= _REPLAY_MAX_SERIALIZED_CHARS:
            break
    return compacted, serialized


def _candidate_focused_replay_entries(
    replay_entries: list[dict[str, Any]],
    *,
    protected_strings: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Keep candidate records while dropping unrelated multi-query diagnostics."""

    focused: list[dict[str, Any]] = []
    for entry in replay_entries:
        output = entry.get("output")
        matches = _lowest_candidate_mappings(output, protected_strings)
        if not matches:
            continue
        if len(matches) > 20:
            # Do not silently discard a large set of distinct candidate-bearing
            # records merely to make a repair prompt fit.
            return []
        provider_metadata: dict[str, Any] = {}
        if isinstance(output, Mapping):
            for key, value in output.items():
                normalized_key = str(key).lower().replace("-", "_")
                if normalized_key not in _REPLAY_CONTROL_KEYS:
                    continue
                if isinstance(value, str | bool | int | float) or value is None:
                    provider_metadata[str(key)[:120]] = _sanitize_replay_value(
                        value,
                        max_string_chars=180,
                    )
        focused.append(
            {
                "tool_name": str(entry.get("tool_name") or ""),
                "output": {
                    **provider_metadata,
                    "candidate_evidence": [
                        _sanitize_replay_value(
                            match,
                            max_mapping_items=24,
                            max_list_items=8,
                            max_string_chars=360,
                            protected_strings=protected_strings,
                        )
                        for match in matches
                    ],
                },
            }
        )
    return focused


def _lowest_candidate_mappings(
    value: Any,
    protected_strings: tuple[str, ...],
    *,
    depth: int = 0,
) -> list[Mapping[str, Any]]:
    """Find the smallest structured records carrying verified candidate identities."""

    if depth >= 8:
        return []
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        child_matches: list[Mapping[str, Any]] = []
        for raw_key, item in value.items():
            normalized_key = str(raw_key).lower().replace("-", "_")
            if any(part in normalized_key for part in _REPLAY_DENIED_KEY_PARTS):
                continue
            if isinstance(item, Mapping | list | tuple):
                child_matches.extend(
                    _lowest_candidate_mappings(
                        item,
                        protected_strings,
                        depth=depth + 1,
                    )
                )
        if child_matches:
            return child_matches
        if _mapping_directly_contains_identity(value, protected_strings):
            return [value]
        return []
    if isinstance(value, list | tuple):
        matches: list[Mapping[str, Any]] = []
        for item in value:
            matches.extend(
                _lowest_candidate_mappings(
                    item,
                    protected_strings,
                    depth=depth + 1,
                )
            )
        return matches
    return []


def _bounded_replay_items(
    items: list[Any],
    *,
    limit: int,
    protected_strings: tuple[str, ...],
    value_getter: Callable[[Any], Any] | None = None,
) -> list[Any]:
    """Bound a collection while retaining every candidate-bearing member."""

    if len(items) <= limit:
        return items
    extract = value_getter or (lambda item: item)
    protected_items = [
        item
        for item in items
        if _contains_replay_identity(extract(item), protected_strings)
    ]
    protected_ids = {id(item) for item in protected_items}
    remaining = [item for item in items if id(item) not in protected_ids]
    return [*protected_items, *remaining[: max(0, limit - len(protected_items))]]


def _contains_replay_identity(
    value: Any,
    protected_strings: tuple[str, ...],
    *,
    depth: int = 0,
) -> bool:
    if not protected_strings or depth >= 8:
        return False
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return any(
            _contains_replay_identity(key, protected_strings, depth=depth + 1)
            or _contains_replay_identity(item, protected_strings, depth=depth + 1)
            for key, item in value.items()
        )
    if isinstance(value, list | tuple):
        return any(
            _contains_replay_identity(item, protected_strings, depth=depth + 1)
            for item in value
        )
    if not isinstance(value, str):
        return False
    normalized = " ".join(value.split())
    return any(normalized == identity for identity in protected_strings)


def _has_descriptive_candidate_evidence(
    value: Any,
    *,
    identities: Iterable[object],
) -> bool:
    """Require each repair candidate to have evidence in its own bounded record."""

    normalized_identities = _ids(identities)
    if not normalized_identities:
        return False
    if isinstance(value, Mapping):
        if _mapping_directly_contains_identity(value, normalized_identities):
            return _has_descriptive_replay_evidence(value)
        return any(
            _has_descriptive_candidate_evidence(item, identities=normalized_identities)
            for item in value.values()
        )
    if isinstance(value, list | tuple):
        return any(
            _has_descriptive_candidate_evidence(item, identities=normalized_identities)
            for item in value
        )
    return False


def _mapping_directly_contains_identity(
    value: Mapping[Any, Any],
    identities: tuple[str, ...],
) -> bool:
    for raw_key, item in value.items():
        if " ".join(str(raw_key).split()) in identities:
            return True
        if isinstance(item, str) and " ".join(item.split()) in identities:
            return True
    return False


def _has_descriptive_replay_evidence(value: Any, *, key: str = "") -> bool:
    if isinstance(value, Mapping):
        return any(
            _has_descriptive_replay_evidence(item, key=str(raw_key).lower())
            for raw_key, item in value.items()
        )
    if isinstance(value, list | tuple):
        return any(_has_descriptive_replay_evidence(item, key=key) for item in value)
    normalized_key = key.replace("-", "_")
    if normalized_key in _REPLAY_IDENTITY_KEYS or normalized_key in _REPLAY_CONTROL_KEYS:
        return False
    if isinstance(value, str):
        return len(value.strip()) >= 3
    return value is not None and normalized_key not in _REPLAY_CONTROL_KEYS


def verified_candidate_fingerprints_from_receipts(
    receipts: Iterable[Any],
) -> tuple[str, ...]:
    """Collect candidate fingerprints and singular IDs from bounded read receipts."""

    fingerprints: list[str] = []
    identity_keys = (
        "record_id",
        "file_id",
        "folder_id",
        "document_id",
        "spreadsheet_id",
        "item_key",
        "collection_key",
        "message_id",
        "thread_id",
        "event_id",
    )
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            continue
        raw = receipt.get("identity_fingerprints") or []
        values = raw if isinstance(raw, list | tuple | set) else [raw]
        fingerprints.extend(str(value or "").strip() for value in values)
        fingerprints.extend(
            identity_fingerprint(receipt.get(key)) for key in identity_keys if receipt.get(key)
        )
    return _ids(fingerprints)


def _candidate_identity_aliases(
    evidence: SpecialistDecisionEvidence,
) -> dict[str, str]:
    aliases = {candidate_id: candidate_id for candidate_id in evidence.candidate_ids}
    proposed: dict[str, set[str]] = {}
    for candidate_id, identities in evidence.provider_identities_by_candidate.items():
        if candidate_id not in aliases:
            continue
        for identity in identities:
            proposed.setdefault(identity, set()).add(candidate_id)
    for identity, candidates in proposed.items():
        if identity in aliases and aliases[identity] not in candidates:
            continue
        if len(candidates) == 1:
            aliases[identity] = next(iter(candidates))
    return aliases


def _canonicalize_decision_identities(
    identities: Iterable[object],
    aliases: Mapping[str, str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    canonical: list[str] = []
    unknown: list[str] = []
    for identity in _ids(identities):
        resolved = aliases.get(identity)
        if resolved is None:
            unknown.append(identity)
            continue
        canonical.append(resolved)
    return _ids(canonical), _ids(unknown)


def _canonical_candidate_assessments(
    decision: AgentDecisionRecord,
    aliases: Mapping[str, str],
) -> tuple[dict[str, str], list[str], list[str]]:
    assessments: dict[str, str] = {}
    source_is_canonical: dict[str, bool] = {}
    unknown: list[str] = []
    conflicts: list[str] = []
    for item in decision.candidate_assessments:
        raw_id = str(item.candidate_id or "").strip()
        canonical_id = aliases.get(raw_id)
        if canonical_id is None:
            unknown.append(raw_id)
            continue
        exact = raw_id == canonical_id
        prior = assessments.get(canonical_id)
        prior_exact = source_is_canonical.get(canonical_id, False)
        if prior is not None and prior != item.disposition:
            if exact and not prior_exact:
                assessments[canonical_id] = item.disposition
                source_is_canonical[canonical_id] = True
                continue
            if prior_exact and not exact:
                continue
            conflicts.append(canonical_id)
            continue
        if prior is None or exact:
            assessments[canonical_id] = item.disposition
            source_is_canonical[canonical_id] = exact or prior_exact
    return assessments, list(_ids(unknown)), list(_ids(conflicts))


def _repair(
    contract: AgentDecisionContract,
    evidence: SpecialistDecisionEvidence,
    *,
    reason_code: str,
    feedback: str,
    decision: AgentDecisionRecord | None = None,
) -> DecisionValidatorOutcome:
    return DecisionValidatorOutcome(
        status="repair_required",
        decision_stage=contract.decision_stage,
        selected_candidate_id=(decision.selected_candidate_id if decision else ""),
        candidate_count=min(len(evidence.candidate_ids), 100),
        selected_identity_in_candidate_set=(
            set(decision.selected_candidate_ids).issubset(set(evidence.candidate_ids))
            if decision is not None
            else None
        ),
        reason_code=reason_code,
        feedback=feedback,
    )


def _accepted(
    contract: AgentDecisionContract,
    evidence: SpecialistDecisionEvidence,
    decision: AgentDecisionRecord,
) -> DecisionValidatorOutcome:
    return DecisionValidatorOutcome(
        status="accepted",
        decision_stage=contract.decision_stage,
        selected_candidate_id=decision.selected_candidate_id,
        candidate_count=min(len(evidence.candidate_ids), 100),
        selected_identity_in_candidate_set=True,
        reason_code="decision_validated",
        feedback="Agent selection and assessments are bound to the verified candidate set.",
    )


__all__ = [
    "AgentDecisionContract",
    "AgentDecisionValidationError",
    "DecisionRepairEvidenceReplay",
    "SpecialistDecisionEvidence",
    "bind_authoritative_tool_evidence",
    "build_decision_repair_evidence_replay",
    "build_cumulative_decision_repair_evidence_replay",
    "decision_contract_prompt",
    "decision_repair_prompt",
    "decision_validation_telemetry",
    "is_provider_read_tool_name",
    "model_tool_output_payloads",
    "pre_model_decision_context_telemetry",
    "verified_candidate_fingerprints_from_receipts",
    "validate_specialist_decision",
]
