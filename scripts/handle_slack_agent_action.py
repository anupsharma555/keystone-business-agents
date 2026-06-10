"""Handle Slack selected-message Keystone agent actions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from keystone_agents.operator_failures import known_exception_to_operator_failure
from keystone_agents.slack_action_contract import slack_agent_feedback_event
from keystone_agents.slack_actions import handle_run_agent_interaction
from keystone_agents.storage.sqlite_store import database_url_from_env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Handle a Slack message shortcut or modal submission for Keystone agents."
    )
    parser.add_argument(
        "--payload-file",
        required=True,
        help="Slack interaction payload JSON file.",
    )
    parser.add_argument("--database-url", default=None, help="SQLite URL for WorkItem storage.")
    parser.add_argument(
        "--context-dir",
        default="artifacts/slack_contexts",
        help="Directory for selected Slack context files.",
    )
    parser.add_argument("--live-search", action="store_true", help="Enable live research search.")
    parser.add_argument("--live-sdk", action="store_true", help="Enable live SDK execution.")
    parser.add_argument("--max-results", type=int, default=3, help="Max WorkItem search results.")
    parser.add_argument(
        "--thread-fetch-error",
        default="",
        help="Optional warning to record when the Slack runtime could not fetch thread replies.",
    )
    parser.add_argument(
        "--feedback-jsonl",
        action="store_true",
        help="Stream Slack agent feedback events as JSONL to stderr while the run executes.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    return parser


def _stream_feedback_jsonl(event_type: str, payload: dict) -> None:
    event = slack_agent_feedback_event(event_type, payload)
    print(json.dumps(event, ensure_ascii=True, sort_keys=True), file=sys.stderr, flush=True)


def main() -> int:
    args = build_parser().parse_args()
    try:
        return _main(args)
    except Exception as exc:
        failure = known_exception_to_operator_failure(exc, context="Slack bridge run")
        error_payload = {
            "stage": "work_item",
            "callback_id": "",
            "status": "error",
            "route": "",
            "send_enabled": False,
            "failure": failure.to_dict(),
            "error": {
                "type": type(exc).__name__,
                "message": failure.reason,
            },
            "summary": failure.summary,
            "next_step": failure.next_step,
        }
        if args.feedback_jsonl:
            _stream_feedback_jsonl(
                "agent_error",
                {
                    "error_type": type(exc).__name__,
                    "message": failure.reason,
                    "failure": failure.to_dict(),
                    "summary": failure.summary,
                    "next_step": failure.next_step,
                    "send_enabled": False,
                },
            )
        if args.json:
            print(json.dumps(error_payload, ensure_ascii=True, indent=2, sort_keys=True))
        else:
            print(f"Keystone Slack agent action failed: {type(exc).__name__}: {exc}")
        return 1


def _main(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.payload_file).read_text(encoding="utf-8"))
    result = handle_run_agent_interaction(
        payload,
        database_url=args.database_url or database_url_from_env(),
        context_dir=args.context_dir,
        live_search=args.live_search,
        live_sdk=args.live_sdk,
        max_results=args.max_results,
        thread_fetch_error=args.thread_fetch_error,
        feedback_callback=_stream_feedback_jsonl if args.feedback_jsonl else None,
    )
    payload_out = result.model_dump(mode="json")
    if args.json:
        print(json.dumps(payload_out, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        if result.stage == "modal":
            print("Keystone Slack modal ready.")
            print(f"Context file: {result.context_file_path}")
        else:
            print("Keystone WorkItem started.")
            print(f"WorkItem: {(result.work_item or {}).get('id', '')}")
            print(f"Route: {result.route}")
            print(f"Status: {result.status}")
        for warning in result.warnings:
            print(f"Warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
