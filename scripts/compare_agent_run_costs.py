#!/usr/bin/env python3
"""Compare repeated Keystone SDK run costs and prompt-cache behavior."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from keystone_agents.cost_experiments import (
    annotate_actual_cost_for_work_item,
    annotate_actual_costs_for_database_selection,
    compare_sdk_cost_records,
    list_sdk_session_run_groups,
    load_sdk_cost_records_from_database,
    load_sdk_cost_records_from_payload_files,
    load_work_item_cost_summary,
    render_sdk_cost_comparison_markdown,
    render_sdk_session_groups_markdown,
    render_work_item_cost_summary_markdown,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare SDK run cache hit rate, cost components, and request fingerprints."
    )
    parser.add_argument(
        "--database-url",
        default="",
        help="SQLite database URL. Defaults to KEYSTONE_DATABASE_URL/local config.",
    )
    parser.add_argument(
        "--run-id",
        action="append",
        default=[],
        help="agent_runs.id to compare. Repeat for first/follow-up runs.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=2,
        help="When --run-id is omitted, compare the latest N agent_runs rows.",
    )
    parser.add_argument(
        "--session-hash",
        default="",
        help="Compare the latest N agent_runs rows with this audit-safe SDK session hash.",
    )
    parser.add_argument(
        "--same-session-as-run-id",
        default="",
        help="Compare the latest N rows sharing the SDK session hash from this agent_runs.id.",
    )
    parser.add_argument(
        "--latest-session",
        action="store_true",
        help="Compare the latest N rows from the newest agent_runs row with SDK session metadata.",
    )
    parser.add_argument(
        "--latest-repeated-session",
        action="store_true",
        help="Compare the latest N rows from the newest SDK session with at least --min-session-runs rows.",
    )
    parser.add_argument(
        "--min-session-runs",
        type=int,
        default=2,
        help="Minimum runs required for --latest-repeated-session or --list-sessions filtering.",
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="List recent audit-safe SDK session groups instead of comparing runs.",
    )
    parser.add_argument(
        "--payload-file",
        action="append",
        default=[],
        help="Saved SDK JSON payload to compare. Repeat in run order.",
    )
    parser.add_argument(
        "--work-item-id",
        default="",
        help="Summarize workflow-level usage events for this WorkItem instead of agent_runs.",
    )
    parser.add_argument(
        "--latest-work-item-cost",
        action="store_true",
        help="Summarize the latest WorkItem with workflow usage events instead of agent_runs.",
    )
    parser.add_argument(
        "--actual-usd",
        action="append",
        default=[],
        help="Operator-provided OpenAI Platform actual USD for the corresponding run.",
    )
    parser.add_argument(
        "--save-actuals",
        action="store_true",
        help="Persist --actual-usd values into selected database agent_runs before comparing.",
    )
    parser.add_argument(
        "--actual-source",
        default="operator_openai_platform",
        help="Source label for persisted actual costs.",
    )
    parser.add_argument(
        "--actual-reference-id",
        default="",
        help="Optional OpenAI Platform request/window id for persisted actual costs.",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Print a concise Markdown report instead of JSON.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.list_sessions:
            if args.payload_file:
                raise SystemExit("error: --list-sessions only supports database-backed agent_runs.")
            groups = list_sdk_session_run_groups(
                database_url=args.database_url or None,
                limit=args.limit,
                min_runs=args.min_session_runs,
            )
            if args.markdown:
                print(render_sdk_session_groups_markdown(groups))
            else:
                print(
                    json.dumps(
                        {"session_groups": groups},
                        ensure_ascii=True,
                        indent=2,
                        sort_keys=True,
                    )
                )
            return 0
        if args.work_item_id or args.latest_work_item_cost:
            if args.payload_file:
                raise SystemExit("error: WorkItem cost summaries do not support --payload-file.")
            if len(args.actual_usd) > 1:
                raise SystemExit("error: WorkItem cost summaries accept at most one --actual-usd.")
            if args.save_actuals:
                if not args.actual_usd:
                    raise SystemExit("error: --save-actuals requires --actual-usd.")
                annotate_actual_cost_for_work_item(
                    database_url=args.database_url or None,
                    work_item_id=args.work_item_id,
                    actual_usd=args.actual_usd[0],
                    actual_source=args.actual_source,
                    actual_reference_id=args.actual_reference_id,
                )
            summary = load_work_item_cost_summary(
                database_url=args.database_url or None,
                work_item_id=args.work_item_id,
                actual_usd=args.actual_usd[0]
                if args.actual_usd and not args.save_actuals
                else None,
                actual_source=args.actual_source,
                actual_reference_id=args.actual_reference_id,
            )
            if args.markdown:
                print(render_work_item_cost_summary_markdown(summary))
            else:
                print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
            return 0
        if args.payload_file:
            if args.save_actuals:
                raise SystemExit("error: --save-actuals only supports database-backed agent_runs.")
            records = load_sdk_cost_records_from_payload_files(
                [Path(path) for path in args.payload_file],
                actual_usd=args.actual_usd,
            )
        else:
            actuals_for_compare = args.actual_usd
            if args.save_actuals:
                annotate_actual_costs_for_database_selection(
                    database_url=args.database_url or None,
                    actual_usd=args.actual_usd,
                    run_ids=args.run_id or None,
                    limit=args.limit,
                    session_hash=args.session_hash,
                    same_session_as_run_id=args.same_session_as_run_id,
                    latest_session=args.latest_session,
                    latest_repeated_session=args.latest_repeated_session,
                    min_session_runs=args.min_session_runs,
                    actual_source=args.actual_source,
                    actual_reference_id=args.actual_reference_id,
                )
                actuals_for_compare = []
            records = load_sdk_cost_records_from_database(
                database_url=args.database_url or None,
                run_ids=args.run_id or None,
                limit=args.limit,
                actual_usd=actuals_for_compare,
                session_hash=args.session_hash,
                same_session_as_run_id=args.same_session_as_run_id,
                latest_session=args.latest_session,
                latest_repeated_session=args.latest_repeated_session,
                min_session_runs=args.min_session_runs,
            )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"error: {exc}") from exc
    summary = compare_sdk_cost_records(records)
    if args.markdown:
        print(render_sdk_cost_comparison_markdown(summary))
    else:
        print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
