#!/usr/bin/env python3
"""Inspect and update the local Promptfoo eval database."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from promptfoo.eval_dashboard import (
    CASE_DETAIL_LOOKUP_LIMIT,
    dashboard_payload,
    slack_run_post_save_state,
)
from promptfoo.eval_database import (
    DEFAULT_EVAL_DB,
    backfill_slack_manual_run_summaries,
    eval_case_status,
    get_eval_trace_event,
    import_promptfoo_results,
    list_eval_cases,
    list_eval_trace_events,
    record_promptfoo_eval_to_benchmark,
    record_slack_eval_run,
)
from promptfoo.eval_urls import eval_case_bundle_url, eval_dashboard_case_url, eval_review_case_url


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the local Promptfoo eval database.")
    parser.add_argument(
        "--database-path",
        default=str(DEFAULT_EVAL_DB),
        help="SQLite database path. Defaults to the repo-local eval DB.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import-latest", help="Import Promptfoo JSON results.")
    import_parser.add_argument(
        "--results-path",
        default=".keystone/promptfoo/latest-eval.json",
        help="Promptfoo JSON output path.",
    )

    status_parser = subparsers.add_parser("status", help="Show merged status for one case.")
    status_parser.add_argument("--case-id", required=True)
    status_parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")

    list_parser = subparsers.add_parser("list", help="List eval cases.")
    list_parser.add_argument("--limit", type=int, default=50)
    list_parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")

    benchmark_parser = subparsers.add_parser(
        "record-benchmark",
        help="Record imported Promptfoo/Slack eval rows in the benchmark store.",
    )
    benchmark_parser.add_argument("--eval-id", required=True)
    benchmark_parser.add_argument("--benchmark-db", default=None)
    benchmark_parser.add_argument("--run-label", default="")
    benchmark_parser.add_argument(
        "--include-slack",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include linked Slack eval rows for the Promptfoo cases.",
    )

    slack_parser = subparsers.add_parser("record-slack-run", help="Record a real Slack eval run.")
    slack_parser.add_argument("--case-id", required=True)
    slack_parser.add_argument("--run-id", default="")
    slack_parser.add_argument("--agent", default="")
    slack_parser.add_argument("--slack-channel-id", default="C0BA17Y9C01")
    slack_parser.add_argument("--slack-channel-name", default="evals")
    slack_parser.add_argument("--slack-thread-ts", default="")
    slack_parser.add_argument("--request-text", default="")
    slack_parser.add_argument("--result-summary", default="")
    slack_parser.add_argument("--work-item-id", default="")
    slack_parser.add_argument("--permalink", default="")
    slack_parser.add_argument("--route", default="")
    slack_parser.add_argument("--status", default="")
    slack_parser.add_argument("--context-policy", default="")
    slack_parser.add_argument("--thread-fetch-status", default="")
    slack_parser.add_argument("--thread-message-count", type=int, default=0)
    slack_parser.add_argument("--warning", action="append", default=[])
    slack_parser.add_argument("--cost-profile", default="")
    slack_parser.add_argument("--source-count", type=int, default=0)
    slack_parser.add_argument("--visible-source-count", type=int, default=0)
    slack_parser.add_argument("--sdk-estimated-cost-usd", type=float, default=None)
    slack_parser.add_argument("--sdk-cache-hit-rate", type=float, default=None)
    slack_parser.add_argument("--duration-ms", type=float, default=None)
    slack_parser.add_argument("--response-hash", default="")
    slack_parser.add_argument("--model-provider", default="")
    slack_parser.add_argument("--model-name", default="")
    slack_parser.add_argument("--run-mode", default="")
    slack_parser.add_argument("--search-provider", default="")
    slack_parser.add_argument("--search-provider-sequence", action="append", default=[])
    slack_parser.add_argument("--git-revision", default="")
    slack_parser.add_argument("--run-label", default="")
    slack_parser.add_argument("--storage-mode", default="")
    slack_parser.add_argument(
        "--evidence-json",
        default="",
        help="Sanitized JSON object to store as Slack run evidence.",
    )
    slack_parser.add_argument(
        "--evidence-path",
        default="",
        help="Path to a sanitized JSON evidence object.",
    )

    backfill_parser = subparsers.add_parser(
        "backfill-manual-run-summaries",
        help="Create or refresh no-API manual trace summaries for saved Slack eval runs.",
    )
    backfill_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report missing summaries without writing trace rows.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    database_path = Path(args.database_path)
    if args.command == "import-latest":
        summary = import_promptfoo_results(
            args.results_path,
            database_path=database_path,
        )
        print(json.dumps(summary.to_dict(), ensure_ascii=True, indent=2, sort_keys=True))
        return 0

    if args.command == "status":
        case_id = _canonical_case_id(args.case_id, database_path=database_path)
        status = eval_case_status(case_id, database_path=database_path)
        status["readiness"] = _case_status_readiness(
            case_id=case_id,
            status=status,
            database_path=database_path,
        )
        if args.json:
            print(json.dumps(status, ensure_ascii=True, indent=2, sort_keys=True))
        else:
            print(_format_status(status))
        return 0

    if args.command == "list":
        rows = list_eval_cases(database_path=database_path, limit=args.limit)
        if args.json:
            print(json.dumps(rows, ensure_ascii=True, indent=2, sort_keys=True))
        else:
            print(_format_case_list(rows))
        return 0

    if args.command == "record-benchmark":
        payload = record_promptfoo_eval_to_benchmark(
            eval_id=args.eval_id,
            database_path=database_path,
            benchmark_db_path=args.benchmark_db,
            run_label=args.run_label,
            include_slack=args.include_slack,
        )
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0

    if args.command == "record-slack-run":
        evidence = _load_optional_json_object(
            inline=args.evidence_json,
            path=args.evidence_path,
            label="evidence",
        )
        case_id = _canonical_case_id(args.case_id, database_path=database_path)
        row_id = record_slack_eval_run(
            case_id=case_id,
            run_id=args.run_id,
            agent=args.agent,
            work_item_id=args.work_item_id,
            slack_channel_id=args.slack_channel_id,
            slack_channel_name=args.slack_channel_name,
            slack_thread_ts=args.slack_thread_ts,
            permalink=args.permalink,
            request_text=args.request_text or sys.stdin.read(),
            result_summary=args.result_summary,
            route=args.route,
            status=args.status,
            context_policy=args.context_policy,
            thread_fetch_status=args.thread_fetch_status,
            thread_message_count=args.thread_message_count,
            warning_count=len(args.warning),
            warnings=args.warning,
            cost_profile=args.cost_profile,
            source_count=args.source_count,
            visible_source_count=args.visible_source_count,
            sdk_estimated_cost_usd=args.sdk_estimated_cost_usd,
            sdk_cache_hit_rate=args.sdk_cache_hit_rate,
            duration_ms=args.duration_ms,
            response_hash=args.response_hash,
            evidence=evidence,
            model_provider=args.model_provider,
            model_name=args.model_name,
            run_mode=args.run_mode,
            search_provider=args.search_provider,
            search_provider_sequence=args.search_provider_sequence,
            git_revision=args.git_revision,
            run_label=args.run_label,
            storage_mode=args.storage_mode,
            database_path=database_path,
        )
        print(
            json.dumps(
                {
                    "id": row_id,
                    "case_id": case_id,
                    "input_case_id": args.case_id,
                    "run_id": args.run_id,
                    "database_path": str(database_path),
                    **_record_slack_run_post_save_state(
                        case_id=case_id,
                        row_id=row_id,
                        database_path=database_path,
                    ),
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "backfill-manual-run-summaries":
        payload = backfill_slack_manual_run_summaries(
            database_path=database_path,
            dry_run=args.dry_run,
        )
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


def _load_optional_json_object(*, inline: str, path: str, label: str) -> dict[str, object]:
    if inline and path:
        raise ValueError(f"{label}: use either inline JSON or a path, not both")
    raw = ""
    if path:
        raw = Path(path).read_text(encoding="utf-8")
    elif inline:
        raw = inline
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label}: invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label}: JSON must be an object")
    return parsed


def _canonical_case_id(case_id: str, *, database_path: Path) -> str:
    normalized = str(case_id or "").strip()
    if not normalized:
        return normalized
    try:
        payload = dashboard_payload(database_path=database_path)
    except Exception:
        return normalized
    for item in payload.get("cases", []):
        if not isinstance(item, dict):
            continue
        canonical = str(item.get("case_id") or "").strip()
        display = str(item.get("display_case_id") or "").strip()
        if normalized in {canonical, display} and canonical:
            return canonical
    return normalized


def _record_slack_run_post_save_state(
    *,
    case_id: str,
    row_id: int,
    database_path: Path,
) -> dict[str, object]:
    return slack_run_post_save_state(
        case_id=case_id,
        row_id=row_id,
        database_path=database_path,
    )


def _case_status_readiness(
    *,
    case_id: str,
    status: dict[str, object],
    database_path: Path,
) -> dict[str, object]:
    dashboard = dashboard_payload(database_path=database_path, limit=CASE_DETAIL_LOOKUP_LIMIT)
    normalized = str(case_id or "").strip()
    dashboard_case = next(
        (
            item
            for item in dashboard.get("cases", [])
            if isinstance(item, dict) and item.get("case_id") == normalized
        ),
        {},
    )
    follow_up_item = next(
        (
            item
            for item in dashboard.get("follow_up_queue", [])
            if isinstance(item, dict) and item.get("case_id") == normalized
        ),
        {},
    )
    trace_summary = dashboard.get("trace_summary") if isinstance(dashboard.get("trace_summary"), dict) else {}
    trace_visible = any(
        isinstance(item, dict)
        and (
            item.get("join_key") == normalized
            or item.get("group_id") == normalized
        )
        for item in [
            *(trace_summary.get("diagnostic_case_rollups") or []),
            *(trace_summary.get("diagnostic_followups") or []),
        ]
    )
    latest_promptfoo = (
        status.get("latest_promptfoo") if isinstance(status.get("latest_promptfoo"), dict) else {}
    )
    latest_human = (
        status.get("latest_target_human_review")
        if isinstance(status.get("latest_target_human_review"), dict)
        else {}
    )
    slack_run_count = int(status.get("slack_run_count") or 0)
    latest_run_id = str(dashboard_case.get("latest_run_id") or "")
    trace_event_visible = trace_visible or _run_summary_trace_event_visible(
        case_id=normalized,
        run_id=latest_run_id,
        database_path=database_path,
    )
    recorded_response_present = bool(
        str(
            dashboard_case.get("scored_response_text")
            or dashboard_case.get("response_text")
            or dashboard_case.get("latest_slack_summary")
            or ""
        ).strip()
    )
    promptfoo_imported = bool(latest_promptfoo)
    human_review_saved = bool(latest_human)
    analysis_excluded = bool(dashboard_case.get("analysis_excluded"))
    return {
        "case_id": normalized,
        "display_case_id": str(dashboard_case.get("display_case_id") or normalized),
        "dashboard_case_url": eval_dashboard_case_url(normalized),
        "review_case_url": eval_review_case_url(normalized),
        "case_bundle_url": eval_case_bundle_url(normalized),
        "case_visible": bool(dashboard_case),
        "recorded_response_present": recorded_response_present,
        "score_save_enabled": recorded_response_present,
        "promptfoo_imported": promptfoo_imported,
        "slack_run_recorded": slack_run_count > 0,
        "human_review_saved": human_review_saved,
        "analysis_ready": promptfoo_imported and not analysis_excluded,
        "analysis_excluded": analysis_excluded,
        "latest_run_source": str(dashboard_case.get("latest_run_source") or "pending"),
        "latest_run_id": latest_run_id,
        "latest_run_at": str(dashboard_case.get("latest_run_at") or ""),
        "in_follow_up_queue": bool(follow_up_item),
        "follow_up_summary": str(follow_up_item.get("follow_up_summary") or "")
        if isinstance(follow_up_item, dict)
        else "",
        "next_follow_up": str(follow_up_item.get("next_follow_up") or "")
        if isinstance(follow_up_item, dict)
        else str(dashboard_case.get("next_follow_up") or ""),
        "missing_labels": follow_up_item.get("missing_labels") or []
        if isinstance(follow_up_item, dict)
        else [],
        "attention_labels": follow_up_item.get("attention_labels") or []
        if isinstance(follow_up_item, dict)
        else [],
        "trace_event_visible": trace_event_visible,
        "trace_diagnostic_category_visible": trace_visible,
        "trace_diagnostics_visible": trace_event_visible,
    }


def _run_summary_trace_event_visible(
    *,
    case_id: str,
    run_id: str,
    database_path: Path,
) -> bool:
    normalized_case_id = str(case_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    if not normalized_case_id:
        return False
    for event in list_eval_trace_events(database_path=database_path, limit=100):
        if event.get("event_type") not in {"manual_run_summary", "sdk_run_summary"}:
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        correlation = (
            metadata.get("correlation") if isinstance(metadata.get("correlation"), dict) else {}
        )
        event_case_id = str(correlation.get("case_id") or event.get("group_id") or "").strip()
        if event_case_id != normalized_case_id:
            continue
        if not normalized_run_id:
            return True
        event_run_id = str(
            correlation.get("run_id") or correlation.get("work_item_id") or ""
        ).strip()
        if not event_run_id or event_run_id == normalized_run_id:
            return True
    return False


def _manual_run_summary_event(*, row_id: int, database_path: Path) -> dict[str, object]:
    expected_span = f"slack_run_summary:{int(row_id)}"
    return get_eval_trace_event(
        database_path=database_path,
        event_type="manual_run_summary",
        span_id=expected_span,
    )


def _format_status(status: dict[str, object]) -> str:
    case_id = str(status.get("case_id") or "")
    readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
    promptfoo = (
        status.get("latest_promptfoo")
        if isinstance(status.get("latest_promptfoo"), dict)
        else None
    )
    human = (
        status.get("latest_target_human_review")
        if isinstance(status.get("latest_target_human_review"), dict)
        else None
    )
    slack_runs = int(status.get("slack_run_count") or 0)
    lines = [f"Eval case: {case_id}"]
    if promptfoo:
        passed = "pass" if promptfoo.get("success") else "fail"
        lines.append(
            f"Promptfoo: {passed} score={promptfoo.get('score')} eval={promptfoo.get('eval_id')}"
        )
        reason = str(promptfoo.get("reason") or "").strip()
        if reason:
            lines.append(f"Promptfoo reason: {reason}")
    else:
        lines.append("Promptfoo: no imported result")
    if human:
        lines.append(
            "Human: "
            f"avg={human.get('average_score')} safety={human.get('safety')} "
            f"reviewer={human.get('reviewer')}"
        )
        notes = str(human.get("notes") or "").strip()
        if notes:
            lines.append(f"Human notes: {notes}")
    else:
        lines.append("Human: no saved review")
    lines.append(f"Slack runs: {slack_runs}")
    if readiness:
        lines.append(
            "Readiness: "
            f"case_visible={bool(readiness.get('case_visible'))} "
            f"score_save_enabled={bool(readiness.get('score_save_enabled'))} "
            f"analysis_ready={bool(readiness.get('analysis_ready'))} "
            f"trace_visible={bool(readiness.get('trace_diagnostics_visible'))}"
        )
        follow_up = str(readiness.get("follow_up_summary") or readiness.get("next_follow_up") or "").strip()
        if follow_up:
            lines.append(f"Next follow-up: {follow_up}")
        lines.append(f"Dashboard: {readiness.get('dashboard_case_url')}")
        lines.append(f"Review: {readiness.get('review_case_url')}")
        lines.append(f"Case bundle: {readiness.get('case_bundle_url')}")
    lines.append(f"Database: {status.get('database_path')}")
    return "\n".join(lines)


def _format_case_list(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "No eval cases imported yet."
    lines = ["case_id | agent | promptfoo | human_avg | safety"]
    for row in rows:
        promptfoo = "pass" if row.get("latest_promptfoo_success") else "fail"
        if row.get("latest_promptfoo_success") is None:
            promptfoo = "-"
        lines.append(
            " | ".join(
                [
                    str(row.get("case_id") or ""),
                    str(row.get("agent_under_test") or ""),
                    promptfoo,
                    str(row.get("latest_human_average") or "-"),
                    str(row.get("latest_human_safety") or "-"),
                ]
            )
        )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
