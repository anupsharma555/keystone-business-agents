"""Summarize longitudinal Keystone benchmark results."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from keystone_agents.benchmark_tracking import (
    format_benchmark_summary,
    summarize_benchmarks,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize local Keystone benchmark results.")
    parser.add_argument(
        "--benchmark-db",
        default=None,
        help=(
            "Optional benchmark SQLite path. Defaults to KEYSTONE_BENCHMARK_DB "
            "or .keystone/state."
        ),
    )
    parser.add_argument("--limit-runs", type=int, default=10, help="Recent runs to show.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = summarize_benchmarks(args.benchmark_db, limit_runs=args.limit_runs)
    if args.json:
        print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(format_benchmark_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
