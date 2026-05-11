"""Run Keystone static evals without live model or integration calls."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_static_evals(agent=args.agent)
    if args.json and not args.markdown:
        print(json.dumps(summary.to_dict(), ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(generate_eval_report(summary))
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
