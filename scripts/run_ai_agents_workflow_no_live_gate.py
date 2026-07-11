#!/usr/bin/env python3
"""Run the current ai-agents-workflow no-live validation ladder.

This intentionally excludes the legacy 102-case Promptfoo suite. That suite is
reserved for future migration and reactivation after the current agents and
backend-selected graph paths are stable.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

FOCUSED_TESTS = (
    "tests/test_advanced_manager_scenarios.py",
    "tests/test_basic_agent_execution_smoke_tasks.py",
    "tests/test_human_agent_execution_jobs.py",
    "tests/test_agent_operational_validation_status.py",
    "tests/test_announcement_context_tools.py",
    "tests/test_announcement_context_read_validation.py",
    "tests/test_manual_request_plan.py",
    "tests/test_workflow_runner.py",
    "tests/test_langgraph_workflow.py",
    "tests/test_langgraph_quality.py",
    "tests/test_cli_work_items.py",
)


def gate_commands(
    *, include_anu60: bool = True, include_evals_readiness: bool = False
) -> list[list[str]]:
    commands = [
        [sys.executable, "-m", "pytest", *FOCUSED_TESTS, "-q"],
        [
            sys.executable,
            "scripts/compare_langgraph_quality.py",
            "--all-scenarios",
            "--require-ready",
        ],
        [
            sys.executable,
            "scripts/run_slack_agent_expansion_gate.py",
            "--quiet",
        ],
        [
            sys.executable,
            "scripts/run_advanced_manager_acceptance.py",
        ],
    ]
    if include_anu60:
        commands.append(["npm", "run", "eval:slack:anu60-preflight"])
    if include_evals_readiness:
        commands.append(["npm", "run", "eval:slack:strict-readiness", "--", "--json"])
    return commands


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run current KBA ai-agents-workflow validation without live APIs."
    )
    parser.add_argument("--list", action="store_true", help="Print commands without running them.")
    parser.add_argument("--skip-anu60", action="store_true")
    parser.add_argument(
        "--include-evals-readiness",
        action="store_true",
        help=(
            "Also check the deferred #evals dashboard/runtime. This is not part of "
            "the current agent/graph gate."
        ),
    )
    parser.add_argument("--json-output", default="")
    args = parser.parse_args(argv)

    commands = gate_commands(
        include_anu60=not args.skip_anu60,
        include_evals_readiness=args.include_evals_readiness,
    )
    if args.list:
        for command in commands:
            print(" ".join(command))
        return 0

    rows: list[dict[str, object]] = []
    for command in commands:
        print(f"\n== {' '.join(command)} ==", flush=True)
        completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        rows.append({"command": command, "exit_code": completed.returncode})
        if completed.returncode != 0:
            _write_summary(args.json_output, rows, passed=False)
            return completed.returncode

    _write_summary(args.json_output, rows, passed=True)
    print("\nai-agents-workflow no-live gate passed; no live API/search/connector call ran.")
    return 0


def _write_summary(path_value: str, rows: list[dict[str, object]], *, passed: bool) -> None:
    if not path_value:
        return
    path = Path(path_value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "keystone.ai_agents_workflow.no_live_gate.v1",
                "passed": passed,
                "live_sdk": False,
                "live_search": False,
                "legacy_promptfoo_102_included": False,
                "commands": rows,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
