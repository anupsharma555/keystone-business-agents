"""Run live-gated browser/rendered-page extraction evals."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from keystone_agents.browser_extraction_eval import (
    BASELINE_BROWSER_PROVIDER,
    DEFAULT_BROWSER_EVAL_CASES_PATH,
    DEFAULT_BROWSER_EVAL_OUTPUT_DIR,
    DEFAULT_RENDERED_PAGE_MAX_OUTPUT_CHARS,
    BrowserExtractionEvalOptions,
    load_browser_extraction_cases,
    normalize_browser_providers,
    render_browser_extraction_eval_report,
    run_browser_extraction_eval,
    write_browser_extraction_artifacts,
)
from keystone_agents.config import (
    require_cli_live_confirmation,
    with_cli_environment,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare rendered-page extraction providers on seeded URL cases."
    )
    parser.add_argument(
        "--provider",
        action="append",
        default=None,
        help=(
            "Provider to evaluate. Repeatable. Supported: trafilatura, firecrawl, "
            "browserless, apify, playwright, crawl4ai. The trafilatura baseline is included "
            "by default."
        ),
    )
    parser.add_argument(
        "--baseline-provider",
        default=BASELINE_BROWSER_PROVIDER,
        help="Baseline provider used for improvement deltas.",
    )
    parser.add_argument(
        "--cases",
        default=str(DEFAULT_BROWSER_EVAL_CASES_PATH),
        help="JSONL browser extraction eval case file.",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_BROWSER_EVAL_OUTPUT_DIR),
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
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit the number of cases after filtering.",
    )
    parser.add_argument(
        "--sample-per-mode",
        type=int,
        default=None,
        help="Run the first N cases for each mode after other filters.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Allow live network/browser execution. Requires --no-dry-run.",
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use dry-run mode by default. Pass --no-dry-run with --live for real fetches.",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="Repeat each provider/case run to measure stability.",
    )
    parser.add_argument(
        "--max-output-chars",
        type=int,
        default=DEFAULT_RENDERED_PAGE_MAX_OUTPUT_CHARS,
        help="Maximum text/html characters retained per provider run.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON summary to stdout.")
    parser.add_argument("--markdown", action="store_true", help="Print Markdown summary to stdout.")
    return parser


@with_cli_environment()
def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    providers = normalize_browser_providers(args.provider)
    baseline = normalize_browser_providers([args.baseline_provider])[0]
    if args.repeats < 1:
        raise SystemExit("--repeats must be at least 1.")
    if args.max_output_chars < 1000:
        raise SystemExit("--max-output-chars must be at least 1000.")
    if args.live:
        try:
            require_cli_live_confirmation(
                dry_run=args.dry_run,
                live_flag=True,
                flag_name="--live",
                live_action="browser extraction evaluation",
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
    elif not args.dry_run:
        raise SystemExit("--no-dry-run requires --live.")

    cases = _filter_cases(
        load_browser_extraction_cases(args.cases),
        case_ids=args.case_id,
        modes=args.mode,
        limit=args.limit,
        sample_per_mode=args.sample_per_mode,
    )
    report = run_browser_extraction_eval(
        cases,
        options=BrowserExtractionEvalOptions(
            providers=providers,
            baseline_provider=baseline,
            live=args.live,
            repeats=args.repeats,
            max_output_chars=args.max_output_chars,
        ),
    )
    artifact_paths = write_browser_extraction_artifacts(report, Path(args.output))
    payload = report.model_dump(mode="json")
    payload["artifacts"] = artifact_paths
    if args.json and not args.markdown:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(render_browser_extraction_eval_report(report))
        print(f"\nArtifacts: {artifact_paths['summary']}")
    return 0


def _filter_cases(cases, *, case_ids, modes, limit, sample_per_mode):
    selected = list(cases)
    if case_ids:
        allowed = set(case_ids)
        selected = [case for case in selected if case.id in allowed]
    if modes:
        allowed_modes = set(modes)
        selected = [case for case in selected if case.mode in allowed_modes]
    if sample_per_mode is not None:
        if sample_per_mode < 1:
            raise SystemExit("--sample-per-mode must be at least 1.")
        counts = {"wide": 0, "focused": 0, "specific": 0}
        sampled = []
        for case in selected:
            if counts.get(case.mode, 0) >= sample_per_mode:
                continue
            sampled.append(case)
            counts[case.mode] = counts.get(case.mode, 0) + 1
        selected = sampled
    if limit is not None:
        if limit < 1:
            raise SystemExit("--limit must be at least 1.")
        selected = selected[:limit]
    if not selected:
        raise SystemExit("No browser extraction cases matched the requested filters.")
    return selected


if __name__ == "__main__":
    raise SystemExit(main())
