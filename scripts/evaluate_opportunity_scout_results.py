#!/usr/bin/env python3
"""Evaluate saved Opportunity Scout result JSON files without live calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from keystone_agents.opportunity_scout.evaluation import evaluate_opportunity_scout_portfolio


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Saved Opportunity Scout JSON result paths.")
    parser.add_argument(
        "--cases",
        help="Optional portfolio case JSON with required lanes and minimum verified yield.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payloads: list[tuple[str, dict[str, Any]]] = []
    for raw_path in args.paths:
        path = Path(raw_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Opportunity Scout result must be an object: {path}")
        payloads.append((path.stem, payload))
    expected_cases = None
    if args.cases:
        raw_cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
        if not isinstance(raw_cases, list):
            raise ValueError("Opportunity Scout portfolio cases must be a list.")
        expected_cases = {
            str(case["id"]): case for case in raw_cases if isinstance(case, dict) and case.get("id")
        }
    result = evaluate_opportunity_scout_portfolio(
        payloads,
        expected_cases=expected_cases,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
