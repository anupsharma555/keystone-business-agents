"""Add a human Promptfoo eval review from a Slack thread score block."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from promptfoo.human_review import (
    DEFAULT_REVIEW_DB,
    build_slack_review_template,
    parse_human_review,
    save_human_review,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Store a human Promptfoo eval score in the local review database."
    )
    parser.add_argument("--case-id", default="", help="Promptfoo case id.")
    parser.add_argument("--run-id", default="", help="Promptfoo or Slack agent run id.")
    parser.add_argument("--agent", default="", help="Agent under review.")
    parser.add_argument("--reviewer", default="anup")
    parser.add_argument("--slack-channel-id", default="C0BA17Y9C01")
    parser.add_argument("--slack-channel-name", default="evals")
    parser.add_argument("--slack-thread-ts", default="")
    parser.add_argument(
        "--database-path",
        default=str(DEFAULT_REVIEW_DB),
        help="SQLite path for human eval reviews.",
    )
    parser.add_argument(
        "--text",
        default="",
        help="Slack score block. If omitted, reads from stdin.",
    )
    parser.add_argument(
        "--template",
        action="store_true",
        help="Print a Slack score template instead of saving a review.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.template:
        print(
            build_slack_review_template(
                case_id=args.case_id or "<case_id>",
                run_id=args.run_id,
                agent=args.agent,
            )
        )
        return 0

    text = args.text or sys.stdin.read()
    review = parse_human_review(
        text,
        case_id=args.case_id,
        run_id=args.run_id,
        agent=args.agent,
        reviewer=args.reviewer,
        slack_channel_id=args.slack_channel_id,
        slack_channel_name=args.slack_channel_name,
        slack_thread_ts=args.slack_thread_ts,
    )
    row_id = save_human_review(review, database_path=Path(args.database_path))
    payload = review.to_dict()
    payload["id"] = row_id
    payload["database_path"] = args.database_path
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
