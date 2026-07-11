#!/usr/bin/env python3
"""Validate ANU-223 manager delegation readiness without live operations."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from keystone_agents.manager_delegation_readiness import MANAGER_DELEGATION_READINESS_CASES

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-output", default="")
    args = parser.parse_args(argv)
    rows: list[dict[str, object]] = []

    for case in MANAGER_DELEGATION_READINESS_CASES:
        nodeids = (*case.manager_entry_nodeids, *case.provider_lifecycle_nodeids)
        command = [sys.executable, "-m", "pytest", *nodeids, "-q"]
        print(f"\n== {case.provider_family} delegation readiness ==", flush=True)
        completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        rows.append(
            {
                "provider_family": case.provider_family,
                "owning_agent": case.owning_agent,
                "status": "ready" if completed.returncode == 0 else "failed",
                "manager_provider_join_proven": case.manager_provider_join_proven,
                "next_live_proof": case.next_live_proof,
                "manager_entry_nodeids": list(case.manager_entry_nodeids),
                "provider_lifecycle_nodeids": list(case.provider_lifecycle_nodeids),
            }
        )
        if completed.returncode != 0:
            _write(args.json_output, rows, passed=False)
            return completed.returncode

    _write(args.json_output, rows, passed=True)
    joined = sum(bool(row["manager_provider_join_proven"]) for row in rows)
    print(
        "\nManager delegation pre-live readiness passed; "
        f"joined manager/provider proofs: {joined}/{len(rows)}; OpenAI API requests: 0."
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
                "schema": "keystone.manager_delegation_readiness.v1",
                "issue": "ANU-223",
                "passed": passed,
                "provider_family_count": len(rows),
                "manager_provider_join_proven_count": sum(
                    bool(row["manager_provider_join_proven"]) for row in rows
                ),
                "openai_api_requests": 0,
                "live_connectors": False,
                "external_side_effects": False,
                "cases": rows,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
