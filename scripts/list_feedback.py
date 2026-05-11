"""List or export human feedback for Keystone agent outputs."""

from __future__ import annotations

import argparse
import json
from typing import get_args

from keystone_agents.feedback import list_feedback
from keystone_agents.reporting import render_markdown_table, safe_export_text
from keystone_agents.schemas.feedback import (
    FeedbackObjectType,
    FeedbackRating,
    FeedbackRecord,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List Keystone feedback records.")
    parser.add_argument("--object-type", choices=get_args(FeedbackObjectType), default=None)
    parser.add_argument("--rating", choices=get_args(FeedbackRating), default=None)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--approval-id", default=None)
    parser.add_argument("--json", action="store_true", help="Print JSON instead of markdown.")
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLite URL. Defaults to DATABASE_URL.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    records = list_feedback(
        object_type=args.object_type,
        rating=args.rating,
        tag=args.tag,
        approval_id=args.approval_id,
        database_url=args.database_url,
    )
    if args.json:
        print(
            json.dumps(
                [_feedback_record_export_dict(record) for record in records],
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(
        _render_feedback_markdown(
            records,
            object_type=args.object_type,
            rating=args.rating,
            tag=args.tag,
            approval_id=args.approval_id,
        )
    )
    return 0


def _feedback_record_export_dict(record: FeedbackRecord) -> dict[str, object]:
    payload = record.model_dump(mode="json")
    payload["notes"] = safe_export_text(record.notes)
    return payload


def _render_feedback_markdown(
    records: list[FeedbackRecord],
    *,
    object_type: str | None = None,
    rating: str | None = None,
    tag: str | None = None,
    approval_id: str | None = None,
) -> str:
    lines = [
        "# Keystone Feedback Export",
        "",
        f"- Records: {len(records)}",
        "- Notes are redacted and truncated for CLI review.",
    ]
    if object_type:
        lines.append(f"- Object type filter: {object_type}")
    if rating:
        lines.append(f"- Rating filter: {rating}")
    if tag:
        lines.append(f"- Tag filter: {tag}")
    if approval_id:
        lines.append(f"- Approval ID filter: {approval_id}")

    lines.extend(["", "## Tag Summary", ""])
    lines.append(
        render_markdown_table(
            ["Tag", "Count"],
            [[tag_name, count] for tag_name, count in _summarize_tags(records).items()],
        )
    )
    lines.extend(["", "## Feedback", ""])
    if not records:
        lines.extend(["No feedback records found.", ""])
    lines.append(
        render_markdown_table(
            [
                "ID",
                "Approval ID",
                "Agent",
                "Stage",
                "Object",
                "Object ID",
                "Rating",
                "Tags",
                "Notes",
                "Created",
            ],
            [
                [
                    record.id,
                    record.approval_id or "",
                    record.source_agent or "",
                    record.review_stage,
                    record.object_type,
                    record.object_id,
                    record.rating,
                    ", ".join(record.tags) if record.tags else "none",
                    record.notes,
                    record.created_at,
                ]
                for record in records
            ],
        )
    )
    return "\n".join(lines)


def _summarize_tags(records: list[FeedbackRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        for tag_name in record.tags:
            counts[tag_name] = counts.get(tag_name, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


if __name__ == "__main__":
    raise SystemExit(main())
