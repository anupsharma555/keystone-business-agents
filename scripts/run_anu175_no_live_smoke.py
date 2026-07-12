#!/usr/bin/env python3
"""Run the ANU-175 differentiation smoke without models or live providers."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

PROOF_NODEIDS: tuple[str, ...] = (
    "tests/test_differentiation_matrix.py::test_safe_offline_observation_proves_no_side_effect_execution_boundary",
    "tests/test_differentiation_matrix.py::test_comparison_supports_claim_only_for_matched_direct_evidence",
    "tests/test_differentiation_matrix.py::test_comparison_rejects_mismatched_ask_instead_of_inventing_baseline",
    "tests/test_controlled_pilot_scorecard.py::test_matched_baseline_supports_only_evidence_backed_comparison",
)


def smoke_command() -> list[str]:
    return [sys.executable, "-m", "pytest", *PROOF_NODEIDS, "-q"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-output", default="")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    command = smoke_command()
    if args.list:
        print(" ".join(command))
        return 0

    completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    passed = completed.returncode == 0
    _write_receipt(args.json_output, command=command, passed=passed)
    if passed:
        print(
            "ANU-175 no-live differentiation smoke passed; "
            "OpenAI requests: 0; provider writes: 0."
        )
    return completed.returncode


def _write_receipt(path_value: str, *, command: list[str], passed: bool) -> None:
    if not path_value:
        return
    path = Path(path_value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "keystone.anu175_no_live_smoke.v1",
                "issue": "ANU-175",
                "passed": passed,
                "proof_scope": (
                    "matched-observation comparison, safe-useful KBA boundary, "
                    "and missing-or-mismatched baseline rejection"
                ),
                "command": command,
                "proof_nodeids": list(PROOF_NODEIDS),
                "openai_api_requests": 0,
                "live_connectors": False,
                "provider_writes": 0,
                "external_side_effects": False,
                "comparative_claim_boundary": (
                    "The smoke proves the executable comparison contract; it does not "
                    "replace a real matched operator baseline observation."
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
