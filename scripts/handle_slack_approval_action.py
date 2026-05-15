"""Apply a Slack interactive approval action to the local approval queue."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from keystone_agents.config import require_cli_live_confirmation, with_cli_environment
from keystone_agents.slack_interactions import handle_slack_approval_interaction


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Handle a Slack approval action payload.")
    parser.add_argument(
        "--payload",
        default=None,
        help="Slack payload JSON or URL-encoded payload=... body.",
    )
    parser.add_argument(
        "--payload-file",
        default=None,
        help="Path to a file containing Slack payload JSON or URL-encoded body.",
    )
    parser.add_argument("--database-url", default=None, help="SQLite URL.")
    parser.add_argument(
        "--live-gmail",
        action="store_true",
        help=(
            "Allow Gmail draft creation only when the approval item explicitly permits "
            "Slack-triggered draft creation. Without this, button clicks only record approval."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use dry-run mode. Pass --no-dry-run with --live-gmail for Gmail draft creation.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON.")
    return parser


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    if args.live_gmail:
        try:
            require_cli_live_confirmation(
                dry_run=args.dry_run,
                live_flag=True,
                flag_name="--live-gmail",
                live_action="creating a Gmail draft from a Slack approval",
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
    elif not args.dry_run:
        raise SystemExit("--no-dry-run requires --live-gmail.")
    if bool(args.payload) == bool(args.payload_file):
        raise SystemExit("Pass exactly one of --payload or --payload-file.")
    raw_payload = (
        Path(args.payload_file).read_text(encoding="utf-8") if args.payload_file else args.payload
    )
    result = handle_slack_approval_interaction(
        raw_payload,
        database_url=args.database_url,
        create_email_draft=args.live_gmail,
        live_gmail=args.live_gmail,
    )
    if args.json:
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=True, indent=2))
    else:
        print(f"{result.approval_id}\t{result.approval_status.value}\t{result.reviewer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
