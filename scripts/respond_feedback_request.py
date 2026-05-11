"""Answer an approval-linked operator feedback request."""

from __future__ import annotations

import argparse
import json
from typing import get_args

from keystone_agents.feedback import respond_feedback_request
from keystone_agents.schemas.feedback import FeedbackRating


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Respond to an approval-linked operator feedback request."
    )
    parser.add_argument("--approval-id", required=True, help="Approval queue item id.")
    parser.add_argument("--rating", required=True, choices=get_args(FeedbackRating))
    parser.add_argument("--tag", action="append", default=[], help="Feedback tag. Repeatable.")
    parser.add_argument("--notes", default="")
    parser.add_argument("--reviewer", default="", help="Optional reviewer name.")
    parser.add_argument(
        "--no-save-memory",
        action="store_true",
        help="Save the feedback record without writing prompt-safe memory.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLite URL. Defaults to DATABASE_URL.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = respond_feedback_request(
        approval_id=args.approval_id,
        rating=args.rating,
        tags=args.tag,
        notes=args.notes,
        reviewer=args.reviewer,
        save_memory=not args.no_save_memory,
        database_url=args.database_url,
    )
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
