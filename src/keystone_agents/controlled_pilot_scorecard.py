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
from keystone_agents.pilot_receipt_replay import PilotReceiptReplay


def build_controlled_pilot_scorecard(
    *,
    trusted_runtime_rows: dict[str, str],
    observations: Iterable[ControlledPilotObservation],
    baseline_observations: Iterable[DifferentiationObservation] = (),
    replay_readiness: Iterable[PilotReceiptReplay] = (),
) -> dict[str, Any]:
    """Build one complete scorecard without inferring missing evidence."""

    observation_by_case = _unique_by_case(observations, label="KBA pilot")
    baseline_by_case = _unique_baselines(baseline_observations)
    replay_by_case = _unique_replays(replay_readiness)
    overlapping_cases = sorted(set(observation_by_case) & set(replay_by_case))
    if overlapping_cases:
        raise ValueError(
            "Replay readiness cannot accompany a pilot observation for: "
            + ", ".join(overlapping_cases)
        )
    case_rows: list[dict[str, Any]] = []
    observed_requests = 0
    observed_cost = 0.0
    observed_latency = 0

    for case in controlled_pilot_cases():
        observation = observation_by_case.get(case.case_id)
        if observation is None:
            replay = replay_by_case.get(case.case_id)
            status = "pending_slack_transport" if replay else "missing"
            failed_checks = (
                ["slack_permalink_missing", "pilot_observation_missing"]
                if replay
                else ["observation_missing"]
            )
            case_rows.append(
                {
                    "case_id": case.case_id,
                    "title": case.title,
                    "status": status,
                    "failed_checks": failed_checks,
                    "backend": case.backend,
                    "entry_owner": case.expected_entry_owner,
                    "context_sources": list(case.context_sources),
                    "expected_artifact": case.expected_artifact,
                    "allowed_provider_writes": case.allowed_provider_writes,
                    "max_openai_requests": case.max_openai_requests,
                    "max_cost_usd": case.max_cost_usd,
                    "comparison_status": "pending_kba_observation",
                    "replay_ready": replay is not None,
                    "replay_openai_requests": (
                        replay.replay_openai_requests if replay is not None else None
                    ),
                    "repeated_model_synthesis": (
                        replay.repeated_model_synthesis if replay is not None else None
                    ),
                    "replay_provider_writes": (
                        replay.provider_writes if replay is not None else None
                    ),
                    "replay_visible_source_count": (
                        len(replay.visible_sources) if replay is not None else 0
                    ),
                    "replay_evidence_refs": (
                        list(replay.evidence_refs) if replay is not None else []
                    ),
                    **_case_guidance(
                        status=status,
                        failed_checks=failed_checks,
                        max_openai_requests=case.max_openai_requests,
                        comparison_status="pending_kba_observation",
                    ),
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
        row = {
            "case_id": case.case_id,
            "title": case.title,
            "status": assessment.status,
            "failed_checks": list(assessment.failed_checks),
            "backend": case.backend,
            "entry_owner": case.expected_entry_owner,
            "context_sources": list(case.context_sources),
            "expected_artifact": case.expected_artifact,
            "allowed_provider_writes": case.allowed_provider_writes,
            "max_openai_requests": case.max_openai_requests,
            "max_cost_usd": case.max_cost_usd,
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
            **_case_guidance(
                status=assessment.status,
                failed_checks=list(assessment.failed_checks),
                max_openai_requests=case.max_openai_requests,
                comparison_status=comparison_status,
            ),
        }
        case_rows.append(row)

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
        "replay_ready_case_count": len(replay_by_case),
        "passing_case_count": sum(row["status"] == "pass" for row in case_rows),
        "totals": {
            "openai_requests": observed_requests,
            "estimated_cost_usd": round(observed_cost, 8),
            "latency_ms": observed_latency,
        },
        "planned_ceiling": {
            "openai_requests": sum(
                case.max_openai_requests for case in controlled_pilot_cases()
            ),
            "estimated_cost_usd": round(
                sum(case.max_cost_usd for case in controlled_pilot_cases()), 8
            ),
            "provider_writes": sum(
                case.allowed_provider_writes for case in controlled_pilot_cases()
            ),
        },
        "cases": case_rows,
        "baseline_observation_count": len(baseline_by_case),
        "comparative_claims_supported": sum(
            row.get("comparison_status") == "supported" for row in case_rows
        ),
    }


def controlled_pilot_observation_from_dict(payload: dict[str, Any]) -> ControlledPilotObservation:
    if payload.get("pilot_observation_claimed") is False or payload.get("transport_status"):
        raise ValueError("Replay readiness cannot be ingested as a pilot observation.")
    values = dict(payload)
    values["evidence_refs"] = tuple(values.get("evidence_refs") or ())
    return ControlledPilotObservation(**values)


def differentiation_observation_from_dict(payload: dict[str, Any]) -> DifferentiationObservation:
    values = dict(payload)
    values["evidence_refs"] = tuple(values.get("evidence_refs") or ())
    return DifferentiationObservation(**values)


def _case_guidance(
    *,
    status: str,
    failed_checks: list[str],
    max_openai_requests: int,
    comparison_status: str,
) -> dict[str, Any]:
    if status == "pending_slack_transport":
        return {
            "reuse_policy": "reuse_saved_specialist_output_no_model_rerun",
            "model_rerun_required": False,
            "transport_only": True,
            "next_required_evidence": [
                "Authorized Slack transport of the prepared replay packet.",
                "Slack permalink plus local run or WorkItem identity.",
                "Human review pass and one final answer-first response.",
            ],
        }
    if status == "missing":
        return {
            "reuse_policy": "no_reusable_case_receipt",
            "model_rerun_required": True,
            "transport_only": False,
            "next_required_evidence": [
                "One exact catalog-ask pilot observation with route, backend, sources, "
                "usage, cost, Slack permalink, and no-side-effect review."
            ],
        }
    if status == "fail":
        evidence: list[str] = []
        mapping = {
            "human_review_passed": "Fresh human copy/usefulness review marked pass.",
            "no_developer_intervention": "Fresh execution requiring no developer repair.",
            "request_ceiling": (
                f"Fresh exact-ask receipt using at most {max_openai_requests} OpenAI requests."
            ),
        }
        for check in failed_checks:
            evidence.append(mapping.get(check, f"Fresh evidence passing check: {check}."))
        return {
            "reuse_policy": "do_not_promote_failed_observation",
            "model_rerun_required": True,
            "transport_only": False,
            "next_required_evidence": evidence,
        }
    next_evidence = []
    if comparison_status == "pending_baseline":
        next_evidence.append("One matched baseline observation for comparative assessment.")
    return {
        "reuse_policy": "reuse_passing_observation",
        "model_rerun_required": False,
        "transport_only": False,
        "next_required_evidence": next_evidence,
    }


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


def _unique_replays(
    replays: Iterable[PilotReceiptReplay],
) -> dict[str, PilotReceiptReplay]:
    indexed: dict[str, PilotReceiptReplay] = {}
    known_cases = {case.case_id for case in controlled_pilot_cases()}
    for replay in replays:
        if replay.case_id not in known_cases:
            raise ValueError(f"Unknown replay-readiness case: {replay.case_id}.")
        if replay.case_id in indexed:
            raise ValueError(f"Duplicate replay readiness for {replay.case_id}.")
        indexed[replay.case_id] = replay
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
