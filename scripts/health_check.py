"""Run offline Keystone system health checks."""

from __future__ import annotations

import argparse

from keystone_agents.health import format_health_report, report_to_json, run_health_check

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency is declared, fallback keeps CLI usable.
    load_dotenv = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an offline Keystone health check.")
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLite URL to initialize/check. Defaults to DATABASE_URL or a temp probe.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Optional dotenv file to load before checking. Pass an empty value to skip.",
    )
    parser.add_argument("--json", action="store_true", help="Print the full report as JSON.")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed section output in text mode.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.env_file and load_dotenv is not None:
        load_dotenv(args.env_file)
    report = run_health_check(database_url=args.database_url)
    if args.json:
        print(report_to_json(report))
    else:
        print(format_health_report(report, verbose=args.verbose))
    return 1 if report.overall_status == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
