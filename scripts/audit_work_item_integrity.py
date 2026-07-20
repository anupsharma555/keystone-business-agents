#!/usr/bin/env python3
"""Audit WorkItem event and artifact parent integrity without modifying state."""

from __future__ import annotations

import argparse
import json

from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=database_url_from_env())
    parser.add_argument("--id-limit", type=int, default=100)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit nonzero when orphan child rows are present.",
    )
    args = parser.parse_args(argv)
    with SQLiteStore(args.database_url) as store:
        payload = store.audit_work_item_integrity(id_limit=args.id_limit)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if args.strict and payload["status"] != "pass" else 0


if __name__ == "__main__":
    raise SystemExit(main())
