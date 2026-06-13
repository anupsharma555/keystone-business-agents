#!/usr/bin/env python3
"""Run Slack eval readiness, then serve the local dashboard for live testing."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml

from promptfoo.eval_dashboard_server import main as dashboard_server_main
from promptfoo.eval_urls import eval_dashboard_case_url, eval_dashboard_url

DEFAULT_START_CASE_ID = "slack_company_research_001"


def main() -> int:
    original_argv = list(sys.argv)
    args = _build_parser().parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    if not args.skip_readiness:
        readiness_command = [sys.executable, "scripts/check_eval_slack_readiness.py"]
        if args.strict_readiness:
            readiness_command.append("--strict")
        if args.live_slack_probe:
            readiness_command.append("--live-slack-probe")
        completed = subprocess.run(
            readiness_command,
            cwd=repo_root,
            check=False,
        )
        if completed.returncode != 0:
            return completed.returncode
    try:
        starter_prompt = _resolve_starter_eval_prompt(
            repo_root,
            agent=args.agent,
            case_id=args.case_id,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(
        "\nReady for #evals. Leave this process running while you click Slack case links.",
        flush=True,
    )
    print("Start with this committed eval prompt in #evals:", flush=True)
    print(f"Agent: {starter_prompt['agent']}", flush=True)
    print(f"Case: {starter_prompt['case_id']}", flush=True)
    if starter_prompt.get("context"):
        print("Context to include before the prompt:", flush=True)
        for context_line in starter_prompt["context"].splitlines():
            print(f"- {context_line}", flush=True)
    print(f"Paste: {starter_prompt['user_input']}", flush=True)
    print(f"Case dashboard: {starter_prompt['dashboard_url']}", flush=True)
    print(f"Dashboard links should open at {eval_dashboard_url()}", flush=True)
    sys.argv = [original_argv[0]]
    try:
        return dashboard_server_main()
    finally:
        sys.argv = original_argv


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Slack eval readiness preflight and start the local eval dashboard server."
        )
    )
    parser.add_argument(
        "--skip-readiness",
        action="store_true",
        help="Start the dashboard server without running the readiness preflight first.",
    )
    parser.add_argument(
        "--strict-readiness",
        action="store_true",
        help="Fail before starting the dashboard server if readiness warnings remain.",
    )
    parser.add_argument(
        "--live-slack-probe",
        action="store_true",
        help="Run read-only Slack Web API checks before starting the dashboard server.",
    )
    parser.add_argument(
        "--agent",
        help=(
            "Print the first committed Promptfoo eval prompt for this agent before "
            "serving the dashboard."
        ),
    )
    parser.add_argument(
        "--case-id",
        help=(
            "Print this committed Promptfoo eval case before serving the dashboard. "
            f"Defaults to {DEFAULT_START_CASE_ID} when --agent is omitted."
        ),
    )
    return parser


def _resolve_starter_eval_prompt(
    repo_root: Path,
    *,
    agent: str | None = None,
    case_id: str | None = None,
) -> dict[str, str]:
    """Return a pasteable Slack prompt from the committed Promptfoo eval cases."""
    requested_agent = str(agent or "").strip()
    requested_case_id = str(case_id or "").strip()
    if not requested_agent and not requested_case_id:
        requested_case_id = DEFAULT_START_CASE_ID

    cases = list(_iter_promptfoo_eval_cases(repo_root))
    if not cases:
        raise ValueError("no Promptfoo eval cases found under promptfoo/tests")

    if requested_case_id:
        candidate_cases = cases
    elif requested_agent:
        agent_cases = [case for case in cases if case["agent"] == requested_agent]
        candidate_cases = [
            *[case for case in agent_cases if not case.get("context")],
            *[case for case in agent_cases if case.get("context")],
        ]
    else:
        candidate_cases = cases

    for case in candidate_cases:
        case_agent = case["agent"]
        case_identifier = case["case_id"]
        if requested_case_id and case_identifier != requested_case_id:
            continue
        if requested_agent and case_agent != requested_agent:
            continue
        return {
            **case,
            "dashboard_url": eval_dashboard_case_url(case_identifier),
        }

    available_agents = sorted({case["agent"] for case in cases})
    if requested_case_id:
        raise ValueError(
            f"no committed Promptfoo eval case matched case_id={requested_case_id!r}"
        )
    raise ValueError(
        f"no committed Promptfoo eval prompt found for agent={requested_agent!r}; "
        f"available agents: {', '.join(available_agents)}"
    )


def _iter_promptfoo_eval_cases(repo_root: Path):
    tests_dir = repo_root / "promptfoo" / "tests"
    for path in sorted(tests_dir.glob("*.yaml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        cases = loaded.get("tests", []) if isinstance(loaded, dict) else loaded
        for raw_case in cases:
            variables = (raw_case or {}).get("vars") or {}
            case_id = str(variables.get("case_id") or "").strip()
            agent = str(variables.get("agent_under_test") or "").strip()
            user_input = str(variables.get("user_input") or "").strip()
            context_lines = _starter_context_lines(variables, user_input=user_input)
            if case_id and agent and user_input:
                yield {
                    "case_id": case_id,
                    "agent": agent,
                    "user_input": user_input,
                    "context": "\n".join(context_lines),
                }


def _starter_context_lines(variables: dict[str, object], *, user_input: str) -> list[str]:
    slack_context = variables.get("slack_context")
    if not isinstance(slack_context, dict):
        return []
    messages = slack_context.get("thread_messages")
    if not isinstance(messages, list):
        return []
    lines: list[str] = []
    normalized_user_input = " ".join(str(user_input or "").split())
    for message in messages:
        if isinstance(message, dict):
            text = str(message.get("text") or "").strip()
            normalized_text = " ".join(text.split())
            if text and normalized_text != normalized_user_input:
                lines.append(text)
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
