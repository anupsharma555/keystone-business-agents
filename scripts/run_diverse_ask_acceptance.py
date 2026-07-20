#!/usr/bin/env python3
"""Render the no-live diverse-ask architecture acceptance matrix."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from keystone_agents.diverse_ask_acceptance import (
    automated_proof_nodeids,
    build_diverse_ask_acceptance_report,
)


def finalize_proof_execution(
    report: dict[str, object],
    *,
    proof_node_count: int,
    proofs_passed: bool,
) -> dict[str, object]:
    """Promote structural coverage only after every automated proof passes."""

    coverage_counts = report.get("coverage_counts")
    automated_count = (
        int(coverage_counts.get("automated") or 0)
        if isinstance(coverage_counts, dict)
        else 0
    )
    all_rows_automated = automated_count == int(report.get("case_count") or 0)
    offline_pass = bool(
        report.get("structurally_complete") and all_rows_automated and proofs_passed
    )
    report.update(
        {
            "automated_proof_node_count": proof_node_count,
            "automated_proofs_passed": proofs_passed,
            "all_rows_automated": all_rows_automated,
            "behavioral_pass_claimed": offline_pass,
            "offline_behavioral_pass_claimed": offline_pass,
            "live_pass_claimed": False,
            "status": "complete" if offline_pass else "incomplete",
        }
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    report = build_diverse_ask_acceptance_report()
    proof_nodes = automated_proof_nodeids()
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *proof_nodes, "-q"],
        check=False,
    )
    report = finalize_proof_execution(
        report,
        proof_node_count=len(proof_nodes),
        proofs_passed=completed.returncode == 0,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.json_output.with_suffix(args.json_output.suffix + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(args.json_output)
    print(rendered, end="")
    return 0 if report["offline_behavioral_pass_claimed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
