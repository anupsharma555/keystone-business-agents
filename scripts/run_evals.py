"""Run Keystone static evals without live model or integration calls."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from keystone_agents.benchmark_tracking import record_eval_summary
from keystone_agents.evals import generate_eval_report, run_static_evals


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run deterministic Keystone agent evals.")
    parser.add_argument(
        "--agent",
        choices=["all", "gmail", "company", "scout", "outreach"],
        default="all",
        help="Eval suite to run.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON summary.")
    parser.add_argument("--markdown", action="store_true", help="Print markdown report.")
    parser.add_argument(
        "--record-benchmark",
        action="store_true",
        help="Record this eval run in the local benchmark SQLite store.",
    )
    parser.add_argument(
        "--benchmark-db",
        default=None,
        help=(
            "Optional benchmark SQLite path. Defaults to KEYSTONE_BENCHMARK_DB or .keystone/state."
        ),
    )
    parser.add_argument(
        "--benchmark-label",
        default="",
        help="Optional label for comparing benchmark runs over time.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_static_evals(agent=args.agent)
    if args.json and not args.markdown:
        print(json.dumps(summary.to_dict(), ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(generate_eval_report(summary))
    if args.record_benchmark:
        record = record_eval_summary(
            summary.to_dict(),
            suite="static",
            db_path=args.benchmark_db,
            run_label=args.benchmark_label,
            entrypoint="scripts/run_evals.py",
            metadata={"agent": args.agent},
        )
        print(f"Recorded benchmark run {record.run_id} in {record.db_path}", file=sys.stderr)
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
