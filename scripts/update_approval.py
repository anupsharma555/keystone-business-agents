"""Update one local Keystone approval queue item."""

from __future__ import annotations

import argparse
import json

from keystone_agents.schemas.approval import ApprovalQueueStatus
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env

UPDATE_STATUSES = (
    ApprovalQueueStatus.APPROVED,
    ApprovalQueueStatus.REJECTED,
    ApprovalQueueStatus.REVISE,
    ApprovalQueueStatus.ARCHIVED,
    ApprovalQueueStatus.EXPIRED,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Update a Keystone approval queue item.")
    parser.add_argument("approval_id", help="Approval queue item id.")
    parser.add_argument(
        "status",
        choices=[status.value for status in UPDATE_STATUSES],
        help="New approval status.",
    )
    parser.add_argument("--notes", default=None, help="Reviewer notes.")
    parser.add_argument("--reviewer", default=None, help="Reviewer name or email.")
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLite URL. Defaults to DATABASE_URL or local DB.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = SQLiteStore(args.database_url or database_url_from_env())
    item = store.update_approval_status(
        args.approval_id,
        args.status,
        reviewer=args.reviewer,
        notes=args.notes,
    )

    if args.json:
        print(
            json.dumps(
                item.model_dump(mode="json"),
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(f"{item.id}\t{item.approval_status.value}\t{item.title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
