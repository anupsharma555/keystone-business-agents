"""Run live-gated search coverage evals."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from keystone_agents.config import require_cli_live_confirmation, with_cli_environment
from keystone_agents.search_coverage_eval import (
    DEFAULT_SEARCH_COVERAGE_CASES_PATH,
    DEFAULT_SEARCH_COVERAGE_OUTPUT_DIR,
    SearchCoverageEvalOptions,
    load_search_coverage_cases,
    normalize_search_coverage_providers,
    render_search_coverage_eval_report,
    run_search_coverage_eval,
    write_search_coverage_artifacts,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare search providers on query-level source coverage cases."
    )
    parser.add_argument(
        "--provider",
        action="append",
        default=None,
        help=(
            "Provider to evaluate. Repeatable. Supported: dry-run, searxng, serper, "
            "firecrawl, tavily, agents-web-search, exa, brave, browserless. Future "
            "providers are eval boundaries until reviewed adapters exist."
        ),
    )
    parser.add_argument(
        "--cases",
        default=str(DEFAULT_SEARCH_COVERAGE_CASES_PATH),
        help="JSONL search coverage eval case file.",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_SEARCH_COVERAGE_OUTPUT_DIR),
        help="Directory for summary, JSONL, and raw result artifacts.",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        default=None,
        help="Run only the selected case id. Repeatable.",
    )
    parser.add_argument(
        "--mode",
        action="append",
        choices=["wide", "focused", "specific"],
        default=None,
        help="Run only cases in the selected mode. Repeatable.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Limit cases after filtering.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Allow live network execution. Requires --no-dry-run.",
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use dry-run mode by default. Pass --no-dry-run with --live for real searches.",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="Repeat each provider/case run to measure stability.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON summary to stdout.")
    parser.add_argument("--markdown", action="store_true", help="Print Markdown summary to stdout.")
    return parser


@with_cli_environment()
def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    providers = normalize_search_coverage_providers(args.provider)
    if args.repeats < 1:
        raise SystemExit("--repeats must be at least 1.")
    if args.live:
        try:
            require_cli_live_confirmation(
                dry_run=args.dry_run,
                live_flag=True,
                flag_name="--live",
                live_action="search coverage evaluation",
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
    elif not args.dry_run:
        raise SystemExit("--no-dry-run requires --live.")

    cases = _filter_cases(
        load_search_coverage_cases(args.cases),
        case_ids=args.case_id,
        modes=args.mode,
        limit=args.limit,
    )
    report = run_search_coverage_eval(
        cases,
        options=SearchCoverageEvalOptions(
            providers=providers,
            live=args.live,
            repeats=args.repeats,
        ),
    )
    artifact_paths = write_search_coverage_artifacts(report, Path(args.output))
    payload = report.model_dump(mode="json")
    payload["artifacts"] = artifact_paths
    if args.json and not args.markdown:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(render_search_coverage_eval_report(report))
        print(f"\nArtifacts: {artifact_paths['summary']}")
    return 0


def _filter_cases(cases, *, case_ids, modes, limit):
    selected = list(cases)
    if case_ids:
        allowed = set(case_ids)
        selected = [case for case in selected if case.id in allowed]
    if modes:
        allowed_modes = set(modes)
        selected = [case for case in selected if case.mode in allowed_modes]
    if limit is not None:
        if limit < 1:
            raise SystemExit("--limit must be at least 1.")
        selected = selected[:limit]
    if not selected:
        raise SystemExit("No search coverage cases matched the requested filters.")
    return selected


if __name__ == "__main__":
    raise SystemExit(main())
