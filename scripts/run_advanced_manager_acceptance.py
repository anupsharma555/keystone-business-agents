#!/usr/bin/env python3
"""Run ANU-222 advanced manager acceptance without live providers or models."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from keystone_agents.advanced_manager_scenarios import (
    ADVANCED_MANAGER_GLOBAL_INVARIANTS,
    ADVANCED_MANAGER_SCENARIOS,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-output", default="")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    if args.list:
        for scenario in ADVANCED_MANAGER_SCENARIOS:
            print(f"{scenario.scenario_id}: {scenario.title}")
        return 0

    rows: list[dict[str, object]] = []
    for scenario in ADVANCED_MANAGER_SCENARIOS:
        command = [sys.executable, "-m", "pytest", *scenario.proof_nodeids, "-q"]
        print(f"\n== {scenario.scenario_id}: {scenario.title} ==", flush=True)
        completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        row = {
            "scenario_id": scenario.scenario_id,
            "title": scenario.title,
            "family": scenario.family,
            "expected_entry_owner": scenario.expected_entry_owner,
            "expected_outcome": scenario.expected_outcome,
            "required_invariants": list(scenario.required_invariants),
            "live_model_required": scenario.live_model_required,
            "external_side_effects_allowed": scenario.external_side_effects_allowed,
            "status": "pass" if completed.returncode == 0 else "fail",
            "failure_category": "" if completed.returncode == 0 else "offline_regression",
            "next_fix": (
                "" if completed.returncode == 0 else "Fix this scenario before live testing."
            ),
            "proof_nodeids": list(scenario.proof_nodeids),
        }
        rows.append(row)
        if completed.returncode != 0:
            _write_scorecard(args.json_output, rows, passed=False)
            return completed.returncode

    _write_scorecard(args.json_output, rows, passed=True)
    print("\nAdvanced manager acceptance passed; OpenAI API requests: 0.")
    return 0


def _write_scorecard(path_value: str, rows: list[dict[str, object]], *, passed: bool) -> None:
    if not path_value:
        return
    path = Path(path_value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "keystone.advanced_manager_acceptance.v1",
                "issue": "ANU-222",
                "passed": passed,
                "scenario_count": len(rows),
                "openai_api_requests": 0,
                "live_connectors": False,
                "external_side_effects": False,
                "legacy_promptfoo_included": False,
                "global_invariants": list(ADVANCED_MANAGER_GLOBAL_INVARIANTS),
                "scenarios": rows,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
