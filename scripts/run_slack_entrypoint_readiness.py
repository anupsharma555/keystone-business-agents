#!/usr/bin/env python3
"""Validate the Slack entrypoint acceptance packet without live Slack or models."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from keystone_agents.slack_entrypoint_readiness import SLACK_ENTRYPOINT_READINESS_CASES

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-output", default="")
    args = parser.parse_args(argv)
    rows: list[dict[str, object]] = []

    for case in SLACK_ENTRYPOINT_READINESS_CASES:
        command = [sys.executable, "-m", "pytest", *case.proof_nodeids, "-q"]
        print(f"\n== {case.probe_id}: {case.title} ==", flush=True)
        completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        rows.append(
            {
                "probe_id": case.probe_id,
                "backend": case.backend,
                "pre_live_status": "ready" if completed.returncode == 0 else "failed",
                "live_slack_evidence_proven": case.live_slack_evidence_proven,
                "live_evidence_refs": list(case.live_evidence_refs),
                "max_openai_requests": case.max_openai_requests,
                "max_cost_usd": case.max_cost_usd,
                "proof_nodeids": list(case.proof_nodeids),
                "required_visible_evidence": list(case.required_visible_evidence),
            }
        )
        if completed.returncode != 0:
            _write(args.json_output, rows, passed=False)
            return completed.returncode

    _write(args.json_output, rows, passed=True)
    live_count = sum(case.live_slack_evidence_proven for case in SLACK_ENTRYPOINT_READINESS_CASES)
    print(
        "\nSlack entrypoint pre-live readiness passed; live Slack evidence: "
        f"{live_count}/{len(SLACK_ENTRYPOINT_READINESS_CASES)}; OpenAI API requests: 0."
    )
    return 0


def _write(path_value: str, rows: list[dict[str, object]], *, passed: bool) -> None:
    if not path_value:
        return
    path = Path(path_value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "keystone.slack_entrypoint_readiness.v1",
                "passed": passed,
                "probe_count": len(rows),
                "live_slack_evidence_proven_count": sum(
                    bool(row["live_slack_evidence_proven"]) for row in rows
                ),
                "combined_max_openai_requests": sum(
                    int(row["max_openai_requests"]) for row in rows
                ),
                "combined_max_cost_usd": sum(float(row["max_cost_usd"]) for row in rows),
                "live_slack_posts": False,
                "openai_api_requests": 0,
                "cases": rows,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
