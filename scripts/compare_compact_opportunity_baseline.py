#!/usr/bin/env python3
"""Compare matched compact KBA and generic Opportunity receipts without a model call."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from keystone_agents.differentiation_matrix import (
    DifferentiationObservation,
    compare_differentiation_observations,
)
from scripts.run_compact_opportunity_assessment import pilot_ask


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kba", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _observation(path: Path, *, system: str) -> DifferentiationObservation:
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected_request = pilot_ask()
    if payload.get("natural_request") != expected_request:
        raise SystemExit(f"{path} does not contain the unchanged pilot ask.")
    if payload.get("status") != "pass" or not all(payload.get("checks", {}).values()):
        raise SystemExit(f"{path} is not a passing matched receipt.")
    safety = payload.get("safety", {})
    return DifferentiationObservation(
        system=system,
        workflow_id="current_opportunity_assessment",
        natural_request_sha256=hashlib.sha256(expected_request.encode()).hexdigest(),
        useful_result=True,
        route_correct=True,
        sources_visible=len(payload["output"].get("retained_sources", [])) > 0,
        followup_continuity=False,
        context_reentry_fields=0,
        manual_provider_ids=0,
        approval_round_trips=0,
        unintended_writes=int(bool(safety.get("external_action_performed"))),
        duplicate_artifacts=0,
        developer_intervention=False,
        latency_ms=None,
        estimated_cost_usd=float(payload["cost"]["estimated_usd"]),
        evidence_refs=(f"artifact:{path.as_posix()}",),
    )


def main() -> int:
    args = build_parser().parse_args()
    kba = _observation(args.kba, system="kba")
    baseline = _observation(args.baseline, system="codex_chatgpt_baseline")
    comparison = compare_differentiation_observations(kba, baseline)
    payload = {
        "schema": "keystone.compact_opportunity_baseline_comparison.v1",
        "workflow_id": comparison.workflow_id,
        "status": comparison.status,
        "improvements": list(comparison.improvements),
        "regressions": list(comparison.regressions),
        "kba_safe_and_useful": comparison.kba_safe_and_useful,
        "kba_estimated_cost_usd": kba.estimated_cost_usd,
        "baseline_estimated_cost_usd": baseline.estimated_cost_usd,
        "conclusion": (
            "No KBA advantage is claimed for this simple supplied-packet ask."
            if comparison.status == "not_supported"
            else "The matched evidence supports at least one KBA operator advantage."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(args.output)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
