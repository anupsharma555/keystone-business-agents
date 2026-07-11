#!/usr/bin/env python3
"""Validate simple, intermediate, and advanced agent execution without paid APIs."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXECUTION_TIER_CASES: dict[str, tuple[str, ...]] = {
    "simple": (
        "tests/test_human_agent_execution_jobs.py::"
        "test_human_job_catalogue_covers_every_agent_family_and_complexity_tier",
        "tests/test_human_agent_execution_jobs.py::"
        "test_human_jobs_route_from_natural_language_to_the_expected_owner",
        "tests/test_basic_agent_execution_smoke_tasks.py::"
        "test_basic_smoke_prompts_route_with_existing_planner_without_new_shortcuts",
        "tests/test_sdk_execution.py::"
        "test_all_specialist_agents_run_with_fake_model_without_openai_key",
        "tests/test_sdk_execution.py::"
        "test_context_agent_fake_model_selects_and_executes_typed_history_tool",
        "tests/test_sdk_execution.py::"
        "test_operating_specialist_fake_model_selects_and_consumes_domain_tool",
        "tests/test_sdk_execution.py::"
        "test_structured_context_agent_fake_model_selects_and_executes_typed_read_tool",
        "tests/test_workflow_runner.py::"
        "test_source_provided_comparison_uses_supplied_evidence_before_generic_comparison",
        "tests/test_workflow_runner.py::"
        "test_basic_smoke_gmail_to_calendar_without_context_returns_exact_safe_blocker",
    ),
    "intermediate": (
        "tests/test_human_agent_execution_jobs.py::"
        "test_human_jobs_preserve_key_scope_and_workflow_constraints",
        "tests/test_sdk_execution.py::test_fake_model_tool_call_executes_fixture_tool",
        "tests/test_sdk_execution.py::"
        "test_structured_context_agent_fake_model_selects_guarded_write_preview_tool",
        "tests/test_sdk_execution.py::"
        "test_gmail_fake_model_session_followup_preserves_message_and_prior_draft",
        "tests/test_sdk_execution.py::"
        "test_structured_context_agent_session_followup_preserves_identity_and_updates",
        "tests/test_sdk_execution.py::"
        "test_gmail_constrained_sdk_rejects_draft_without_approval_gate",
        "tests/test_workflow_runner.py::"
        "test_basic_smoke_outreach_revision_uses_approved_inline_facts_without_side_effects",
        "tests/test_workflow_runner.py::test_chief_advisory_requests_stay_chief_owned",
    ),
    "advanced": (
        "tests/test_human_agent_execution_jobs.py::"
        "test_advanced_human_jobs_select_the_graph_backend",
        "tests/test_sdk_execution.py::"
        "test_chief_specialist_agent_tools_invoke_nested_agents_with_fake_model",
        "tests/test_langgraph_workflow.py::"
        "test_backend_selected_manager_loop_uses_graph_for_research_opportunity_outreach_checkpoint",
        "tests/test_langgraph_workflow.py::"
        "test_backend_selected_manager_loop_uses_graph_for_preprints_zotero_research_artifact_plan",
        "tests/test_langgraph_workflow.py::"
        "test_backend_selected_checkpoint_approval_resume_reports_saved_state",
    ),
}


def tier_commands(tiers: list[str]) -> list[tuple[str, list[str]]]:
    return [
        (tier, [sys.executable, "-m", "pytest", *EXECUTION_TIER_CASES[tier], "-q"])
        for tier in tiers
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run framework-level KBA execution tiers without OpenAI API calls."
    )
    parser.add_argument(
        "--tier",
        action="append",
        choices=tuple(EXECUTION_TIER_CASES),
        help="Tier to run. Repeat to select multiple; defaults to all tiers.",
    )
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--json-output", default="")
    args = parser.parse_args(argv)
    tiers = args.tier or list(EXECUTION_TIER_CASES)
    commands = tier_commands(tiers)

    if args.list:
        for tier, command in commands:
            print(f"{tier}: {' '.join(command)}")
        return 0

    rows: list[dict[str, object]] = []
    for tier, command in commands:
        print(f"\n== {tier} agent execution ==", flush=True)
        completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        rows.append(
            {
                "tier": tier,
                "case_count": len(EXECUTION_TIER_CASES[tier]),
                "exit_code": completed.returncode,
            }
        )
        if completed.returncode != 0:
            _write_summary(args.json_output, rows, passed=False)
            return completed.returncode

    _write_summary(args.json_output, rows, passed=True)
    print("\nAgent execution tiers passed; OpenAI API requests: 0.")
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
                "schema": "keystone.agent_execution_tiers.v1",
                "passed": passed,
                "openai_api_requests": 0,
                "live_connectors": False,
                "legacy_promptfoo_included": False,
                "tiers": rows,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
