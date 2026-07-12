"""ANU-61 and ANU-125 scorecard aggregation over saved pilot observations."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from keystone_agents.controlled_pilot import (
    TRUSTED_RUNTIME_PREREQUISITES,
    ControlledPilotObservation,
    assess_controlled_pilot_observation,
    controlled_pilot_cases,
    controlled_pilot_ready,
)
from keystone_agents.differentiation_matrix import (
    DifferentiationObservation,
    compare_differentiation_observations,
)


def build_controlled_pilot_scorecard(
    *,
    trusted_runtime_rows: dict[str, str],
    observations: Iterable[ControlledPilotObservation],
    baseline_observations: Iterable[DifferentiationObservation] = (),
) -> dict[str, Any]:
    """Build one complete scorecard without inferring missing evidence."""

    observation_by_case = _unique_by_case(observations, label="KBA pilot")
    baseline_by_case = _unique_baselines(baseline_observations)
    case_rows: list[dict[str, Any]] = []
    observed_requests = 0
    observed_cost = 0.0
    observed_latency = 0

    for case in controlled_pilot_cases():
        observation = observation_by_case.get(case.case_id)
        if observation is None:
            case_rows.append(
                {
                    "case_id": case.case_id,
                    "status": "missing",
                    "failed_checks": ["observation_missing"],
                    "comparison_status": "pending_kba_observation",
                }
            )
            continue

        assessment = assess_controlled_pilot_observation(observation)
        observed_requests += observation.openai_requests
        observed_cost += observation.estimated_cost_usd
        observed_latency += observation.latency_ms
        baseline = baseline_by_case.get(case.case_id)
        comparison_status = "pending_baseline"
        improvements: list[str] = []
        regressions: list[str] = []
        if baseline is not None:
            comparison = compare_differentiation_observations(
                _as_differentiation_observation(observation, assessment.status == "pass"),
                baseline,
            )
            comparison_status = comparison.status
            improvements = list(comparison.improvements)
            regressions = list(comparison.regressions)
        case_rows.append(
            {
                "case_id": case.case_id,
                "status": assessment.status,
                "failed_checks": list(assessment.failed_checks),
                "backend": case.backend,
                "entry_owner": case.expected_entry_owner,
                "openai_requests": observation.openai_requests,
                "estimated_cost_usd": observation.estimated_cost_usd,
                "latency_ms": observation.latency_ms,
                "visible_source_count": observation.visible_source_count,
                "context_reentry_fields": observation.context_reentry_fields,
                "manual_provider_ids": observation.manual_provider_ids,
                "approval_round_trips": observation.approval_round_trips,
                "provider_writes": observation.provider_writes,
                "evidence_refs": list(observation.evidence_refs),
                "comparison_status": comparison_status,
                "improvements": improvements,
                "regressions": regressions,
            }
        )

    trusted_ready = controlled_pilot_ready(trusted_runtime_rows)
    all_observed = len(observation_by_case) == len(controlled_pilot_cases())
    all_cases_pass = all(row["status"] == "pass" for row in case_rows)
    pilot_status = (
        "pass"
        if trusted_ready and all_observed and all_cases_pass
        else "fail"
        if any(row["status"] == "fail" for row in case_rows)
        else "pending"
    )
    return {
        "schema": "keystone.controlled_pilot.scorecard.v1",
        "pilot_status": pilot_status,
        "trusted_runtime_ready": trusted_ready,
        "trusted_runtime_rows": {
            row: str(trusted_runtime_rows.get(row) or "missing")
            for row in TRUSTED_RUNTIME_PREREQUISITES
        },
        "case_count": len(case_rows),
        "observed_case_count": len(observation_by_case),
        "passing_case_count": sum(row["status"] == "pass" for row in case_rows),
        "totals": {
            "openai_requests": observed_requests,
            "estimated_cost_usd": round(observed_cost, 8),
            "latency_ms": observed_latency,
        },
        "cases": case_rows,
        "baseline_observation_count": len(baseline_by_case),
        "comparative_claims_supported": sum(
            row.get("comparison_status") == "supported" for row in case_rows
        ),
    }


def controlled_pilot_observation_from_dict(payload: dict[str, Any]) -> ControlledPilotObservation:
    values = dict(payload)
    values["evidence_refs"] = tuple(values.get("evidence_refs") or ())
    return ControlledPilotObservation(**values)


def differentiation_observation_from_dict(payload: dict[str, Any]) -> DifferentiationObservation:
    values = dict(payload)
    values["evidence_refs"] = tuple(values.get("evidence_refs") or ())
    return DifferentiationObservation(**values)


def _unique_by_case(
    observations: Iterable[ControlledPilotObservation],
    *,
    label: str,
) -> dict[str, ControlledPilotObservation]:
    indexed: dict[str, ControlledPilotObservation] = {}
    known_cases = {case.case_id for case in controlled_pilot_cases()}
    for observation in observations:
        if observation.case_id not in known_cases:
            raise ValueError(f"Unknown {label} case: {observation.case_id}.")
        if observation.case_id in indexed:
            raise ValueError(f"Duplicate {label} observation for {observation.case_id}.")
        indexed[observation.case_id] = observation
    return indexed


def _unique_baselines(
    observations: Iterable[DifferentiationObservation],
) -> dict[str, DifferentiationObservation]:
    indexed: dict[str, DifferentiationObservation] = {}
    known_cases = {case.case_id for case in controlled_pilot_cases()}
    for observation in observations:
        if observation.system != "codex_chatgpt_baseline":
            raise ValueError("Pilot baseline rows must identify codex_chatgpt_baseline.")
        if observation.workflow_id not in known_cases:
            raise ValueError(f"Unknown baseline case: {observation.workflow_id}.")
        if observation.workflow_id in indexed:
            raise ValueError(
                f"Duplicate baseline observation for {observation.workflow_id}."
            )
        indexed[observation.workflow_id] = observation
    return indexed


def _as_differentiation_observation(
    observation: ControlledPilotObservation,
    useful_result: bool,
) -> DifferentiationObservation:
    return DifferentiationObservation(
        system="kba",
        workflow_id=observation.case_id,
        natural_request_sha256=observation.natural_request_sha256,
        useful_result=useful_result,
        route_correct=observation.route_correct,
        sources_visible=observation.visible_source_count > 0,
        followup_continuity=observation.work_item_continuity,
        context_reentry_fields=observation.context_reentry_fields,
        manual_provider_ids=observation.manual_provider_ids,
        approval_round_trips=observation.approval_round_trips,
        unintended_writes=observation.unintended_writes,
        duplicate_artifacts=observation.duplicate_artifacts,
        developer_intervention=observation.developer_intervention,
        latency_ms=observation.latency_ms,
        estimated_cost_usd=observation.estimated_cost_usd,
        evidence_refs=observation.evidence_refs,
    )
