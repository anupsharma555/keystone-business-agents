"""Add human feedback for a Keystone agent output."""

from __future__ import annotations

import argparse
import json
from typing import get_args

from keystone_agents.feedback import save_feedback
from keystone_agents.schemas.feedback import FeedbackObjectType, FeedbackRating


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Add feedback for a Keystone agent output.")
    parser.add_argument("--object-type", required=True, choices=get_args(FeedbackObjectType))
    parser.add_argument("--object-id", required=True)
    parser.add_argument("--rating", required=True, choices=get_args(FeedbackRating))
    parser.add_argument("--tag", action="append", default=[], help="Feedback tag. Repeatable.")
    parser.add_argument("--notes", default="")
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLite URL. Defaults to DATABASE_URL.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    record = save_feedback(
        object_type=args.object_type,
        object_id=args.object_id,
        rating=args.rating,
        tags=args.tag,
        notes=args.notes,
        database_url=args.database_url,
    )
    print(json.dumps(record.model_dump(), ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
