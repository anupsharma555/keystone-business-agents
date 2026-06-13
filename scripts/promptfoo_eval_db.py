#!/usr/bin/env python3
"""Inspect and update the local Promptfoo eval database."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from promptfoo.eval_database import (
    DEFAULT_EVAL_DB,
    eval_case_status,
    import_promptfoo_results,
    list_eval_cases,
    record_slack_eval_run,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the local Promptfoo eval database.")
    parser.add_argument(
        "--database-path",
        default=str(DEFAULT_EVAL_DB),
        help="SQLite database path. Defaults to the repo-local eval DB.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import-latest", help="Import Promptfoo JSON results.")
    import_parser.add_argument(
        "--results-path",
        default=".keystone/promptfoo/latest-eval.json",
        help="Promptfoo JSON output path.",
    )

    status_parser = subparsers.add_parser("status", help="Show merged status for one case.")
    status_parser.add_argument("--case-id", required=True)
    status_parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")

    list_parser = subparsers.add_parser("list", help="List eval cases.")
    list_parser.add_argument("--limit", type=int, default=50)
    list_parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")

    slack_parser = subparsers.add_parser("record-slack-run", help="Record a real Slack eval run.")
    slack_parser.add_argument("--case-id", required=True)
    slack_parser.add_argument("--run-id", default="")
    slack_parser.add_argument("--agent", default="")
    slack_parser.add_argument("--slack-channel-id", default="C0BA17Y9C01")
    slack_parser.add_argument("--slack-channel-name", default="evals")
    slack_parser.add_argument("--slack-thread-ts", default="")
    slack_parser.add_argument("--request-text", default="")
    slack_parser.add_argument("--result-summary", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    database_path = Path(args.database_path)
    if args.command == "import-latest":
        summary = import_promptfoo_results(
            args.results_path,
            database_path=database_path,
        )
        print(json.dumps(summary.to_dict(), ensure_ascii=True, indent=2, sort_keys=True))
        return 0

    if args.command == "status":
        status = eval_case_status(args.case_id, database_path=database_path)
        if args.json:
            print(json.dumps(status, ensure_ascii=True, indent=2, sort_keys=True))
        else:
            print(_format_status(status))
        return 0

    if args.command == "list":
        rows = list_eval_cases(database_path=database_path, limit=args.limit)
        if args.json:
            print(json.dumps(rows, ensure_ascii=True, indent=2, sort_keys=True))
        else:
            print(_format_case_list(rows))
        return 0

    if args.command == "record-slack-run":
        row_id = record_slack_eval_run(
            case_id=args.case_id,
            run_id=args.run_id,
            agent=args.agent,
            slack_channel_id=args.slack_channel_id,
            slack_channel_name=args.slack_channel_name,
            slack_thread_ts=args.slack_thread_ts,
            request_text=args.request_text or sys.stdin.read(),
            result_summary=args.result_summary,
            database_path=database_path,
        )
        print(
            json.dumps(
                {
                    "id": row_id,
                    "case_id": args.case_id,
                    "run_id": args.run_id,
                    "database_path": str(database_path),
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


def _format_status(status: dict[str, object]) -> str:
    case_id = str(status.get("case_id") or "")
    promptfoo = (
        status.get("latest_promptfoo")
        if isinstance(status.get("latest_promptfoo"), dict)
        else None
    )
    human = (
        status.get("latest_human_review")
        if isinstance(status.get("latest_human_review"), dict)
        else None
    )
    slack_runs = int(status.get("slack_run_count") or 0)
    lines = [f"Eval case: {case_id}"]
    if promptfoo:
        passed = "pass" if promptfoo.get("success") else "fail"
        lines.append(
            f"Promptfoo: {passed} score={promptfoo.get('score')} eval={promptfoo.get('eval_id')}"
        )
        reason = str(promptfoo.get("reason") or "").strip()
        if reason:
            lines.append(f"Promptfoo reason: {reason}")
    else:
        lines.append("Promptfoo: no imported result")
    if human:
        lines.append(
            "Human: "
            f"avg={human.get('average_score')} safety={human.get('safety')} "
            f"reviewer={human.get('reviewer')}"
        )
        notes = str(human.get("notes") or "").strip()
        if notes:
            lines.append(f"Human notes: {notes}")
    else:
        lines.append("Human: no saved review")
    lines.append(f"Slack runs: {slack_runs}")
    lines.append(f"Database: {status.get('database_path')}")
    return "\n".join(lines)


def _format_case_list(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "No eval cases imported yet."
    lines = ["case_id | agent | promptfoo | human_avg | safety"]
    for row in rows:
        promptfoo = "pass" if row.get("latest_promptfoo_success") else "fail"
        if row.get("latest_promptfoo_success") is None:
            promptfoo = "-"
        lines.append(
            " | ".join(
                [
                    str(row.get("case_id") or ""),
                    str(row.get("agent_under_test") or ""),
                    promptfoo,
                    str(row.get("latest_human_average") or "-"),
                    str(row.get("latest_human_safety") or "-"),
                ]
            )
        )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
