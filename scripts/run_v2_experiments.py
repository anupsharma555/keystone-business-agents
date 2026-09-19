"""Run isolated KBA V2 controls or explicitly budgeted tool-free model variants."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from keystone_agents.experiments.v2 import (
    apply_human_ratings,
    load_catalog,
    run_experiments,
    write_report,
)
from keystone_agents.schemas.experiment_v2 import ExperimentReport


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List the seven experiments and cases.")
    parser.add_argument("--experiment", default="all")
    parser.add_argument("--case", dest="case_id")
    parser.add_argument("--variant")
    parser.add_argument("--split", choices=["all", "development", "held_out"], default="all")
    parser.add_argument("--live", action="store_true", help="Permit tool-free model calls only.")
    parser.add_argument("--max-model-requests", type=int)
    parser.add_argument("--model", help="Explicit configured OpenAI-compatible model override.")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--review-report", type=Path, help="Import ratings without rerunning models."
    )
    parser.add_argument("--ratings", type=Path)
    args = parser.parse_args(argv)
    if args.list:
        definitions, cases = load_catalog()
        print(
            json.dumps(
                {
                    "experiments": [d.model_dump() for d in definitions],
                    "cases": [{"case_id": c.case_id, "split": c.split} for c in cases],
                },
                indent=2,
            )
        )
        return 0
    if args.review_report or args.ratings:
        if not (args.review_report and args.ratings) or args.live:
            parser.error("Use --review-report and --ratings together without --live.")
        report = ExperimentReport.model_validate_json(args.review_report.read_text())
        report = apply_human_ratings(report, json.loads(args.ratings.read_text()))
    else:
        if args.live:
            if (
                args.experiment == "all"
                or not args.case_id
                or not args.max_model_requests
                or args.max_model_requests <= 0
            ):
                parser.error(
                    "--live requires one --experiment, --case and positive --max-model-requests."
                )
            from keystone_agents.config import load_settings

            load_settings(force_dotenv=True)
        report = run_experiments(
            experiment_id=args.experiment,
            case_id=args.case_id,
            variant=args.variant,
            split=args.split,
            live=args.live,
            max_model_requests=args.max_model_requests,
            model=args.model,
        )
    destination = args.output_dir or Path("artifacts/v2-experiments") / report.run_id
    if (destination / "report.json").exists():
        parser.error("Output report already exists; choose a new directory to preserve evidence.")
    write_report(report, destination)
    print(
        json.dumps(
            {
                "report": str((destination / "report.json").resolve()),
                "mode": report.mode,
                "quality_status": report.quality_status,
                "model_requests": report.model_requests,
                "provider_writes": report.provider_writes,
                "observations": len(report.observations),
            },
            indent=2,
        )
    )
    return 0 if all(row.status == "completed" for row in report.observations) else 1


if __name__ == "__main__":
    raise SystemExit(main())
