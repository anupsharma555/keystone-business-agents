from __future__ import annotations

import json
from dataclasses import asdict
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from keystone_agents.controlled_pilot import (
    ControlledPilotObservation,
    controlled_pilot_cases,
    controlled_pilot_natural_request_sha256,
)
from keystone_agents.controlled_pilot_scorecard import build_controlled_pilot_scorecard
from keystone_agents.differentiation_matrix import DifferentiationObservation


def _observation(case_id: str) -> ControlledPilotObservation:
    case = next(item for item in controlled_pilot_cases() if item.case_id == case_id)
    return ControlledPilotObservation(
        case_id=case_id,
        natural_request_sha256=controlled_pilot_natural_request_sha256(case),
        slack_permalink_present=True,
        answer_first=True,
        human_review_passed=True,
        final_response_count=1,
        route_correct=True,
        graph_used=case.backend == "langgraph",
        work_item_continuity=True,
        visible_source_count=2,
        context_reentry_fields=0,
        manual_provider_ids=0,
        approval_round_trips=1 if case.allowed_provider_writes else 0,
        provider_writes=case.allowed_provider_writes,
        provider_readback_verified=case.allowed_provider_writes > 0,
        cleanup_verified=not case.requires_cleanup,
        unintended_writes=0,
        duplicate_artifacts=0,
        developer_intervention=False,
        openai_requests=case.max_openai_requests,
        estimated_cost_usd=case.max_cost_usd,
        latency_ms=1_000,
        evidence_refs=(f"slack:{case_id}", f"trace:{case_id}"),
    )


def _baseline(case_id: str) -> DifferentiationObservation:
    case = next(item for item in controlled_pilot_cases() if item.case_id == case_id)
    return DifferentiationObservation(
        system="codex_chatgpt_baseline",
        workflow_id=case_id,
        natural_request_sha256=controlled_pilot_natural_request_sha256(case),
        useful_result=True,
        route_correct=True,
        sources_visible=False,
        followup_continuity=False,
        context_reentry_fields=2,
        manual_provider_ids=1,
        approval_round_trips=2,
        unintended_writes=0,
        duplicate_artifacts=0,
        developer_intervention=False,
        latency_ms=900,
        estimated_cost_usd=0.01,
        evidence_refs=(f"baseline:{case_id}",),
    )


def test_scorecard_stays_pending_when_trusted_rows_or_cases_are_missing() -> None:
    scorecard = build_controlled_pilot_scorecard(
        trusted_runtime_rows={"L174-14": "pass"},
        observations=[_observation("current_opportunity_assessment")],
    )

    assert scorecard["pilot_status"] == "pending"
    assert scorecard["trusted_runtime_ready"] is False
    assert scorecard["observed_case_count"] == 1
    assert [row["status"] for row in scorecard["cases"]].count("missing") == 3
    assert scorecard["comparative_claims_supported"] == 0
    assert scorecard["planned_ceiling"]["provider_writes"] == 0
    missing = next(row for row in scorecard["cases"] if row["status"] == "missing")
    assert missing["backend"] in {"direct_specialist", "langgraph"}
    assert missing["expected_artifact"]
    assert missing["max_openai_requests"] > 0


def test_complete_safe_observations_pass_and_aggregate_metrics() -> None:
    observations = [_observation(case.case_id) for case in controlled_pilot_cases()]
    scorecard = build_controlled_pilot_scorecard(
        trusted_runtime_rows={
            row: "pass" for row in ("L174-14", "L174-16", "L174-19", "L174-20")
        },
        observations=observations,
    )

    assert scorecard["pilot_status"] == "pass"
    assert scorecard["passing_case_count"] == 4
    assert scorecard["totals"]["openai_requests"] == sum(
        case.max_openai_requests for case in controlled_pilot_cases()
    )
    assert scorecard["totals"]["latency_ms"] == 4_000
    assert all(row["comparison_status"] == "pending_baseline" for row in scorecard["cases"])


def test_matched_baseline_supports_only_evidence_backed_comparison() -> None:
    case_id = "selected_gmail_thread_followup"
    scorecard = build_controlled_pilot_scorecard(
        trusted_runtime_rows={},
        observations=[_observation(case_id)],
        baseline_observations=[_baseline(case_id)],
    )

    row = next(item for item in scorecard["cases"] if item["case_id"] == case_id)
    assert row["comparison_status"] == "supported"
    assert set(row["improvements"]) == {
        "context_reentry_fields",
        "manual_provider_ids",
        "approval_round_trips",
        "sources_visible",
        "followup_continuity",
    }
    assert scorecard["comparative_claims_supported"] == 1


def test_duplicate_observations_are_rejected_instead_of_cherry_picking() -> None:
    observation = _observation("weekly_project_brief")
    with pytest.raises(ValueError, match="Duplicate KBA pilot observation"):
        build_controlled_pilot_scorecard(
            trusted_runtime_rows={},
            observations=[observation, observation],
        )


def test_unknown_or_wrong_system_rows_are_rejected() -> None:
    unknown = _observation("weekly_project_brief")
    unknown = ControlledPilotObservation(**{**asdict(unknown), "case_id": "unknown"})
    with pytest.raises(ValueError, match="Unknown KBA pilot case"):
        build_controlled_pilot_scorecard(
            trusted_runtime_rows={},
            observations=[unknown],
        )

    wrong_system = _baseline("weekly_project_brief")
    wrong_system = DifferentiationObservation(
        **{**asdict(wrong_system), "system": "kba"}
    )
    with pytest.raises(ValueError, match="must identify codex_chatgpt_baseline"):
        build_controlled_pilot_scorecard(
            trusted_runtime_rows={},
            observations=[],
            baseline_observations=[wrong_system],
        )


def test_cli_writes_atomic_pending_scorecard(tmp_path: Path, capsys) -> None:
    script_path = Path(__file__).parents[1] / "scripts" / "build_controlled_pilot_scorecard.py"
    spec = spec_from_file_location("build_controlled_pilot_scorecard", script_path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "scorecard.json"
    input_path.write_text(
        json.dumps(
            {
                "trusted_runtime_rows": {},
                "observations": [asdict(_observation("current_opportunity_assessment"))],
            }
        ),
        encoding="utf-8",
    )

    assert module.main(["--input", str(input_path), "--output", str(output_path)]) == 1
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["pilot_status"] == "pending"
    assert written["observed_case_count"] == 1
    assert json.loads(capsys.readouterr().out) == written
