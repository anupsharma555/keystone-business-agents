"""Append one manual outreach lifecycle tracking record."""

from __future__ import annotations

import argparse
import json
from typing import get_args

from keystone_agents.schemas.outreach import (
    OutreachChannel,
    OutreachLifecycleStatus,
    OutreachOutcome,
    OutreachTrackingRecord,
)
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Append a manual outreach tracking snapshot. This never sends outreach."
    )
    parser.add_argument(
        "--draft-id",
        required=True,
        help="Outreach draft id or external draft ref.",
    )
    parser.add_argument("--company-name", default="")
    parser.add_argument("--contact-name", default="")
    parser.add_argument("--channel", choices=get_args(OutreachChannel), default="email")
    parser.add_argument(
        "--lifecycle-status",
        choices=get_args(OutreachLifecycleStatus),
        default=None,
        help="Lifecycle status. Inferred from --reply-received or --outreach-sent if omitted.",
    )
    parser.add_argument("--outreach-sent", action="store_true")
    parser.add_argument("--sent-at", default="")
    parser.add_argument("--sent-by", default="")
    parser.add_argument("--sent-via", default="")
    parser.add_argument("--reply-received", action="store_true")
    parser.add_argument("--reply-received-at", default="")
    parser.add_argument("--reply-summary", default="")
    parser.add_argument("--outcome", choices=get_args(OutreachOutcome), default="unknown")
    parser.add_argument("--outcome-notes", default="")
    parser.add_argument("--next-step", default="")
    parser.add_argument("--last-checked-at", default="")
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLite URL. Defaults to DATABASE_URL or local DB.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    record = OutreachTrackingRecord(
        draft_id=args.draft_id,
        company_name=args.company_name,
        contact_name=args.contact_name,
        channel=args.channel,
        lifecycle_status=_resolve_lifecycle_status(
            args.lifecycle_status,
            outreach_sent=args.outreach_sent,
            reply_received=args.reply_received,
        ),
        outreach_sent=args.outreach_sent,
        sent_at=args.sent_at,
        sent_by=args.sent_by,
        sent_via=args.sent_via,
        reply_received=args.reply_received,
        reply_received_at=args.reply_received_at,
        reply_summary=args.reply_summary,
        outcome=args.outcome,
        outcome_notes=args.outcome_notes,
        next_step=args.next_step,
        last_checked_at=args.last_checked_at,
    )
    store = SQLiteStore(args.database_url or database_url_from_env())
    row_id = store.save_outreach_tracking(record)
    payload = record.model_dump(mode="json")
    payload["id"] = row_id

    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(
            "\t".join(
                [
                    str(row_id),
                    record.draft_id,
                    record.lifecycle_status,
                    "sent" if record.outreach_sent else "not_sent",
                    "reply" if record.reply_received else "no_reply",
                    record.outcome,
                ]
            )
        )
    return 0


def _resolve_lifecycle_status(
    value: str | None,
    *,
    outreach_sent: bool,
    reply_received: bool,
) -> str:
    if value:
        return value
    if reply_received:
        return "reply_received"
    if outreach_sent:
        return "sent_manually"
    return "not_started"


if __name__ == "__main__":
    raise SystemExit(main())
