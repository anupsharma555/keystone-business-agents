#!/usr/bin/env python3
"""Run the zero-API readiness gate for a fresh ANU-61 Gmail pilot observation."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from keystone_agents.gmail_pilot_rerun_readiness import gmail_pilot_rerun_readiness

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/gmail-pilot-rerun-readiness.json"),
    )
    args = parser.parse_args(argv)
    readiness = gmail_pilot_rerun_readiness()
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *readiness.proof_nodeids, "-q"],
        cwd=PROJECT_ROOT,
        check=False,
    )
    passed = completed.returncode == 0
    payload = {
        "schema": "keystone.gmail_pilot_rerun_readiness.v1",
        "status": "offline_ready_live_not_started" if passed else "offline_failed",
        "case_id": readiness.case_id,
        "natural_request_sha256": readiness.natural_request_sha256,
        "backend": readiness.backend,
        "model": readiness.model,
        "max_openai_requests": readiness.max_openai_requests,
        "max_cost_usd": readiness.max_cost_usd,
        "provider_writes_allowed": 0,
        "proof_nodeids": list(readiness.proof_nodeids),
        "proofs_passed": passed,
        "offline_no_repair_path_proven": passed,
        "human_review_rubric": list(readiness.required_human_review_checks),
        "human_review_passed": False,
        "developer_intervention_free_live_run_proven": False,
        "fresh_live_observation_required": True,
        "live_stop_conditions": list(readiness.live_stop_conditions),
        "openai_api_requests": 0,
        "gmail_reads": 0,
        "gmail_writes": 0,
        "slack_posts": 0,
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    output = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(output)
    print(rendered, end="")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
