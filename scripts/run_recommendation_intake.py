"""Qualify an operator recommendation as a generalized opportunity."""

from __future__ import annotations

import argparse
import json

from keystone_agents.config import with_cli_environment
from keystone_agents.recommendations import (
    qualify_recommendation,
    recommendation_intake_markdown,
    recommendation_orchestrator_route,
    save_recommendation_intake_result,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Qualify a company, person, institute, conference, grant, or other lead."
    )
    parser.add_argument("recommendation", help="Recommendation or lead to qualify.")
    parser.add_argument(
        "--source-link",
        action="append",
        default=[],
        help="Optional source URL supporting the recommendation. Can be passed multiple times.",
    )
    parser.add_argument(
        "--live-search",
        action="store_true",
        help="Mark the intake as requiring live search enrichment downstream.",
    )
    parser.add_argument("--save", action="store_true", help="Persist run and entity memory.")
    parser.add_argument("--database-url", default=None, help="SQLite URL.")
    parser.add_argument("--markdown", action="store_true", help="Print markdown output.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    return parser


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    result = qualify_recommendation(
        args.recommendation,
        source_links=args.source_link,
        live_search=args.live_search,
    )
    payload = result.model_dump(mode="json")
    payload["orchestrator_route"] = recommendation_orchestrator_route(args.recommendation)
    if args.save:
        payload["storage"] = save_recommendation_intake_result(
            result,
            database_url=args.database_url,
        )
    if args.markdown and not args.json:
        print(recommendation_intake_markdown(result))
    else:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
