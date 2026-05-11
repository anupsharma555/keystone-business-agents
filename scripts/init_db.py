"""Initialize the local Keystone SQLite database."""

from __future__ import annotations

import argparse

from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Initialize Keystone agent SQLite storage.")
    parser.add_argument(
        "--database-url", default=None, help="SQLite URL. Defaults to DATABASE_URL or local DB."
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    store = SQLiteStore(args.database_url or database_url_from_env())
    print(f"Initialized SQLite database: {store.path}")
    print(f"Schema version: {store.current_schema_version()}")
    print(f"Migrations: {', '.join(str(version) for version in store.migration_versions())}")
    print("Tables:")
    for table in sorted(store.table_names()):
        print(f"- {table}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
