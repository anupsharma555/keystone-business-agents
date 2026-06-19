#!/usr/bin/env python3
"""Run read-only multi-agent automation surfaces for Slack scheduled jobs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from keystone_agents.multi_agent_automations import (
    run_announcements_research_synthesis,
    run_github_repo_opportunities,
    run_meeting_prep_automation,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a Keystone multi-agent automation helper.")
    parser.add_argument(
        "--kind",
        choices=["meeting-prep", "announcements-research", "github-repo-opportunities"],
        required=True,
        help="Automation helper to run.",
    )
    parser.add_argument("--input-json", default="", help="Inline JSON payload.")
    parser.add_argument("--input-file", default="", help="Path to JSON payload.")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--live-sdk", action="store_true")
    parser.add_argument("--live-search", action="store_true")
    parser.add_argument(
        "--database-url",
        default=None,
        help="Optional SQLite database URL for durable application-data persistence.",
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON.")
    parser.add_argument("--max-items", type=int, default=None)
    parser.add_argument("--min-items", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = _load_payload(args)
    if args.kind == "meeting-prep":
        result = run_meeting_prep_automation(
            payload,
            live_sdk=args.live_sdk,
            live_search=args.live_search,
            max_items=args.max_items or 3,
        )
    elif args.kind == "announcements-research":
        result = run_announcements_research_synthesis(
            payload,
            live_sdk=args.live_sdk,
            live_search=args.live_search,
            min_items=args.min_items or 3,
            max_items=args.max_items or 5,
            database_url=args.database_url,
        )
    else:
        result = run_github_repo_opportunities(
            payload,
            live_sdk=args.live_sdk,
            live_search=args.live_search,
            max_items=args.max_items or 4,
        )
    output: dict[str, Any] = result.model_dump(mode="json")
    output["dry_run"] = bool(args.dry_run)
    if args.json:
        print(json.dumps(output, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(result.slack_text)
    return 0


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.input_file:
        return json.loads(Path(args.input_file).read_text())
    if args.input_json:
        return json.loads(args.input_json)
    if not sys.stdin.isatty():
        text = sys.stdin.read().strip()
        if text:
            return json.loads(text)
    return {}


if __name__ == "__main__":
    raise SystemExit(main())
