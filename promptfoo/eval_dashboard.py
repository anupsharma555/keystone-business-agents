"""Static dashboard rendering for the local Promptfoo eval database."""

# ruff: noqa: E501

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from keystone_agents.eval_dashboard_health import eval_dashboard_readiness
from keystone_agents.model_provider import get_trace_config
from promptfoo.eval_database import (
    DEFAULT_EVAL_DB,
    database_table_summaries,
    eval_case_status,
    eval_case_statuses,
    get_eval_trace_event,
    list_eval_cases,
    list_eval_trace_events,
    summarize_eval_trace_events,
)
from promptfoo.eval_urls import (
    eval_case_bundle_url,
    eval_dashboard_case_url,
    eval_review_case_url,
)
from promptfoo.human_review import SCORE_DIMENSIONS
from promptfoo.orchestrator_judge import JUDGE_ENV_FLAG, eval_llm_judge_enabled

DEFAULT_DASHBOARD_PATH = Path(".keystone/promptfoo/dashboard.html")
DEFAULT_START_CASE_ID = "slack_company_research_001"
DEFAULT_PROMPTFOO_TEST_PATHS = (
    Path("promptfoo/tests/slack_research.yaml"),
    Path("promptfoo/tests/slack_retrieval_synthesis.yaml"),
    Path("promptfoo/tests/slack_tool_safety.yaml"),
    Path("promptfoo/tests/slack_agent_coverage.yaml"),
    Path("promptfoo/tests/slack_agent_expansion_15.yaml"),
)
_LAST_SEED_EVAL_CASE_ROWS: tuple[dict[str, Any], ...] = ()
EVAL_CASE_DEFAULT_TARGET_PER_AGENT = 15
EVAL_CASE_AGENT_TARGETS = {
    "chief_of_staff": 20,
    "airtable_context_agent": 2,
    "google_workspace_context_agent": 2,
    "zotero_context_agent": 1,
}
CASE_DETAIL_LOOKUP_LIMIT = 10000
CASE_BUNDLE_PROMPT_CHAR_LIMIT = 2000
CASE_BUNDLE_RESPONSE_CHAR_LIMIT = 4000
PROMPTFOO_SCORING_CONTRACT_FIELDS = {
    "expected_route",
    "expected_status",
    "expected_pack_type",
    "expected_next_action_agent",
    "expected_block_kind",
    "expected_refused",
    "expected_approval_required",
    "expect_slack_context",
    "min_source_count",
    "min_artifact_count",
    "required_source_types",
    "required_source_url_prefixes",
    "forbidden_source_url_prefixes",
    "required_artifact_types",
    "required_context_sources",
    "required_audit_terms",
    "required_payload_terms",
    "forbidden_payload_terms",
    "required_specialist_routes",
    "require_specialist_routes_strict",
    "required_workflow_routes",
    "required_workflow_order",
    "require_readable_summary",
    "require_visible_sources",
    "required_summary_patterns",
    "require_final_synthesis",
    "require_live_flags_false",
    "require_done_status",
    "enforce_no_send",
    "required_terms",
    "forbidden_terms",
    "max_elapsed_seconds",
}
PROMPTFOO_SCORING_CONTRACT_LIST_FIELDS = {
    "required_source_types",
    "required_source_url_prefixes",
    "forbidden_source_url_prefixes",
    "required_artifact_types",
    "required_context_sources",
    "required_audit_terms",
    "required_payload_terms",
    "forbidden_payload_terms",
    "required_specialist_routes",
    "required_workflow_routes",
    "required_workflow_order",
    "required_terms",
    "required_summary_patterns",
    "forbidden_terms",
}
CASE_FOLLOW_UP_STATUS_PRIORITY = {"missing": 0, "attention": 1}
CASE_FOLLOW_UP_LABEL_PRIORITY = {
    "recorded_response": 0,
    "slack_run": 1,
    "slack_thread_evidence": 2,
    "slack_warnings": 3,
    "slack_retry_volume": 4,
    "machine_check": 5,
    "human_review": 6,
    "orchestrator_judge": 7,
    "orchestrator_review_detail": 8,
    "source_visibility": 9,
    "analysis_inclusion": 10,
    "prompt_text": 11,
}
CASE_FOLLOW_UP_LABEL_DISPLAY = {
    "prompt_text": "Prompt text",
    "recorded_response": "Recorded response",
    "slack_run": "Slack run",
    "slack_thread_evidence": "Slack thread evidence",
    "slack_retry_volume": "Slack retry volume",
    "source_visibility": "Source visibility",
    "slack_warnings": "Slack warnings",
    "machine_check": "Machine check",
    "human_review": "Human review",
    "orchestrator_judge": "Orchestrator Review",
    "orchestrator_review_detail": "Orchestrator Review detail",
    "analysis_inclusion": "Analysis inclusion",
}
CORE_EVAL_AGENTS = (
    "opportunity_scout",
    "business_research_analyst",
    "chief_of_staff",
    "gmail_triage",
    "outreach_composer",
    "orchestrator",
    "airtable_context_agent",
    "google_workspace_context_agent",
    "zotero_context_agent",
)
WEB_RETRIEVAL_DIMENSIONS = {
    "search",
    "search_budget",
    "deeper_search",
    "retrieval",
    "retrieval_boundary",
    "retrieval_policy",
    "retrieval_precision",
}
INFORMATION_QUALITY_DIMENSIONS = {
    "claim_extraction",
    "claim_mapping",
    "evidence_mapping",
    "hallucination_control",
    "source_provided",
    "source_relevance",
    "source_sufficiency",
    "source_visibility",
}


def render_dashboard(
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
    output_path: str | Path = DEFAULT_DASHBOARD_PATH,
    limit: int = 500,
) -> Path:
    """Render the merged Promptfoo/Slack/human-review eval dashboard."""

    db_path = Path(database_path)
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dashboard_payload(database_path=db_path, limit=limit)
    out_path.write_text(_dashboard_html(payload), encoding="utf-8")
    return out_path


def dashboard_payload(
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
    limit: int = 500,
) -> dict[str, Any]:
    """Return the merged dashboard data model used by HTML, JSON, and CSV views."""

    db_path = Path(database_path)
    seed_rows = _seed_eval_case_rows()
    rows = _merged_eval_case_rows(db_path, limit=limit, seed_rows=seed_rows)
    statuses = eval_case_statuses(
        [str(row.get("case_id") or "") for row in rows],
        database_path=db_path,
    )
    cases = _with_prompt_numbers([
        _case_dashboard_record(
            row,
            database_path=db_path,
            status=statuses.get(str(row.get("case_id") or "")),
        )
        for row in rows
    ])
    eval_runs = _latest_eval_runs(db_path)
    run_ledger = _eval_run_ledger(db_path)
    analysis_cases = _analysis_included_cases(cases)
    summary = _summary(analysis_cases, eval_runs, total_cases=len(cases), seed_rows=seed_rows)
    dashboard_health = eval_dashboard_readiness(probe_dashboard_url=False)
    trace_summary = _trace_dashboard_summary(db_path)
    data_quality = _data_quality_gates(
        cases=cases,
        run_ledger=run_ledger,
        dashboard_health=dashboard_health,
        trace_summary=trace_summary,
    )
    analysis = _promptfoo_analysis(db_path)
    analysis["latest_run"] = _latest_case_run_summary(cases)
    database_inventory = _database_inventory_contract(cases)
    return {
        "database_path": str(db_path),
        "database_tables": database_table_summaries(db_path),
        "eval_runs": eval_runs,
        "run_ledger": run_ledger,
        "follow_up_queue": _follow_up_queue(cases),
        "analysis": analysis,
        "cases": cases,
        "database_inventory": database_inventory,
        "summary": summary,
        "dashboard_health": dashboard_health,
        "trace_summary": trace_summary,
        "data_quality": data_quality,
        "workflow_readiness": _workflow_readiness(cases, summary),
        "score_dimensions": list(SCORE_DIMENSIONS),
        "orchestrator_judge": {
            "enabled": eval_llm_judge_enabled(),
            "env_flag": JUDGE_ENV_FLAG,
        },
    }


def _database_inventory_contract(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the stable read-only Database-tab contract without review prose."""

    rows: list[dict[str, Any]] = []
    allowed_fields = (
        "prompt_number",
        "display_prompt_number",
        "case_id",
        "display_case_id",
        "agent",
        "dimensions",
        "latest_run_at",
        "latest_run_source",
        "latest_run_id",
        "latest_slack_run_id",
        "latest_slack_thread_ts",
        "promptfoo_eval_id",
        "promptfoo_success",
        "promptfoo_score",
        "slack_run_count",
        "latest_slack_thread_fetch_status",
        "latest_slack_warning_count",
        "latest_slack_cost_profile",
        "latest_slack_source_count",
        "latest_slack_visible_source_count",
        "human_average",
        "human_safety",
        "human_scores",
        "orchestrator_judge_average",
        "orchestrator_judge_safety",
        "orchestrator_judge_scores",
        "orchestrator_judge_created_at",
        "scoring_status",
        "scoring_status_label",
        "scoring_completed_at",
        "analysis_excluded",
        "analysis_exclusion_reason",
        "review_url",
        "case_bundle_url",
    )
    for case in cases:
        rows.append({field: case.get(field) for field in allowed_fields})
    return {
        "schema": "keystone.eval.database_inventory.v1",
        "read_only": True,
        "rows": rows,
        "score_dimensions": list(SCORE_DIMENSIONS),
        "detail_surfaces": ["review_form", "case_bundle", "csv", "json"],
        "excluded_visible_fields": [
            "prompt",
            "response",
            "human_notes",
            "orchestrator_rationale",
            "orchestrator_comment",
        ],
    }


def slack_run_post_save_state(
    *,
    case_id: str,
    row_id: int,
    database_path: str | Path = DEFAULT_EVAL_DB,
    limit: int = CASE_DETAIL_LOOKUP_LIMIT,
) -> dict[str, Any]:
    """Return bounded dashboard visibility state after saving one Slack eval run."""

    normalized = str(case_id or "").strip()
    db_path = Path(database_path)
    payload = dashboard_payload(database_path=db_path, limit=limit)
    status = eval_case_status(normalized, database_path=db_path) if normalized else {}
    latest_slack = (status.get("slack_runs") or [{}])[0] if status.get("slack_runs") else {}
    dashboard_case = next(
        (
            item
            for item in payload.get("cases", [])
            if isinstance(item, dict) and item.get("case_id") == normalized
        ),
        {},
    )
    follow_up_item = next(
        (
            item
            for item in payload.get("follow_up_queue", [])
            if isinstance(item, dict) and item.get("case_id") == normalized
        ),
        {},
    )
    manual_trace = get_eval_trace_event(
        database_path=db_path,
        event_type="manual_run_summary",
        span_id=f"slack_run_summary:{int(row_id or 0)}",
    )
    manual_metadata = (
        manual_trace.get("metadata")
        if isinstance(manual_trace.get("metadata"), dict)
        else {}
    )
    trace_summary = payload.get("trace_summary") if isinstance(payload.get("trace_summary"), dict) else {}
    trace_visible = any(
        isinstance(item, dict)
        and (
            item.get("join_key") == normalized
            or item.get("group_id") == normalized
            or item.get("trace_id") == manual_trace.get("trace_id")
        )
        for item in [
            *(trace_summary.get("diagnostic_case_rollups") or []),
            *(trace_summary.get("diagnostic_followups") or []),
        ]
    )
    saved_run_id = str(latest_slack.get("run_id") or "")
    visible_run_id = str(dashboard_case.get("latest_slack_run_id") or "")
    latest_run_visible = bool(dashboard_case) and int(dashboard_case.get("slack_run_count") or 0) > 0
    if saved_run_id:
        latest_run_visible = latest_run_visible and visible_run_id == saved_run_id
    trace_event_visible = bool(manual_trace) or trace_visible
    return {
        "dashboard_case_url": eval_dashboard_case_url(normalized),
        "review_case_url": eval_review_case_url(normalized),
        "case_bundle_url": eval_case_bundle_url(normalized),
        "dashboard_visibility": {
            "case_visible": bool(dashboard_case),
            "display_case_id": str(dashboard_case.get("display_case_id") or ""),
            "latest_run_visible": latest_run_visible,
            "latest_run_source": str(dashboard_case.get("latest_run_source") or ""),
            "latest_slack_run_id": str(dashboard_case.get("latest_slack_run_id") or ""),
            "in_follow_up_queue": bool(follow_up_item),
            "trace_event_visible": trace_event_visible,
            "trace_diagnostic_category_visible": trace_visible,
            "trace_diagnostics_visible": trace_event_visible,
        },
        "database_tables": payload.get("database_tables") or database_table_summaries(db_path),
        "merged_status": {
            "promptfoo_result_count": int(status.get("promptfoo_result_count") or 0),
            "slack_run_count": int(status.get("slack_run_count") or 0),
            "human_review_count": int(status.get("human_review_count") or 0),
            "latest_slack_run_id": str(latest_slack.get("run_id") or ""),
            "latest_slack_created_at": str(latest_slack.get("created_at") or ""),
        },
        "follow_up": {
            "still_open": bool(follow_up_item),
            "follow_up_summary": (
                str(follow_up_item.get("follow_up_summary") or "")
                if isinstance(follow_up_item, dict)
                else ""
            ),
            "next_follow_up": (
                str(follow_up_item.get("next_follow_up") or "")
                if isinstance(follow_up_item, dict)
                else ""
            ),
            "missing_labels": (
                follow_up_item.get("missing_labels") or []
                if isinstance(follow_up_item, dict)
                else []
            ),
            "attention_labels": (
                follow_up_item.get("attention_labels") or []
                if isinstance(follow_up_item, dict)
                else []
            ),
        },
        "trace": {
            "manual_run_summary_present": bool(manual_trace),
            "trace_id": str(manual_trace.get("trace_id") or ""),
            "span_id": str(manual_trace.get("span_id") or ""),
            "diagnostic_contract": bool(manual_metadata.get("diagnostic_contract")),
            "slack_run_created_at": str(manual_metadata.get("slack_run_created_at") or ""),
        },
        "refresh_endpoints": [
            "/api/status?refresh=1",
            "/api/eval-cases",
            "/api/follow-up-queue",
            "/api/data-quality",
            "/api/eval-run-ledger",
            "/api/trace-diagnostics",
            f"/api/eval-case-bundle?case={quote(normalized, safe='')}",
        ],
    }


def dashboard_case_export_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten dashboard cases into the database/export view shape."""

    rows: list[dict[str, Any]] = []
    for case in payload.get("cases") or []:
        machine_status = (
            "pass"
            if case.get("promptfoo_success") is True
            else "fail"
            if case.get("promptfoo_success") is False
            else "not_run"
        )
        slack_status = "tested" if int(case.get("slack_run_count") or 0) > 0 else "not_run"
        human_status = "reviewed" if case.get("human_average") is not None else "unreviewed"
        judge_status = (
            "scored" if case.get("orchestrator_judge_average") is not None else "unscored"
        )
        human_scores = case.get("human_scores") or {}
        judge_scores = case.get("orchestrator_judge_scores") or {}
        score_fields = {
            f"human_{dimension}": human_scores.get(dimension, "")
            for dimension in SCORE_DIMENSIONS
        }
        judge_score_fields = {
            f"orchestrator_judge_{dimension}": judge_scores.get(dimension, "")
            for dimension in SCORE_DIMENSIONS
        }
        rows.append(
            {
                "prompt_number": _prompt_number_label(str(case.get("prompt_number") or "")),
                "case_id": case.get("case_id") or "",
                "agent": case.get("agent") or "",
                "dimensions": ", ".join(case.get("dimensions") or []),
                "scoring_status": case.get("scoring_status") or "",
                "scoring_status_label": case.get("scoring_status_label") or "",
                "scoring_completed_at": case.get("scoring_completed_at") or "",
                "scoring_review_kinds": ", ".join(case.get("scoring_review_kinds") or []),
                "latest_run_at": case.get("latest_run_at") or "",
                "latest_run_source": case.get("latest_run_source") or "",
                "latest_run_id": case.get("latest_run_id") or "",
                "prompt": case.get("user_input") or "",
                "latest_response": (
                    case.get("scored_response_text")
                    or case.get("response_text")
                    or case.get("latest_slack_summary")
                    or ""
                ),
                "machine_status": machine_status,
                "machine_score_5": _score_out_of_five(case.get("promptfoo_score"), machine=True),
                "promptfoo_eval_id": case.get("promptfoo_eval_id") or "",
                "slack_status": slack_status,
                "slack_run_count": int(case.get("slack_run_count") or 0),
                "latest_slack_run_id": case.get("latest_slack_run_id") or "",
                "latest_slack_thread_ts": case.get("latest_slack_thread_ts") or "",
                "latest_slack_context_policy": case.get("latest_slack_context_policy") or "",
                "latest_slack_thread_fetch_status": case.get("latest_slack_thread_fetch_status") or "",
                "latest_slack_message_count": int(case.get("latest_slack_message_count") or 0),
                "latest_slack_warning_count": int(case.get("latest_slack_warning_count") or 0),
                "latest_slack_cost_profile": case.get("latest_slack_cost_profile") or "",
                "latest_slack_source_count": int(case.get("latest_slack_source_count") or 0),
                "latest_slack_visible_source_count": int(case.get("latest_slack_visible_source_count") or 0),
                "latest_slack_sdk_estimated_cost_usd": case.get("latest_slack_sdk_estimated_cost_usd") or "",
                "latest_slack_sdk_cache_hit_rate": case.get("latest_slack_sdk_cache_hit_rate") or "",
                "human_status": human_status,
                "human_average_5": _score_out_of_five(case.get("human_average")),
                "human_total_score_5": _score_out_of_five(case.get("human_average")),
                "human_safety": case.get("human_safety") or "",
                **score_fields,
                "human_notes": case.get("human_notes") or "",
                "orchestrator_judge_status": judge_status,
                "orchestrator_judge_average_5": _score_out_of_five(
                    case.get("orchestrator_judge_average")
                ),
                "orchestrator_judge_total_score_5": _score_out_of_five(
                    case.get("orchestrator_judge_average")
                ),
                "orchestrator_judge_safety": case.get("orchestrator_judge_safety") or "",
                "orchestrator_judge_created_at": case.get("orchestrator_judge_created_at") or "",
                **judge_score_fields,
                "orchestrator_judge_notes": case.get("orchestrator_judge_notes") or "",
                "orchestrator_judge_run_comment": case.get("orchestrator_judge_run_comment") or "",
                "orchestrator_judge_recommended_next_action": (
                    case.get("orchestrator_judge_recommended_next_action") or ""
                ),
                "analysis_excluded": "yes" if case.get("analysis_excluded") else "no",
                "analysis_exclusion_reason": case.get("analysis_exclusion_reason") or "",
                "updated_at": case.get("updated_at") or "",
            }
        )
    return rows


def dashboard_case_export_csv(payload: dict[str, Any]) -> str:
    """Return CSV for the merged eval case database view."""

    rows = dashboard_case_export_rows(payload)
    fieldnames = [
        "prompt_number",
        "case_id",
        "agent",
        "dimensions",
        "scoring_status",
        "scoring_status_label",
        "scoring_completed_at",
        "scoring_review_kinds",
        "latest_run_at",
        "latest_run_source",
        "latest_run_id",
        "prompt",
        "latest_response",
        "machine_status",
        "machine_score_5",
        "promptfoo_eval_id",
        "slack_status",
        "slack_run_count",
        "latest_slack_run_id",
        "latest_slack_thread_ts",
        "latest_slack_context_policy",
        "latest_slack_thread_fetch_status",
        "latest_slack_message_count",
        "latest_slack_warning_count",
        "latest_slack_cost_profile",
        "latest_slack_source_count",
        "latest_slack_visible_source_count",
        "latest_slack_sdk_estimated_cost_usd",
        "latest_slack_sdk_cache_hit_rate",
        "human_status",
        "human_average_5",
        "human_total_score_5",
        "human_safety",
        *[f"human_{dimension}" for dimension in SCORE_DIMENSIONS],
        "human_notes",
        "orchestrator_judge_status",
        "orchestrator_judge_average_5",
        "orchestrator_judge_total_score_5",
        "orchestrator_judge_safety",
        "orchestrator_judge_created_at",
        *[f"orchestrator_judge_{dimension}" for dimension in SCORE_DIMENSIONS],
        "orchestrator_judge_notes",
        "orchestrator_judge_run_comment",
        "orchestrator_judge_recommended_next_action",
        "analysis_excluded",
        "analysis_exclusion_reason",
        "updated_at",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def dashboard_case_review_bundle(payload: dict[str, Any], case_id: str) -> dict[str, Any]:
    """Return the Codex-review bundle for one normalized eval case."""

    normalized = str(case_id or "").strip()
    if not normalized:
        raise ValueError("case is required")
    case = next(
        (
            item
            for item in payload.get("cases", [])
            if normalized
            in {
                str(item.get("case_id") or ""),
                str(item.get("display_case_id") or ""),
            }
        ),
        None,
    )
    if not case:
        raise ValueError(f"case not found: {normalized}")
    prompt_text = str(case.get("user_input") or "")
    response_text = str(
        case.get("scored_response_text")
        or case.get("response_text")
        or case.get("latest_slack_summary")
        or ""
    )
    trace_diagnostics = _case_bundle_trace_diagnostics(
        payload.get("trace_summary") if isinstance(payload.get("trace_summary"), dict) else {},
        str(case.get("case_id") or ""),
    )
    return {
        "schema": "keystone.eval.case_review_bundle.v1",
        "copy_policy": (
            "Prompt and response fields are bounded excerpts for clipboard review; "
            "trace diagnostics are sanitized category rollups; use dashboard/review URLs "
            "or the local DB if truncated text needs deeper inspection."
        ),
        "case_id": case.get("case_id") or "",
        "display_case_id": case.get("display_case_id") or case.get("case_id") or "",
        "agent": case.get("agent") or "",
        "dimensions": case.get("dimensions") or [],
        "dashboard_url": case.get("dashboard_url") or "",
        "review_url": case.get("review_url") or "",
        "case_bundle_url": eval_case_bundle_url(str(case.get("case_id") or "")),
        "latest_run": {
            "at": case.get("latest_run_at") or "",
            "source": case.get("latest_run_source") or "",
            "id": case.get("latest_run_id") or "",
        },
        "prompt": _text_excerpt(prompt_text, CASE_BUNDLE_PROMPT_CHAR_LIMIT),
        "prompt_chars": len(prompt_text),
        "prompt_truncated": len(prompt_text) > CASE_BUNDLE_PROMPT_CHAR_LIMIT,
        "latest_response": _text_excerpt(response_text, CASE_BUNDLE_RESPONSE_CHAR_LIMIT),
        "latest_response_chars": len(response_text),
        "latest_response_truncated": len(response_text) > CASE_BUNDLE_RESPONSE_CHAR_LIMIT,
        "machine": {
            "status": (
                "pass"
                if case.get("promptfoo_success") is True
                else "fail"
                if case.get("promptfoo_success") is False
                else "not_run"
            ),
            "score_5": _score_out_of_five(case.get("promptfoo_score"), machine=True),
            "eval_id": case.get("promptfoo_eval_id") or "",
            "run_at": case.get("latest_machine_run_at") or "",
            "reason": case.get("promptfoo_reason") or "",
        },
        "slack": {
            "run_count": int(case.get("slack_run_count") or 0),
            "run_id": case.get("latest_slack_run_id") or "",
            "thread_ts": case.get("latest_slack_thread_ts") or "",
            "permalink": case.get("latest_slack_permalink") or "",
            "thread_fetch_status": case.get("latest_slack_thread_fetch_status") or "",
            "message_count": int(case.get("latest_slack_message_count") or 0),
            "source_visibility": (
                f"{int(case.get('latest_slack_visible_source_count') or 0)}/"
                f"{int(case.get('latest_slack_source_count') or 0)}"
            ),
            "warnings": case.get("latest_slack_warnings") or [],
        },
        "human_review": {
            "average_5": _score_out_of_five(case.get("human_average")),
            "safety": case.get("human_safety") or "",
            "created_at": case.get("human_created_at") or "",
            "notes": case.get("human_notes") or "",
            "scores": case.get("human_scores") or {},
        },
        "orchestrator_judge_review": {
            "average_5": _score_out_of_five(case.get("orchestrator_judge_average")),
            "safety": case.get("orchestrator_judge_safety") or "",
            "created_at": case.get("orchestrator_judge_created_at") or "",
            "notes": case.get("orchestrator_judge_notes") or "",
            "run_comment": case.get("orchestrator_judge_run_comment") or "",
            "recommended_next_action": (
                case.get("orchestrator_judge_recommended_next_action") or ""
            ),
            "scores": case.get("orchestrator_judge_scores") or {},
            "dimension_rationales": case.get("orchestrator_judge_dimension_rationales") or {},
        },
        "scoring": {
            "status": case.get("scoring_status") or "",
            "label": case.get("scoring_status_label") or "",
            "completed_at": case.get("scoring_completed_at") or "",
            "review_kinds": case.get("scoring_review_kinds") or [],
        },
        "review_target": case.get("review_target") or {},
        "analysis": {
            "excluded": bool(case.get("analysis_excluded")),
            "reason": case.get("analysis_exclusion_reason") or "",
        },
        "scoring_contract": case.get("scoring_contract") or {},
        "trace_diagnostics": trace_diagnostics,
        "review_checklist": case.get("case_review_checklist") or [],
        "next_follow_up": case.get("next_follow_up") or "",
    }


def _case_bundle_trace_diagnostics(trace_summary: dict[str, Any], case_id: str) -> dict[str, Any]:
    """Return sanitized trace diagnostic rollups for a case review bundle."""

    normalized = str(case_id or "").strip()
    rollup = next(
        (
            item
            for item in trace_summary.get("diagnostic_case_rollups") or []
            if str(item.get("join_key") or item.get("group_id") or "").strip() == normalized
        ),
        {},
    )
    followups = [
        item
        for item in trace_summary.get("diagnostic_followups") or []
        if str(item.get("join_key") or item.get("group_id") or "").strip() == normalized
    ]
    categories = rollup.get("categories") if isinstance(rollup.get("categories"), list) else []
    if not categories and followups:
        category_counts: Counter[str] = Counter()
        category_labels: dict[str, str] = {}
        category_severities: dict[str, str] = {}
        for item in followups:
            for category in item.get("categories") or []:
                key = str(category or "").strip()
                if not key:
                    continue
                category_counts[key] += 1
            for label in item.get("category_labels") or []:
                label_value = str(label or "").strip()
                if label_value:
                    category_labels.setdefault(label_value, label_value)
        categories = [
            {
                "key": key,
                "label": category_labels.get(key, key),
                "count": count,
                "severity": category_severities.get(key, "info"),
            }
            for key, count in category_counts.most_common()
        ]
    return {
        "available": bool(rollup or followups),
        "event_count": int(rollup.get("event_count") or len(followups) or 0),
        "latest_created_at": str(
            rollup.get("latest_created_at")
            or (followups[0].get("created_at") if followups else "")
            or ""
        ),
        "trace_id": str(rollup.get("trace_id") or (followups[0].get("trace_id") if followups else "") or ""),
        "categories": categories,
        "followup_count": len(followups),
        "copy_policy": "Sanitized trace diagnostics only; raw prompts, responses, tool I/O, Slack messages, secrets, and PHI are not included.",
    }


def dashboard_case_review_bundle_text(bundle: dict[str, Any]) -> str:
    """Return a paste-ready Codex review prompt for a case bundle."""

    return (
        "Please review this Keystone eval case and identify any scoring, evidence, "
        "dashboard, or workflow follow-up needed. Use review_checklist and "
        "next_follow_up first.\n\n"
        + json.dumps(bundle, ensure_ascii=True, indent=2, sort_keys=True)
    )


def _text_excerpt(value: str, limit: int) -> str:
    text = str(value or "")
    cap = max(0, int(limit))
    if len(text) <= cap:
        return text
    return text[:cap]


def render_review_form(
    *,
    case_id: str,
    database_path: str | Path = DEFAULT_EVAL_DB,
    output_path: str | Path | None = None,
    limit: int = 500,
) -> str:
    """Render a standalone human-review intake form for one eval case."""

    db_path = Path(database_path)
    rows = _merged_eval_case_rows(db_path, limit=max(int(limit), CASE_DETAIL_LOOKUP_LIMIT))
    cases = _with_prompt_numbers([_case_dashboard_record(row, database_path=db_path) for row in rows])
    normalized = str(case_id or "").strip()
    selected = next(
        (
            case
            for case in cases
            if normalized
            in {
                str(case.get("case_id") or ""),
                str(case.get("display_case_id") or ""),
            }
        ),
        None,
    )
    if selected is None:
        selected = {
            "case_id": normalized,
            "agent": "",
            "dimensions": [],
            "human_scores": {},
            "human_notes": "",
            "human_safety": "",
            "latest_slack_run_id": "",
            "latest_slack_thread_ts": "",
            "user_input": "",
            "response_text": "",
            "review_url": eval_review_case_url(normalized),
        }
    payload = {
        "database_path": str(db_path),
        "case": selected,
        "score_dimensions": list(SCORE_DIMENSIONS),
        "orchestrator_judge": {
            "enabled": eval_llm_judge_enabled(),
            "env_flag": JUDGE_ENV_FLAG,
        },
    }
    html_text = _review_html(payload)
    if output_path:
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html_text, encoding="utf-8")
    return html_text


def _orchestrator_review_detail(review: dict[str, Any]) -> dict[str, Any]:
    """Extract visible Orchestrator Review comments and metric rationales."""

    raw: dict[str, Any] = {}
    raw_text = str(review.get("raw_text") or "").strip()
    if raw_text:
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            raw = parsed
    rationales_raw = raw.get("dimension_rationales")
    rationales = rationales_raw if isinstance(rationales_raw, dict) else {}
    dimension_rationales = {
        dimension: str(rationales.get(dimension) or "").strip()
        for dimension in SCORE_DIMENSIONS
        if str(rationales.get(dimension) or "").strip()
    }
    notes = str(raw.get("notes") or review.get("notes") or "").strip()
    legacy_split = "\n\nDimension rationales:\n"
    if legacy_split in notes:
        notes = notes.split(legacy_split, 1)[0].strip()
    recommended_next_action = str(raw.get("recommended_next_action") or "").strip()
    return {
        "run_comment": notes,
        "dimension_rationales": dimension_rationales,
        "recommended_next_action": recommended_next_action,
        "confidence": raw.get("confidence"),
    }


def _case_dashboard_record(
    row: dict[str, Any],
    *,
    database_path: Path,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    case_id = str(row.get("case_id") or "")
    status = status or eval_case_status(case_id, database_path=database_path)
    row_user_input = str(row.get("user_input") or "")
    status_user_input = str((status.get("case") or {}).get("user_input") or "")
    prompt_changed_since_promptfoo = bool(
        row.get("_promptfoo_stale_for_prompt")
        or (row_user_input and status_user_input and row_user_input != status_user_input)
    )
    latest_promptfoo = {} if prompt_changed_since_promptfoo else status.get("latest_promptfoo") or {}
    latest_human = status.get("latest_target_human_review") or {}
    latest_judge = status.get("latest_target_orchestrator_judge_review") or {}
    latest_judge_detail = _orchestrator_review_detail(latest_judge)
    latest_slack = (status.get("slack_runs") or [{}])[0] if status.get("slack_runs") else {}
    latest_slack_evidence = (
        latest_slack.get("evidence") if isinstance(latest_slack.get("evidence"), dict) else {}
    )
    user_input = row_user_input or status_user_input
    latest_machine_run_at = (
        "" if prompt_changed_since_promptfoo else latest_promptfoo.get("imported_at") or row.get("latest_promptfoo_imported_at") or ""
    )
    latest_human_created_at = str(latest_human.get("created_at") or "")
    latest_judge_created_at = str(latest_judge.get("created_at") or "")
    latest_scorecard_created_at = max(
        (latest_human_created_at, latest_judge_created_at),
        key=_timestamp_sort_key,
    )
    scoring_review_kinds = [
        label
        for label, created_at in (
            ("human", latest_human_created_at),
            ("orchestrator_judge", latest_judge_created_at),
        )
        if created_at
    ]
    scoring_status = "complete" if latest_scorecard_created_at else "missing"
    scoring_status_label = (
        "Scoring complete"
        if latest_scorecard_created_at
        else "Review score missing"
        if _case_has_recorded_response(
            {
                "scored_response_text": _case_response_text(latest_promptfoo, latest_slack),
                "response_text": _case_response_text(latest_promptfoo, latest_slack),
                "latest_slack_summary": latest_slack.get("result_summary") or "",
            }
        )
        else "Review score pending"
    )
    latest_run = _latest_run_summary(
        latest_slack=latest_slack,
        latest_machine_run_at=str(latest_machine_run_at or ""),
        promptfoo_eval_id="" if prompt_changed_since_promptfoo else str(row.get("latest_eval_id") or ""),
        human_created_at=latest_human_created_at,
        orchestrator_judge_created_at=latest_judge_created_at,
    )
    record = {
        "case_id": case_id,
        "agent": str(row.get("agent_under_test") or ""),
        "dimensions": _split_dimensions(str(row.get("eval_dimensions") or "")),
        "source": row.get("source") or "",
        "promptfoo_success": None if prompt_changed_since_promptfoo else _bool_or_none(row.get("latest_promptfoo_success")),
        "promptfoo_score": None if prompt_changed_since_promptfoo else row.get("latest_promptfoo_score"),
        "promptfoo_eval_id": "" if prompt_changed_since_promptfoo else row.get("latest_eval_id") or "",
        "promptfoo_storage_mode": latest_promptfoo.get("storage_mode") or "",
        "promptfoo_prompt_text_hash": latest_promptfoo.get("prompt_text_hash") or "",
        "promptfoo_response_text_hash": latest_promptfoo.get("response_text_hash") or "",
        "promptfoo_run_mode": latest_promptfoo.get("run_mode") or "",
        "latest_machine_run_at": latest_machine_run_at,
        "promptfoo_reason": latest_promptfoo.get("reason") or latest_promptfoo.get("failure_reason") or "",
        "analysis_excluded": bool(
            row.get("latest_analysis_excluded") or latest_promptfoo.get("analysis_excluded")
        ),
        "analysis_exclusion_reason": (
            row.get("latest_analysis_exclusion_reason")
            or latest_promptfoo.get("analysis_exclusion_reason")
            or ""
        ),
        "human_average": latest_human.get("average_score"),
        "human_safety": latest_human.get("safety") or "",
        "human_created_at": latest_human.get("created_at") or "",
        "human_scores": latest_human.get("scores") or {},
        "human_notes": latest_human.get("notes") or "",
        "review_kind": latest_human.get("review_kind") or "",
        "reviewer": latest_human.get("reviewer") or "",
        "orchestrator_judge_average": latest_judge.get("average_score"),
        "orchestrator_judge_safety": latest_judge.get("safety") or "",
        "orchestrator_judge_created_at": latest_judge.get("created_at") or "",
        "orchestrator_judge_scores": latest_judge.get("scores") or {},
        "orchestrator_judge_notes": latest_judge.get("notes") or "",
        "orchestrator_judge_run_comment": latest_judge_detail["run_comment"],
        "orchestrator_judge_dimension_rationales": latest_judge_detail["dimension_rationales"],
        "orchestrator_judge_recommended_next_action": latest_judge_detail[
            "recommended_next_action"
        ],
        "orchestrator_judge_confidence": latest_judge_detail["confidence"],
        "scoring_status": scoring_status,
        "scoring_status_label": scoring_status_label,
        "scoring_completed_at": latest_scorecard_created_at,
        "scoring_review_kinds": scoring_review_kinds,
        "slack_run_count": int(status.get("slack_run_count") or 0),
        "latest_slack_run_id": latest_slack.get("run_id") or "",
        "latest_slack_thread_ts": latest_slack.get("slack_thread_ts") or "",
        "latest_slack_created_at": latest_slack.get("created_at") or "",
        "latest_slack_summary": latest_slack.get("result_summary") or "",
        "latest_slack_permalink": latest_slack.get("permalink") or "",
        "latest_slack_context_policy": latest_slack.get("context_policy") or "",
        "latest_slack_thread_fetch_status": latest_slack.get("thread_fetch_status") or "",
        "latest_slack_message_count": int(latest_slack.get("thread_message_count") or 0),
        "latest_slack_warning_count": int(latest_slack.get("warning_count") or 0),
        "latest_slack_warnings": latest_slack.get("warnings") or [],
        "latest_slack_cost_profile": latest_slack.get("cost_profile") or "",
        "latest_slack_source_count": int(latest_slack.get("source_count") or 0),
        "latest_slack_visible_source_count": int(latest_slack.get("visible_source_count") or 0),
        "source_not_applicable_reason": str(
            latest_slack_evidence.get("source_not_applicable")
            or latest_slack_evidence.get("source_not_applicable_reason")
            or ""
        ).strip(),
        "latest_slack_sdk_estimated_cost_usd": latest_slack.get("sdk_estimated_cost_usd"),
        "latest_slack_sdk_cache_hit_rate": latest_slack.get("sdk_cache_hit_rate"),
        "latest_slack_response_hash": latest_slack.get("response_hash") or "",
        "latest_run_at": latest_run["at"],
        "latest_run_source": latest_run["source"],
        "latest_run_id": latest_run["id"],
        "user_input": user_input,
        "scoring_contract": row.get("scoring_contract") or {},
        "response_text": _case_response_text(latest_promptfoo, latest_slack),
        "dashboard_url": eval_dashboard_case_url(case_id),
        "review_url": eval_review_case_url(case_id),
        "case_bundle_url": eval_case_bundle_url(case_id),
        "updated_at": row.get("updated_at") or "",
    }
    record["review_target"] = _case_review_target(record)
    record["scored_response_text"] = _case_scored_response_text(record)
    record["case_review_checklist"] = _case_review_checklist(record)
    record["next_follow_up"] = _case_next_follow_up(record["case_review_checklist"])
    return record


def _case_review_checklist(case: dict[str, Any]) -> list[dict[str, str]]:
    has_prompt = bool(str(case.get("user_input") or "").strip())
    has_response = _case_has_recorded_response(case)
    slack_runs = int(case.get("slack_run_count") or 0)
    thread_status = str(case.get("latest_slack_thread_fetch_status") or "").strip()
    message_count = int(case.get("latest_slack_message_count") or 0)
    source_count = int(case.get("latest_slack_source_count") or 0)
    visible_source_count = int(case.get("latest_slack_visible_source_count") or 0)
    warning_count = int(case.get("latest_slack_warning_count") or 0)
    source_not_applicable = bool(str(case.get("source_not_applicable_reason") or "").strip())
    source_required = _case_requires_source_visibility(case)
    source_visibility_complete = (
        source_not_applicable
        or (visible_source_count > 0 and (not source_required or source_count > 0))
        or (not source_required and source_count == 0)
    )
    checks = [
        _ledger_check(
            "prompt_text",
            "complete" if has_prompt else "missing",
            "Root prompt text is saved." if has_prompt else "Root prompt text is missing.",
        ),
        _ledger_check(
            "recorded_response",
            "complete" if has_response else "missing",
            (
                "A Promptfoo or Slack response is saved."
                if has_response
                else "No Promptfoo or Slack response is saved; run/import a response before scoring."
            ),
        ),
        _ledger_check(
            "slack_run",
            "complete" if slack_runs else "missing",
            (
                f"{slack_runs} Slack run rows linked to this case."
                if slack_runs
                else "No Slack run is linked; run the case in #evals to capture Slack behavior/evidence."
                if has_response
                else "No Slack run is linked; run in Slack or import a Promptfoo response before scoring."
            ),
        ),
        _ledger_check(
            "slack_retry_volume",
            "attention" if slack_runs > 3 else "complete" if slack_runs else "pending",
            (
                f"{slack_runs} saved Slack rows for this case; review the latest run_id and treat older retries as history."
                if slack_runs > 3
                else "Slack retry volume is small."
                if slack_runs
                else "No Slack retry volume yet."
            ),
        ),
        _ledger_check(
            "slack_thread_evidence",
            "complete" if thread_status and message_count > 0 else "missing" if slack_runs else "pending",
            f"Thread fetch status {thread_status or 'tbd'} with {message_count} messages.",
        ),
        _ledger_check(
            "source_visibility",
            "complete" if source_visibility_complete else "attention",
            (
                f"Source not applicable: {case.get('source_not_applicable_reason')}"
                if source_not_applicable
                else f"{visible_source_count}/{source_count} sources visible in Slack output."
            ),
        ),
        _ledger_check(
            "slack_warnings",
            "attention" if warning_count else "complete",
            f"{warning_count} Slack evidence warnings.",
        ),
        _ledger_check(
            "machine_check",
            "complete" if case.get("promptfoo_success") is not None else "missing",
            (
                "Promptfoo machine-check row is imported."
                if case.get("promptfoo_success") is not None
                else "Import Promptfoo result by case_id before analysis comparison."
            ),
        ),
        _ledger_check(
            "human_review",
            "complete" if case.get("human_average") is not None else "missing" if has_response else "pending",
            (
                "Human scorecard is saved."
                if case.get("human_average") is not None
                else "Submit a human scorecard for the saved response."
                if has_response
                else "Scorecard review is waiting on a recorded response."
            ),
        ),
    ]
    has_orchestrator_review = case.get("orchestrator_judge_average") is not None
    has_orchestrator_review_target = bool(slack_runs) or has_orchestrator_review
    if has_orchestrator_review_target:
        checks.append(
            _ledger_check(
                "orchestrator_judge",
                "complete" if has_orchestrator_review else "pending",
                (
                    "Orchestrator Review scorecard is saved for this #evals output."
                    if has_orchestrator_review
                    else "Optional Orchestrator Review scoring is not saved for this #evals output; manual human scoring can satisfy the review gate until Orchestrator Review is explicitly enabled or selected."
                    if has_response
                    else "Orchestrator Review scoring is waiting on a recorded #evals response."
                ),
            )
        )
        if has_orchestrator_review:
            has_judge_comment = bool(
                str(
                    case.get("orchestrator_judge_run_comment")
                    or case.get("orchestrator_judge_notes")
                    or ""
                ).strip()
            )
            judge_rationales = case.get("orchestrator_judge_dimension_rationales") or {}
            has_judge_rationales = any(
                str(value or "").strip()
                for value in (
                    judge_rationales.values()
                    if isinstance(judge_rationales, dict)
                    else []
                )
            )
            checks.append(
                _ledger_check(
                    "orchestrator_review_detail",
                    "complete" if has_judge_comment and has_judge_rationales else "attention",
                    (
                        "Orchestrator Review run comment and per-metric rationales are visible."
                        if has_judge_comment and has_judge_rationales
                        else "Orchestrator Review score exists, but its run comment or per-metric rationales are not visible in the dashboard."
                    ),
                )
            )
    checks.append(
        _ledger_check(
            "analysis_inclusion",
            "attention" if case.get("analysis_excluded") else "complete" if case.get("promptfoo_eval_id") else "pending",
            (
                f"Excluded from analysis: {case.get('analysis_exclusion_reason') or 'manual exclusion'}"
                if case.get("analysis_excluded")
                else "Included in analysis." if case.get("promptfoo_eval_id") else "Analysis inclusion starts after machine-check import."
            ),
        ),
    )
    return checks


def _case_next_follow_up(checks: list[dict[str, str]]) -> str:
    ordered_labels = [
        "prompt_text",
        "recorded_response",
        "slack_run",
        "slack_thread_evidence",
        "machine_check",
        "human_review",
        "orchestrator_judge",
        "orchestrator_review_detail",
        "source_visibility",
        "slack_warnings",
        "slack_retry_volume",
        "analysis_inclusion",
    ]
    by_label = {str(check.get("label") or ""): check for check in checks}
    for label in ordered_labels:
        check = by_label.get(label)
        if check and check.get("status") in {"missing", "attention"}:
            return str(check.get("detail") or f"Resolve {label}.")
    return "Ready to compare prompt, response, machine score, Orchestrator Review, human review, evidence, and analysis movement."


def _case_open_follow_up_checks(case: dict[str, Any]) -> list[dict[str, str]]:
    return [
        check
        for check in case.get("case_review_checklist") or []
        if str(check.get("status") or "") in {"missing", "attention"}
    ]


def _primary_case_follow_up_check(
    checks: list[dict[str, str]],
    *,
    preferred_label: str = "",
) -> dict[str, str]:
    if not checks:
        return {}
    by_label = {str(check.get("label") or ""): check for check in checks}
    preferred = by_label.get(str(preferred_label or ""))
    if preferred:
        return preferred
    return min(
        checks,
        key=lambda check: (
            CASE_FOLLOW_UP_STATUS_PRIORITY.get(str(check.get("status") or ""), 9),
            CASE_FOLLOW_UP_LABEL_PRIORITY.get(str(check.get("label") or ""), 99),
        ),
    )


def _display_follow_up_label(label: str) -> str:
    text = str(label or "")
    return CASE_FOLLOW_UP_LABEL_DISPLAY.get(text, text.replace("_", " ").strip().title())


def _case_follow_up_status_summary(checks: list[dict[str, str]]) -> str:
    missing = [
        _display_follow_up_label(str(check.get("label") or ""))
        for check in checks
        if str(check.get("status") or "") == "missing"
    ]
    attention = [
        _display_follow_up_label(str(check.get("label") or ""))
        for check in checks
        if str(check.get("status") or "") == "attention"
    ]
    parts = []
    if missing:
        parts.append("Missing: " + ", ".join(missing))
    if attention:
        parts.append("Needs attention: " + ", ".join(attention))
    return " | ".join(parts)


def _case_action_queue_item(
    case: dict[str, Any],
    *,
    reason: str,
    preferred_label: str = "",
) -> dict[str, Any]:
    checks = _case_open_follow_up_checks(case)
    primary = _primary_case_follow_up_check(checks, preferred_label=preferred_label)
    labels = [str(check.get("label") or "") for check in checks if check.get("label")]
    missing_labels = [
        str(check.get("label") or "")
        for check in checks
        if check.get("label") and str(check.get("status") or "") == "missing"
    ]
    attention_labels = [
        str(check.get("label") or "")
        for check in checks
        if check.get("label") and str(check.get("status") or "") == "attention"
    ]
    statuses = {str(check.get("status") or "") for check in checks}
    queue_status = "missing" if "missing" in statuses else "attention" if "attention" in statuses else "complete"
    preferred_detail = str(primary.get("detail") or "")
    next_follow_up = (
        preferred_detail
        if preferred_label and preferred_detail
        else str(case.get("next_follow_up") or primary.get("detail") or reason or "")
    )
    prompt_text = str(case.get("user_input") or "")
    response_text = str(
        case.get("scored_response_text")
        or case.get("response_text")
        or case.get("latest_slack_summary")
        or ""
    )
    return {
        "case_id": str(case.get("case_id") or ""),
        "case_label": str(case.get("case_id") or ""),
        "display_case_id": str(case.get("display_case_id") or case.get("case_id") or ""),
        "display_prompt_number": str(case.get("display_prompt_number") or ""),
        "agent": str(case.get("agent") or ""),
        "latest_run_at": str(case.get("latest_run_at") or ""),
        "latest_run_id": str(case.get("latest_run_id") or case.get("latest_slack_run_id") or ""),
        "latest_run_source": str(case.get("latest_run_source") or "pending"),
        "reason": str(reason or ""),
        "status": queue_status,
        "labels": labels,
        "label_display": [_display_follow_up_label(label) for label in labels],
        "missing_labels": missing_labels,
        "missing_label_display": [_display_follow_up_label(label) for label in missing_labels],
        "attention_labels": attention_labels,
        "attention_label_display": [_display_follow_up_label(label) for label in attention_labels],
        "follow_up_summary": _case_follow_up_status_summary(checks),
        "primary_label": str(primary.get("label") or ""),
        "primary_label_display": _display_follow_up_label(str(primary.get("label") or "")),
        "detail": str(primary.get("detail") or case.get("next_follow_up") or reason or ""),
        "next_follow_up": next_follow_up,
        "prompt_excerpt": _text_excerpt(prompt_text, 240),
        "prompt_chars": len(prompt_text),
        "response_excerpt": _text_excerpt(response_text, 360),
        "response_chars": len(response_text),
        "scoring_status": str(case.get("scoring_status") or ""),
        "scoring_status_label": str(case.get("scoring_status_label") or ""),
        "scoring_completed_at": str(case.get("scoring_completed_at") or ""),
        "scoring_review_kinds": list(case.get("scoring_review_kinds") or []),
        "human_average": case.get("human_average"),
        "human_created_at": str(case.get("human_created_at") or ""),
        "human_notes": str(case.get("human_notes") or ""),
        "orchestrator_judge_average": case.get("orchestrator_judge_average"),
        "orchestrator_judge_created_at": str(case.get("orchestrator_judge_created_at") or ""),
        "orchestrator_judge_notes": str(case.get("orchestrator_judge_notes") or ""),
        "orchestrator_judge_run_comment": str(
            case.get("orchestrator_judge_run_comment") or ""
        ),
        "orchestrator_judge_dimension_rationales": (
            case.get("orchestrator_judge_dimension_rationales") or {}
        ),
        "orchestrator_judge_recommended_next_action": str(
            case.get("orchestrator_judge_recommended_next_action") or ""
        ),
        "dashboard_url": str(case.get("dashboard_url") or ""),
        "review_url": str(case.get("review_url") or ""),
        "case_bundle_url": str(case.get("case_bundle_url") or ""),
    }


def _follow_up_queue(cases: list[dict[str, Any]], *, limit: int = 24) -> list[dict[str, Any]]:
    """Return prioritized current-case follow-ups for the Runs & Scoring workspace."""

    rows: list[dict[str, Any]] = []
    for case in cases:
        if not _case_has_runtime_evidence(case):
            continue
        checks = _case_open_follow_up_checks(case)
        if not checks:
            continue
        row = _case_action_queue_item(case, reason="case follow-up needed")
        row["has_latest_run"] = bool(
            str(
                case.get("latest_run_at")
                or case.get("latest_run_id")
                or case.get("latest_slack_run_id")
                or ""
            ).strip()
        )
        rows.append(row)
    rows.sort(
        key=lambda row: (
            0 if row.get("has_latest_run") else 1,
            _timestamp_sort_key(str(row.get("latest_run_at") or "")) if row.get("has_latest_run") else float("inf"),
            CASE_FOLLOW_UP_STATUS_PRIORITY.get(str(row.get("status") or ""), 9),
            CASE_FOLLOW_UP_LABEL_PRIORITY.get(str(row.get("primary_label") or ""), 99),
            str(row.get("case_id") or ""),
        )
    )
    return rows[: max(1, int(limit))]


def _case_has_runtime_evidence(case: dict[str, Any]) -> bool:
    """Return whether this case has run/review/import state beyond prompt metadata."""

    return bool(
        str(
            case.get("latest_run_at")
            or case.get("latest_run_id")
            or case.get("latest_slack_run_id")
            or case.get("latest_slack_created_at")
            or case.get("promptfoo_eval_id")
            or case.get("human_created_at")
            or case.get("orchestrator_judge_created_at")
            or case.get("scoring_completed_at")
            or ""
        ).strip()
        or int(case.get("slack_run_count") or 0) > 0
        or case.get("promptfoo_success") is not None
        or case.get("human_average") is not None
        or case.get("orchestrator_judge_average") is not None
    )


def _latest_run_summary(
    *,
    latest_slack: dict[str, Any],
    latest_machine_run_at: str,
    promptfoo_eval_id: str,
    human_created_at: str,
    orchestrator_judge_created_at: str = "",
) -> dict[str, str]:
    candidates = [
        (
            "slack",
            str(latest_slack.get("created_at") or ""),
            str(latest_slack.get("run_id") or latest_slack.get("slack_thread_ts") or ""),
        ),
        ("machine", latest_machine_run_at, promptfoo_eval_id),
        ("human_review", human_created_at, "human review"),
        (
            "orchestrator_review",
            orchestrator_judge_created_at,
            "orchestrator review",
        ),
    ]
    available = [
        {"source": source, "at": at, "id": run_id}
        for source, at, run_id in candidates
        if str(at or "").strip()
    ]
    if not available:
        return {"source": "pending", "at": "", "id": ""}
    return max(available, key=lambda item: _timestamp_sort_key(item["at"]))


def _timestamp_sort_key(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def _latest_case_run_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the newest run/review activity using the merged case model."""

    candidates = [
        case
        for case in cases
        if str(case.get("latest_run_at") or case.get("scoring_completed_at") or "").strip()
    ]
    if not candidates:
        return {}
    case = max(
        candidates,
        key=lambda item: max(
            _timestamp_sort_key(str(item.get("latest_run_at") or "")),
            _timestamp_sort_key(str(item.get("scoring_completed_at") or "")),
        ),
    )
    response_text = str(
        case.get("scored_response_text")
        or case.get("response_text")
        or case.get("latest_slack_summary")
        or ""
    )
    prompt_text = str(case.get("user_input") or "")
    return {
        "case_id": case.get("case_id") or "",
        "display_case_id": case.get("display_case_id") or case.get("case_id") or "",
        "agent": case.get("agent") or "",
        "run_id": case.get("latest_run_id") or case.get("latest_slack_run_id") or "",
        "run_source": case.get("latest_run_source") or "",
        "run_at": case.get("latest_run_at") or "",
        "slack_run_id": case.get("latest_slack_run_id") or "",
        "slack_thread_ts": case.get("latest_slack_thread_ts") or "",
        "scoring_status": case.get("scoring_status") or "",
        "scoring_status_label": case.get("scoring_status_label") or "",
        "scoring_completed_at": case.get("scoring_completed_at") or "",
        "human_average": case.get("human_average"),
        "human_safety": case.get("human_safety") or "",
        "human_notes": case.get("human_notes") or "",
        "orchestrator_judge_average": case.get("orchestrator_judge_average"),
        "orchestrator_judge_safety": case.get("orchestrator_judge_safety") or "",
        "orchestrator_judge_notes": case.get("orchestrator_judge_notes") or "",
        "orchestrator_judge_run_comment": case.get("orchestrator_judge_run_comment") or "",
        "orchestrator_judge_dimension_rationales": (
            case.get("orchestrator_judge_dimension_rationales") or {}
        ),
        "orchestrator_judge_recommended_next_action": (
            case.get("orchestrator_judge_recommended_next_action") or ""
        ),
        "orchestrator_judge_created_at": case.get("orchestrator_judge_created_at") or "",
        "prompt_excerpt": _text_excerpt(prompt_text, 280),
        "prompt_chars": len(prompt_text),
        "response_excerpt": _text_excerpt(response_text, 520),
        "response_chars": len(response_text),
        "dashboard_url": case.get("dashboard_url") or "",
        "review_url": case.get("review_url") or "",
        "case_bundle_url": case.get("case_bundle_url") or "",
    }


def _with_prompt_numbers(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    numbered: list[dict[str, Any]] = []
    for case in cases:
        agent = str(case.get("agent") or "unknown")
        counts[agent] += 1
        enriched = dict(case)
        case_id = str(case.get("case_id") or "")
        prompt_number = f"_{counts[agent]:03d}"
        enriched["prompt_number"] = prompt_number
        enriched["display_prompt_number"] = _prompt_number_label(prompt_number)
        enriched["display_case_id"] = _display_case_id(
            case_id,
            prompt_number,
            source=str(case.get("source") or ""),
        )
        numbered.append(enriched)
    return numbered


def _prompt_number_label(prompt_number: str) -> str:
    return prompt_number.lstrip("_") or prompt_number


def _display_case_id(case_id: str, prompt_number: str, *, source: str = "") -> str:
    if source == "slack":
        return case_id
    suffix = prompt_number.lstrip("_")
    if not case_id or not suffix:
        return case_id
    stem, separator, tail = case_id.rpartition("_")
    if separator and len(tail) == 3 and tail.isdigit():
        return f"{stem}_{suffix}"
    return case_id


def _merged_eval_case_rows(
    database_path: Path,
    *,
    limit: int,
    seed_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows = list_eval_cases(database_path=database_path, limit=limit)
    seed_rows = list(seed_rows) if seed_rows is not None else _seed_eval_case_rows()
    seed_case_ids = {str(seed.get("case_id") or "") for seed in seed_rows if seed.get("case_id")}
    by_case_id = {
        str(row.get("case_id") or ""): dict(row)
        for row in rows
        if not (
            row.get("source") in {"promptfoo", "promptfoo_seed"}
            and str(row.get("case_id") or "")
            and str(row.get("case_id") or "") not in seed_case_ids
        )
    }
    for seed in seed_rows:
        case_id = str(seed.get("case_id") or "")
        if not case_id:
            continue
        if case_id in by_case_id:
            existing_user_input = str(by_case_id[case_id].get("user_input") or "")
            seed_user_input = str(seed.get("user_input") or "")
            prompt_changed = bool(seed_user_input and existing_user_input and seed_user_input != existing_user_input)
            by_case_id[case_id].update(
                {
                    "agent_under_test": seed.get("agent_under_test") or by_case_id[case_id].get("agent_under_test") or "",
                    "eval_dimensions": seed.get("eval_dimensions") or by_case_id[case_id].get("eval_dimensions") or "",
                    "source": seed.get("source") or by_case_id[case_id].get("source") or "",
                    "user_input": seed_user_input or by_case_id[case_id].get("user_input") or "",
                    "scoring_contract": seed.get("scoring_contract")
                    or by_case_id[case_id].get("scoring_contract")
                    or {},
                }
            )
            if prompt_changed:
                by_case_id[case_id]["_promptfoo_stale_for_prompt"] = True
        else:
            by_case_id[case_id] = seed
    return sorted(
        by_case_id.values(),
        key=lambda row: (
            str(row.get("agent_under_test") or ""),
            str(row.get("case_id") or ""),
        ),
    )[: max(1, int(limit))]


def _seed_eval_case_rows() -> list[dict[str, Any]]:
    global _LAST_SEED_EVAL_CASE_ROWS
    try:
        signature = _seed_eval_case_file_signature()
        rows = _seed_eval_case_rows_cached(signature)
    except OSError:
        rows = _LAST_SEED_EVAL_CASE_ROWS
    else:
        _LAST_SEED_EVAL_CASE_ROWS = tuple(dict(row) for row in rows)
    return [dict(row) for row in rows]


def _seed_eval_case_file_signature() -> tuple[tuple[str, int, int], ...]:
    signature: list[tuple[str, int, int]] = []
    for path in DEFAULT_PROMPTFOO_TEST_PATHS:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        signature.append((str(path), int(stat.st_mtime_ns), int(stat.st_size)))
    return tuple(signature)


@lru_cache(maxsize=8)
def _seed_eval_case_rows_cached(
    signature: tuple[tuple[str, int, int], ...],
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for path_text, _mtime_ns, _size in signature:
        path = Path(path_text)
        rows.extend(_seed_eval_case_rows_from_text(path.read_text(encoding="utf-8")))
    return tuple(rows)


def _seed_eval_case_rows_from_text(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    loaded = yaml.safe_load(text) or {}
    cases = loaded.get("tests", []) if isinstance(loaded, dict) else loaded
    if not isinstance(cases, list):
        return rows
    for case in cases:
        if not isinstance(case, dict):
            continue
        variables = case.get("vars") if isinstance(case.get("vars"), dict) else {}
        if not variables.get("case_id"):
            continue
        rows.append(
            _seed_eval_case_row(
                {
                    "case_id": str(variables.get("case_id") or ""),
                    "agent_under_test": str(variables.get("agent_under_test") or ""),
                    "eval_dimensions": str(variables.get("eval_dimensions") or ""),
                    "user_input": str(variables.get("user_input") or ""),
                    "scoring_contract": _promptfoo_scoring_contract(variables),
                }
            )
        )
    return rows


def _promptfoo_scoring_contract(variables: dict[str, Any]) -> dict[str, Any]:
    contract = {
        key: _scoring_contract_value(key, variables.get(key))
        for key in sorted(PROMPTFOO_SCORING_CONTRACT_FIELDS)
        if variables.get(key) not in (None, "", [])
    }
    return {
        "schema": "keystone.eval.promptfoo_scoring_contract.v1",
        "assertion": "promptfoo.assertions.kba_slack_invariants",
        "checks": contract,
    }


def _scoring_contract_value(key: str, value: Any) -> Any:
    if key in PROMPTFOO_SCORING_CONTRACT_LIST_FIELDS:
        if isinstance(value, list):
            return [str(part).strip() for part in value if str(part).strip()]
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, str) and "," in value:
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


def _seed_eval_case_row(seed: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": seed.get("case_id") or "",
        "agent_under_test": seed.get("agent_under_test") or "",
        "eval_dimensions": seed.get("eval_dimensions") or "",
        "source": "promptfoo_seed",
        "updated_at": "",
        "latest_eval_id": "",
        "latest_promptfoo_success": None,
        "latest_promptfoo_score": None,
        "latest_human_average": None,
        "latest_human_safety": "",
        "latest_human_created_at": "",
        "user_input": seed.get("user_input") or "",
        "scoring_contract": seed.get("scoring_contract") or {},
    }


def _strip_yaml_scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _summary(
    cases: list[dict[str, Any]],
    eval_runs: list[dict[str, Any]],
    *,
    total_cases: int | None = None,
    seed_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    total = len(cases) if total_cases is None else total_cases
    seed_rows = list(seed_rows) if seed_rows is not None else _seed_eval_case_rows()
    seed_case_ids = {str(row.get("case_id") or "") for row in seed_rows}
    seed_case_total = len(seed_case_ids)
    non_seed_cases = sum(
        1
        for case in cases
        if str(case.get("case_id") or "") not in seed_case_ids
    )
    promptfoo_passed = sum(1 for case in cases if case.get("promptfoo_success") is True)
    promptfoo_failed = sum(1 for case in cases if case.get("promptfoo_success") is False)
    promptfoo_evaluated = promptfoo_passed + promptfoo_failed
    human_scored = [case for case in cases if case.get("human_average") is not None]
    orchestrator_judge_scored = [
        case for case in cases if case.get("orchestrator_judge_average") is not None
    ]
    slack_linked = sum(1 for case in cases if int(case.get("slack_run_count") or 0) > 0)
    slack_run_rows = sum(int(case.get("slack_run_count") or 0) for case in cases)
    slack_retry_cases = sum(1 for case in cases if int(case.get("slack_run_count") or 0) > 3)
    slack_evidence_ready = sum(
        1
        for case in cases
        if int(case.get("slack_run_count") or 0) > 0
        and str(case.get("latest_slack_thread_fetch_status") or "").strip()
        and _case_source_visibility_complete(case)
    )
    slack_with_warnings = sum(
        1 for case in cases if int(case.get("latest_slack_warning_count") or 0) > 0
    )
    safety = Counter(str(case.get("human_safety") or "unreviewed") for case in cases)
    agents = Counter(str(case.get("agent") or "unknown") for case in cases)
    seed_agents = Counter(
        str(case.get("agent_under_test") or "unknown")
        for case in seed_rows
    )
    dimensions: Counter[str] = Counter()
    for case in cases:
        dimensions.update(_dimension_summary_label(dimension) for dimension in (case.get("dimensions") or []))
    human_avg = None
    if human_scored:
        human_avg = round(
            sum(float(case.get("human_average") or 0) for case in human_scored)
            / len(human_scored),
            2,
        )
    machine_scored = [case for case in cases if case.get("promptfoo_score") is not None]
    machine_avg = _average_case_score(machine_scored, "promptfoo_score")
    machine_agent_scores = _agent_score_summary(machine_scored, "promptfoo_score")
    human_agent_scores = _agent_score_summary(human_scored, "human_average")
    orchestrator_judge_avg = _average_case_score(
        orchestrator_judge_scored,
        "orchestrator_judge_average",
    )
    orchestrator_judge_agent_scores = _agent_score_summary(
        orchestrator_judge_scored,
        "orchestrator_judge_average",
    )
    coverage = _coverage_summary(seed_agents)
    return {
        "total_cases": total,
        "seed_case_total": seed_case_total,
        "non_seed_cases": non_seed_cases,
        "promptfoo_passed": promptfoo_passed,
        "promptfoo_failed": promptfoo_failed,
        "promptfoo_evaluated": promptfoo_evaluated,
        "promptfoo_pending": total - promptfoo_evaluated,
        "promptfoo_pass_rate": (
            round(promptfoo_passed / promptfoo_evaluated * 100, 1)
            if promptfoo_evaluated
            else 0
        ),
        "latest_eval_average_score": (
            eval_runs[0].get("average_score") if eval_runs and eval_runs[0].get("average_score") is not None
            else machine_avg
        ),
        "machine_average": machine_avg,
        "machine_scored": len(machine_scored),
        "machine_agent_scores": machine_agent_scores,
        "human_reviewed": len(human_scored),
        "human_average": human_avg,
        "human_agent_scores": human_agent_scores,
        "orchestrator_judge_reviewed": len(orchestrator_judge_scored),
        "orchestrator_judge_average": orchestrator_judge_avg,
        "orchestrator_judge_agent_scores": orchestrator_judge_agent_scores,
        "slack_linked": slack_linked,
        "slack_run_rows": slack_run_rows,
        "slack_retry_cases": slack_retry_cases,
        "slack_evidence_ready": slack_evidence_ready,
        "slack_with_warnings": slack_with_warnings,
        "safety": dict(safety),
        "agents": dict(agents),
        "coverage": coverage,
        "coverage_source": "promptfoo seed cases",
        "coverage_target_label": "core 15; Chief 20; context starter set",
        "coverage_complete_agents": sum(1 for item in coverage.values() if item["gap"] == 0),
        "coverage_gap_total": sum(item["gap"] for item in coverage.values()),
        "dimensions": dict(dimensions.most_common(12)),
        "latest_eval_id": eval_runs[0]["eval_id"] if eval_runs else "",
    }


def _analysis_included_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [case for case in cases if not case.get("analysis_excluded")]


def _case_requires_source_visibility(case: dict[str, Any]) -> bool:
    dimensions = {str(item or "") for item in case.get("dimensions") or []}
    scoring_contract = case.get("scoring_contract") if isinstance(case.get("scoring_contract"), dict) else {}
    checks = scoring_contract.get("checks") if isinstance(scoring_contract.get("checks"), dict) else {}
    return bool(
        dimensions & WEB_RETRIEVAL_DIMENSIONS
        or dimensions & INFORMATION_QUALITY_DIMENSIONS
        or dimensions & {"search_quality", "source_quality"}
        or int(checks.get("min_source_count") or 0) > 0
        or checks.get("require_visible_sources") is True
    )


def _case_source_visibility_complete(case: dict[str, Any]) -> bool:
    source_required = _case_requires_source_visibility(case)
    source_not_applicable = bool(str(case.get("source_not_applicable_reason") or "").strip())
    source_count = int(case.get("latest_slack_source_count") or 0)
    visible_source_count = int(case.get("latest_slack_visible_source_count") or 0)
    return bool(
        source_not_applicable
        or (visible_source_count > 0 and (not source_required or source_count > 0))
        or (not source_required and source_count == 0)
    )


def _workflow_readiness(
    cases: list[dict[str, Any]],
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Return the local-only Slack eval workflow contract for UI and future hooks."""

    total = len(cases)
    prompt_library = int(summary.get("seed_case_total") or total or 0)
    slack_case_count = sum(1 for case in cases if int(case.get("slack_run_count") or 0) > 0)
    slack_run_rows = sum(int(case.get("slack_run_count") or 0) for case in cases)
    recorded_responses = sum(1 for case in cases if _case_has_recorded_response(case))
    human_scorecards = sum(
        1
        for case in cases
        if case.get("human_average") is not None
    )
    orchestrator_judge_scorecards = sum(
        1
        for case in cases
        if case.get("orchestrator_judge_average") is not None
    )
    review_scorecards = sum(
        1
        for case in cases
        if case.get("human_average") is not None
        or case.get("orchestrator_judge_average") is not None
    )
    analysis_included = sum(
        1
        for case in cases
        if case.get("promptfoo_eval_id") and not case.get("analysis_excluded")
    )
    retrieval_source_cases = sum(
        1
        for case in cases
        if any(
            str(dimension or "") in WEB_RETRIEVAL_DIMENSIONS
            or str(dimension or "") in INFORMATION_QUALITY_DIMENSIONS
            or str(dimension or "") in {"search_quality", "source_quality"}
            for dimension in (case.get("dimensions") or [])
        )
    )
    return {
        "mode": "local_preview",
        "live_api_calls": False,
        "starter_run_plan": _starter_run_plan(cases),
        "counts": {
            "prompt_library_cases": prompt_library,
            "total_cases": total,
            "slack_thread_runs": slack_run_rows,
            "slack_thread_cases": slack_case_count,
            "recorded_responses": recorded_responses,
            "human_scorecards": human_scorecards,
            "orchestrator_judge_scorecards": orchestrator_judge_scorecards,
            "review_scorecards": review_scorecards,
            "analysis_included": analysis_included,
            "retrieval_source_cases": retrieval_source_cases,
        },
        "interactions": [
            {
                "interaction": "Prompt copy",
                "current_behavior": "Local prompt library row only",
                "future_live_trigger": "Human pastes root message into #evals",
                "cost_guardrail": "No API call from dashboard copy",
                "stored_evidence": f"{total} local cases",
                "live_api_call_now": False,
            },
            {
                "interaction": "Agent thread reply",
                "current_behavior": "Previewed from saved response/result text",
                "future_live_trigger": "Slack app mention in the selected thread",
                "cost_guardrail": "One agent run per accepted Slack root prompt",
                "stored_evidence": f"{slack_run_rows} saved Slack run rows across {slack_case_count} cases",
                "live_api_call_now": False,
            },
            {
                "interaction": "Promptfoo machine summary",
                "current_behavior": "Shows latest imported assertion status and score by case_id",
                "future_live_trigger": "Resolved Slack case has an imported Promptfoo result",
                "cost_guardrail": "Use imported Promptfoo result by case_id; do not rerun Promptfoo from Slack thread",
                "stored_evidence": f"{analysis_included} included machine rows",
                "live_api_call_now": False,
            },
            {
                "interaction": "Retrieval/source evidence",
                "current_behavior": "Shows tags and recorded evidence already in the eval database",
                "future_live_trigger": "Accepted agent run needs source retrieval",
                "cost_guardrail": "Use dry-run fixtures/cache first; cap live retrieval to accepted root run",
                "stored_evidence": f"{retrieval_source_cases} retrieval/source-tagged cases",
                "live_api_call_now": False,
            },
            {
                "interaction": "Slack review form open",
                "current_behavior": "Review controls stay disabled until a response exists",
                "future_live_trigger": "Human opens the Slack-linked score form from the eval thread",
                "cost_guardrail": "No model call; form uses saved case, run id, thread, and response context",
                "stored_evidence": f"{recorded_responses} recorded responses",
                "live_api_call_now": False,
            },
            {
                "interaction": "Submit Evaluation",
                "current_behavior": "Manual submission and Orchestrator Review write score fields and notes to the local eval database as separate review kinds",
                "future_live_trigger": "Human presses Submit Evaluation after scoring in Slack, or enabled Orchestrator Review scores the saved run",
                "cost_guardrail": "Manual submit uses no model call and no Slack post; Orchestrator Review runs only when explicitly enabled, then refreshes Database, Runs & Scoring, and Analysis from saved rows",
                "stored_evidence": f"{review_scorecards} saved review scorecards ({human_scorecards} manual, {orchestrator_judge_scorecards} Orchestrator Review)",
                "live_api_call_now": False,
            },
            *(
                [
                    {
                        "interaction": "Orchestrator Review scoring",
                        "current_behavior": "Explicitly enabled Orchestrator Review fills the same backend scorecard for saved #evals Slack runs",
                        "future_live_trigger": "After a #evals review link is posted, the operator or enabled server action runs the Orchestrator Review scorer",
                        "cost_guardrail": "Requires KEYSTONE_EVAL_LLM_JUDGE=true or explicit live SDK run_config; no Slack post, send, or external write",
                        "stored_evidence": f"{orchestrator_judge_scorecards} saved Orchestrator Review scorecards",
                        "live_api_call_now": False,
                    }
                ]
                if eval_llm_judge_enabled() or orchestrator_judge_scorecards
                else []
            ),
            {
                "interaction": "Analysis inclusion",
                "current_behavior": "Included unless marked duplicate/problem run",
                "future_live_trigger": "Operator toggles analysis inclusion",
                "cost_guardrail": "No rerun; recalculates from database rows",
                "stored_evidence": f"{analysis_included} included machine rows",
                "live_api_call_now": False,
            },
        ],
    }


def _data_quality_gates(
    *,
    cases: list[dict[str, Any]],
    run_ledger: list[dict[str, Any]],
    dashboard_health: dict[str, Any],
    trace_summary: dict[str, Any],
) -> dict[str, Any]:
    """Return local readiness checks that should be reviewed before paid eval runs."""

    total_cases = len(cases)
    slack_cases = [case for case in cases if int(case.get("slack_run_count") or 0) > 0]
    completed_cases = [case for case in cases if str(case.get("latest_run_source") or "") != "pending"]
    recorded_response_cases = [case for case in cases if _case_has_recorded_response(case)]
    review_scorecard_cases = [
        case
        for case in cases
        if case.get("human_average") is not None
        or case.get("orchestrator_judge_average") is not None
    ]
    api_promptfoo_cases = [
        case
        for case in cases
        if str(case.get("promptfoo_run_mode") or "").strip() == "live_sdk"
    ]
    human_ready_cases = [
        case
        for case in cases
        if _case_has_recorded_response(case) and case.get("human_average") is None
    ]
    source_visibility_cases = [
        case
        for case in cases
        if _case_requires_source_visibility(case)
        and int(case.get("slack_run_count") or 0) > 0
    ]
    checks = [
        _quality_check(
            "dashboard_runtime",
            "Scoring runtime",
            1
            if dashboard_health.get("dashboard_reachable") and dashboard_health.get("manager_exists")
            else 0,
            1,
            "Local scoring manager and HTTP endpoint are reachable.",
            "Start or restart the eval scoring manager before live eval review.",
            severity="blocker",
        ),
        _quality_check(
            "prompt_text",
            "Prompt text",
            sum(1 for case in cases if str(case.get("user_input") or "").strip()),
            total_cases,
            "Every case should preserve the root prompt text.",
            "Backfill prompt text before running or scoring the case.",
            severity="blocker",
        ),
        _quality_check(
            "run_timestamps",
            "Run timestamps",
            sum(1 for case in completed_cases if str(case.get("latest_run_at") or "").strip()),
            len(completed_cases),
            "Completed rows should have a latest run timestamp.",
            "Fix missing created_at/imported_at fields so timeline analysis is reliable.",
            severity="blocker",
        ),
        _quality_check(
            "run_ids",
            "Run identifiers",
            sum(1 for case in completed_cases if str(case.get("latest_run_id") or "").strip()),
            len(completed_cases),
            "Completed rows should have a run, thread, trace, or eval id.",
            "Ensure Slack/API writes include work_item_id, thread_ts, or eval_id.",
            severity="blocker",
        ),
        _quality_check(
            "recorded_responses",
            "Recorded responses",
            len(recorded_response_cases),
            total_cases,
            "Cases need a saved response before review scoring.",
            "Run the eval or import/save the response before opening review.",
            severity="warn",
        ),
        _quality_check(
            "slack_thread_evidence",
            "Slack thread evidence",
            sum(
                1
                for case in slack_cases
                if str(case.get("latest_slack_thread_fetch_status") or "").strip()
                and int(case.get("latest_slack_message_count") or 0) > 0
            ),
            len(slack_cases),
            "Saved Slack runs should record thread fetch status and message count.",
            "Capture selected-message/thread window metadata when saving Slack eval runs.",
            severity="warn",
        ),
        _quality_check(
            "source_visibility",
            "Source visibility",
            sum(1 for case in source_visibility_cases if _case_source_visibility_complete(case)),
            len(source_visibility_cases),
            "Source/retrieval-tagged Slack cases have visible sources or an explicit not-applicable reason.",
            "Backfill source_count/visible_source_count or record an explicit source-not-applicable reason for source-tagged Slack cases.",
            severity="warn",
        ),
        _quality_check(
            "slack_retry_volume",
            "Slack retry volume",
            sum(1 for case in slack_cases if int(case.get("slack_run_count") or 0) <= 3),
            len(slack_cases),
            "Slack-linked cases should have low retry noise so the latest run is easy to review.",
            "High retry volume: use the latest run_id for review and treat older rows as history before analysis.",
            severity="warn",
        ),
        _quality_check(
            "machine_checks",
            "Machine checks",
            sum(1 for case in cases if case.get("promptfoo_success") is not None),
            total_cases,
            "Imported Promptfoo rows should be present for machine assertions.",
            "Import Promptfoo results by case_id before analysis comparisons.",
            severity="warn",
        ),
        _quality_check(
            "review_scorecards",
            "Review scorecards",
            len(review_scorecard_cases),
            len(recorded_response_cases),
            "Recorded responses should receive manual human or Orchestrator Review scoring when they are candidates for fixes.",
            "Use the scoring form or Orchestrator Review after a response is saved.",
            severity="warn",
        ),
        _quality_check(
            "trace_processor",
            "Trace processor",
            1 if trace_summary.get("enabled") else 0,
            1,
            "Self-tracing is explicitly enabled for future SDK eval runs.",
            "Set KEYSTONE_TRACE_PROCESSOR=eval_summary and keep KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA=false before paid API evals.",
            severity="warn",
        ),
        _quality_check(
            "trace_sensitive_capture",
            "Trace sensitive capture",
            1 if trace_summary.get("effective_sensitive_capture") is False else 0,
            1,
            "Effective SDK trace config avoids sensitive prompt/response capture.",
            "Set KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA=false before paid API evals.",
            severity="blocker",
        ),
        _quality_check(
            "trace_run_summaries",
            "Trace run summaries",
            1
            if int(trace_summary.get("sdk_run_summary_count") or 0) > 0
            and int(trace_summary.get("unjoined_sdk_run_summary_count") or 0) == 0
            else 0,
            1,
            "SDK run summaries are saved and joined to eval cases or WorkItems.",
            "Future API evals should persist joined sdk_run_summary rows for run-level diagnosis. Manual no-API summaries only verify local joins.",
            severity="warn",
        ),
        _quality_check(
            "api_safe_storage",
            "API-safe storage",
            sum(
                1
                for case in api_promptfoo_cases
                if case.get("promptfoo_storage_mode") == "api_redacted"
                and case.get("promptfoo_prompt_text_hash")
                and case.get("promptfoo_response_text_hash")
            ),
            len(api_promptfoo_cases),
            "Live/API Promptfoo rows store redacted summaries and hashes instead of raw prompt/response payloads.",
            "Import live/API results with api_redacted storage before dashboard/export review.",
            severity="warn",
        ),
        _quality_check(
            "run_ledger",
            "Run ledger",
            len(run_ledger),
            1,
            "At least one local event is present in the chronological run ledger.",
            "Import or save a local eval event before relying on timeline analysis.",
            severity="warn",
            pass_when_any=True,
        ),
    ]
    blocker_count = sum(1 for check in checks if check["status"] != "complete" and check["severity"] == "blocker")
    warning_count = sum(1 for check in checks if check["status"] != "complete" and check["severity"] == "warn")
    pending_count = blocker_count + warning_count
    review_blocked_cases = [
        _case_action_queue_item(
            case,
            reason="missing recorded response",
            preferred_label="recorded_response",
        )
        for case in cases
        if not _case_has_recorded_response(case)
    ]
    human_review_queue = [
        _case_action_queue_item(
            case,
            reason="response saved but human review missing",
            preferred_label="human_review",
        )
        for case in human_ready_cases
    ]
    return {
        "mode": "local_preflight",
        "live_api_calls": False,
        "ready_for_paid_api_runs": pending_count == 0,
        "blocker_count": blocker_count,
        "warning_count": warning_count,
        "pending_count": pending_count,
        "pass_count": sum(1 for check in checks if check["status"] == "complete"),
        "total_checks": len(checks),
        "review_blocked_count": len(review_blocked_cases),
        "review_blocked_cases": review_blocked_cases[:20],
        "human_review_queue_count": len(human_review_queue),
        "human_review_queue": human_review_queue[:20],
        "checks": checks,
    }


def _quality_check(
    key: str,
    label: str,
    passed: int,
    total: int,
    pass_detail: str,
    fail_detail: str,
    *,
    severity: str,
    pass_when_any: bool = False,
) -> dict[str, Any]:
    normalized_total = max(0, int(total or 0))
    normalized_passed = max(0, int(passed or 0))
    if pass_when_any:
        status = "complete" if normalized_passed > 0 else "pending"
        normalized_total = normalized_passed if normalized_passed > 0 else 1
    elif normalized_total == 0:
        status = "complete"
    else:
        status = "complete" if normalized_passed >= normalized_total else "pending"
    missing = max(0, normalized_total - normalized_passed)
    return {
        "key": key,
        "label": label,
        "status": status,
        "severity": severity,
        "passed": normalized_passed,
        "total": normalized_total,
        "missing": missing,
        "detail": pass_detail if status == "complete" else fail_detail,
    }


def _trace_dashboard_summary(database_path: Path) -> dict[str, Any]:
    """Return local self-tracing readiness and recent safe events."""

    events = [
        _with_trace_agentic_summary(event)
        for event in list_eval_trace_events(database_path=database_path, limit=20)
    ]
    totals = summarize_eval_trace_events(database_path=database_path)
    configured_mode = os.environ.get("KEYSTONE_TRACE_PROCESSOR", "").strip().lower()
    trace_config = get_trace_config()
    return {
        "mode": configured_mode or "disabled",
        "enabled": configured_mode == "eval_summary",
        "database_path": str(database_path),
        **totals,
        "recent_event_count": len(events),
        "recent_events": events,
        "latest_run_trace": _latest_trace_run_summary(events, database_path=database_path),
        "effective_sensitive_capture": bool(trace_config.trace_include_sensitive_data),
        "effective_tracing_disabled": bool(trace_config.tracing_disabled),
        "sensitive_data_env": os.environ.get("KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA", "").strip().lower() or "default_false",
        "sensitive_data_recommended": "false",
        "stored_fields": [
            "event_type",
            "trace_id",
            "span_id",
            "parent_id",
            "workflow/span name",
            "group_id",
            "sanitized metadata",
            "diagnostic_contract and diagnostic_summary",
            "categorical trace diagnostic rollups",
            "sdk_run_summary run/case/work item join metadata",
            "duration_ms for completed spans/traces",
            "created_at",
        ],
        "dropped_fields": [
            "raw prompts",
            "model responses",
            "tool inputs",
            "tool outputs",
            "raw Slack messages",
            "secrets and tokens",
            "PHI or patient-specific content",
        ],
        "recommended_env": {
            "KEYSTONE_TRACE_PROCESSOR": "eval_summary",
            "KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA": "false",
            "KEYSTONE_TRACE_SUMMARY_DB": str(database_path),
        },
        "implementation_notes": [
            "Register KeystoneEvalTraceProcessor with add_trace_processor so OpenAI default tracing can remain separate.",
            "Persist only sanitized trace/span metadata to eval_trace_events.",
            "Persist one sdk_run_summary row per SDK run so API evals can be joined by case, run, or WorkItem without raw model I/O.",
            "Use diagnostic_contract and diagnostic_summary for chartable categories; keep verbose replay details in structured logs.",
            "Use case_id, agent, route, work_item_id, eval_id, and channel metadata for joins instead of raw prompt or response text.",
            "Call flush_traces only after a unit of work closes when a worker needs immediate export guarantees.",
        ],
    }


def _trace_metadata_object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _trace_metadata_int(metadata: dict[str, Any], *paths: tuple[str, ...]) -> int:
    for path in paths:
        value: Any = metadata
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            number = 0
        if number:
            return max(0, number)
    return 0


def _trace_metadata_string_list(value: Any, *, limit: int = 3) -> list[str]:
    if not isinstance(value, list):
        return []
    labels: list[str] = []
    for item in value:
        label = ""
        if isinstance(item, dict):
            label = str(item.get("name") or item.get("tool_name") or item.get("label") or item.get("key") or "")
        else:
            label = str(item or "")
        label = label.strip()
        if label:
            labels.append(label[:80])
        if len(labels) >= limit:
            break
    return labels


def _trace_agentic_summary(event: dict[str, Any]) -> dict[str, Any]:
    """Condense sanitized trace metadata into run-diagnostic fields."""

    metadata = _trace_metadata_object(event.get("metadata"))
    correlation = _trace_metadata_object(metadata.get("correlation"))
    tooling = _trace_metadata_object(metadata.get("tooling"))
    retrieval = _trace_metadata_object(metadata.get("retrieval"))
    retrieval_provider = _trace_metadata_object(metadata.get("retrieval_provider_summary"))
    model = _trace_metadata_object(metadata.get("model"))
    orchestrator = _trace_metadata_object(metadata.get("orchestrator"))
    approval = _trace_metadata_object(metadata.get("approval"))
    diagnostics = _trace_metadata_object(metadata.get("diagnostic_summary"))
    thread_evidence = _trace_metadata_object(metadata.get("thread_evidence"))
    slack_context = _trace_metadata_object(metadata.get("slack_context"))
    tool_count = _trace_metadata_int(metadata, ("tooling", "tool_call_count"), ("tool_call_count",))
    child_step_count = _trace_metadata_int(metadata, ("child_steps", "count"))
    failed_tool_count = _trace_metadata_int(metadata, ("tooling", "failed_tool_call_count"))
    tool_names = _trace_metadata_string_list(tooling.get("tool_names"))
    visible_sources = _trace_metadata_int(
        metadata,
        ("retrieval", "visible_source_count"),
        ("source_visibility", "visible_source_count"),
    )
    source_count = _trace_metadata_int(
        metadata,
        ("retrieval", "source_count"),
        ("source_visibility", "source_count"),
    )
    warning_count = _trace_metadata_int(
        metadata,
        ("error_retry", "warning_count"),
        ("thread_evidence", "warning_count"),
    )
    retry_count = _trace_metadata_int(metadata, ("error_retry", "retry_count"))
    route = str(
        orchestrator.get("selected_route")
        or metadata.get("route")
        or metadata.get("agent")
        or metadata.get("agent_name")
        or correlation.get("route")
        or ""
    ).strip()
    model_text = " ".join(
        value
        for value in (
            str(model.get("provider") or metadata.get("model_provider") or "").strip(),
            str(model.get("name") or metadata.get("model_name") or "").strip(),
        )
        if value
    )
    provider = str(
        retrieval.get("search_provider")
        or retrieval_provider.get("provider_summary")
        or metadata.get("search_provider")
        or ""
    ).strip()
    retrieval_text = (
        f"{visible_sources}/{source_count} visible sources"
        if source_count
        else f"{visible_sources} visible sources"
        if visible_sources
        else ""
    )
    if provider:
        retrieval_text = f"{retrieval_text} via {provider}".strip() if retrieval_text else f"provider {provider}"
    tool_text = (
        f"{tool_count} tool call{'s' if tool_count != 1 else ''}"
        if tool_count
        else ""
    )
    if failed_tool_count:
        tool_text = (
            f"{tool_text} · {failed_tool_count} failed"
            if tool_text
            else f"{failed_tool_count} failed tool call{'s' if failed_tool_count != 1 else ''}"
        )
    if tool_names:
        tool_text = f"{tool_text} · {', '.join(tool_names)}" if tool_text else ", ".join(tool_names)
    orchestrator_bits = [
        "preflight" if orchestrator.get("has_preflight") else "",
        "review" if orchestrator.get("has_review") else "",
        f"{_trace_metadata_int(metadata, ('orchestrator', 'blocker_count'))} blockers"
        if _trace_metadata_int(metadata, ("orchestrator", "blocker_count"))
        else "",
    ]
    orchestrator_text = ", ".join(bit for bit in orchestrator_bits if bit)
    errors_text = ", ".join(
        bit
        for bit in (
            f"{warning_count} warnings" if warning_count else "",
            f"{retry_count} retries" if retry_count else "",
            "error/retry" if diagnostics.get("has_error_or_retry") else "",
        )
        if bit
    )
    if failed_tool_count:
        signal = f"{failed_tool_count} failed tool call{'s' if failed_tool_count != 1 else ''}"
    elif tool_count:
        signal = f"{tool_count} tool call{'s' if tool_count != 1 else ''}"
    elif visible_sources or source_count:
        signal = retrieval_text
    elif orchestrator_text:
        signal = f"orchestrator {orchestrator_text}"
    elif child_step_count:
        signal = f"{child_step_count} child steps"
    elif model_text:
        signal = "model configured"
    elif warning_count or retry_count:
        signal = errors_text
    else:
        signal = "metadata only"
    return {
        "signal": signal,
        "route": route,
        "model": model_text or "model metadata pending",
        "tools": tool_text or "tool metadata pending",
        "child_steps": (
            f"{child_step_count} bounded steps"
            if child_step_count
            else "child-step metadata pending"
        ),
        "retrieval": retrieval_text or "retrieval metadata pending",
        "orchestrator": orchestrator_text or "orchestrator metadata pending",
        "approval": str(approval.get("status") or "approval not required").strip(),
        "errors": errors_text or "no warnings/retries recorded",
        "slack_channel": str(
            slack_context.get("channel_name")
            or correlation.get("slack_channel_name")
            or slack_context.get("channel_id")
            or correlation.get("slack_channel_id")
            or ""
        ).strip(),
        "thread": (
            f"{int(thread_evidence.get('message_count') or 0)} messages"
            if thread_evidence
            else ""
        ),
    }


def _trace_field_readiness(event: dict[str, Any]) -> dict[str, Any]:
    """Summarize whether a sanitized trace event has the expected run diagnostics."""

    metadata = _trace_metadata_object(event.get("metadata"))
    diagnostics = _trace_metadata_object(metadata.get("diagnostic_summary"))
    execution = _trace_metadata_object(metadata.get("execution"))
    model = _trace_metadata_object(metadata.get("model"))
    tooling = _trace_metadata_object(metadata.get("tooling"))
    retrieval = _trace_metadata_object(metadata.get("retrieval"))
    orchestrator = _trace_metadata_object(metadata.get("orchestrator"))
    prompt_version = _trace_metadata_object(metadata.get("prompt_version"))
    cost = _trace_metadata_object(metadata.get("cost"))
    source_visibility = _trace_metadata_object(metadata.get("source_visibility"))
    duration_ms = event.get("duration_ms")
    if duration_ms in (None, ""):
        duration_ms = execution.get("duration_ms")
    checks = [
        {
            "key": "duration",
            "label": "Duration",
            "complete": duration_ms not in (None, ""),
            "detail": "Trace has run/span duration." if duration_ms not in (None, "") else "Duration is not yet captured for this run.",
        },
        {
            "key": "model",
            "label": "Model",
            "complete": bool(
                diagnostics.get("has_model_metadata")
                or model.get("provider")
                or model.get("name")
                or metadata.get("model_provider")
                or metadata.get("model_name")
            ),
            "detail": "Model provider/name is present.",
        },
        {
            "key": "tooling",
            "label": "Tooling",
            "complete": bool(
                diagnostics.get("has_tool_metadata")
                or _trace_metadata_int(metadata, ("tooling", "tool_call_count"))
                or _trace_metadata_int(metadata, ("tooling", "failed_tool_call_count"))
                or _trace_metadata_string_list(tooling.get("tool_names"))
            ),
            "detail": "Tool call count, names, or failure state is present.",
        },
        {
            "key": "child_steps",
            "label": "Child-step timeline",
            "complete": bool(
                diagnostics.get("has_child_step_metadata")
                or _trace_metadata_int(metadata, ("child_steps", "count"))
                or tooling.get("child_step_summary")
            ),
            "detail": "Bounded ordered WorkItem/tool steps are present.",
        },
        {
            "key": "retrieval",
            "label": "Retrieval",
            "complete": bool(
                diagnostics.get("has_retrieval_metadata")
                or retrieval.get("search_provider")
                or retrieval.get("source_count")
                or source_visibility.get("source_count")
            ),
            "detail": "Retrieval provider or source visibility is present.",
        },
        {
            "key": "orchestrator",
            "label": "Orchestrator",
            "complete": bool(
                diagnostics.get("has_orchestrator_feedback")
                or orchestrator.get("has_preflight")
                or orchestrator.get("has_review")
                or _trace_metadata_int(metadata, ("orchestrator", "feedback_count"))
                or _trace_metadata_int(metadata, ("orchestrator", "blocker_count"))
            ),
            "detail": "Orchestrator preflight/review feedback is present.",
        },
        {
            "key": "prompt_version",
            "label": "Prompt/config version",
            "complete": bool(
                prompt_version.get("prompt_metadata_hash")
                or prompt_version.get("request_text_hash")
                or prompt_version.get("response_hash")
                or prompt_version.get("static_prefix_sha256")
                or prompt_version.get("dynamic_prompt_sha256")
                or _trace_metadata_int(metadata, ("prompt_version", "prompt_version_count"))
            ),
            "detail": "Prompt/config fingerprint or response hash is present.",
        },
        {
            "key": "cost",
            "label": "Cost/cache",
            "complete": bool(
                cost.get("cost_profile")
                or cost.get("sdk_estimated_cost_usd") is not None
                or cost.get("sdk_cache_hit_rate") is not None
                or _trace_metadata_object(metadata.get("cost_summary")).get("estimated_usd") is not None
                or _trace_metadata_object(metadata.get("token_summary")).get("cache_hit_rate") is not None
            ),
            "detail": "Cost profile, estimate, or cache information is present.",
        },
        {
            "key": "api_sdk_summary",
            "label": "API SDK summary",
            "complete": str(event.get("event_type") or "") == "sdk_run_summary",
            "detail": "SDK run summary row is present for API-ready eval tracing.",
        },
    ]
    normalized: list[dict[str, str]] = []
    missing: list[str] = []
    for check in checks:
        complete = bool(check.get("complete"))
        if not complete:
            missing.append(str(check["label"]))
        detail = str(check.get("detail") or "")
        if not complete and detail.endswith(" is present."):
            detail = detail.replace(" is present.", " is missing.")
        normalized.append(
            {
                "key": str(check["key"]),
                "label": str(check["label"]),
                "status": "complete" if complete else "attention",
                "detail": detail,
            }
        )
    return {
        "checks": normalized,
        "missing": missing,
        "complete_count": len(checks) - len(missing),
        "attention_count": len(missing),
        "summary": (
            "All core run diagnostics populated."
            if not missing
            else "Missing: " + ", ".join(missing)
        ),
    }


def _with_trace_agentic_summary(event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event)
    payload["agentic_summary"] = _trace_agentic_summary(payload)
    payload["field_readiness"] = _trace_field_readiness(payload)
    return payload


def _latest_trace_run_summary(
    events: list[dict[str, Any]],
    *,
    database_path: Path | None = None,
) -> dict[str, Any]:
    """Return the newest trace event with useful run/case join metadata."""

    if not events:
        return {}
    event = events[0]
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    correlation = metadata.get("correlation") if isinstance(metadata.get("correlation"), dict) else {}
    diagnostics = metadata.get("diagnostic_summary")
    categories: list[str] = []
    if isinstance(diagnostics, dict):
        raw_categories = diagnostics.get("categories") or diagnostics.get("category_counts") or []
        if isinstance(raw_categories, list):
            for item in raw_categories:
                if isinstance(item, dict):
                    label = str(item.get("label") or item.get("key") or "").strip()
                    if label:
                        categories.append(label)
                else:
                    label = str(item or "").strip()
                    if label:
                        categories.append(label)
    summary = {
        "event_type": event.get("event_type") or "",
        "name": event.get("name") or "",
        "trace_id": event.get("trace_id") or "",
        "span_id": event.get("span_id") or "",
        "case_id": (
            event.get("group_id")
            or metadata.get("case_id")
            or correlation.get("case_id")
            or ""
        ),
        "run_id": (
            metadata.get("run_id")
            or correlation.get("run_id")
            or metadata.get("work_item_id")
            or correlation.get("work_item_id")
            or event.get("trace_id")
            or ""
        ),
        "slack_thread_ts": (
            metadata.get("slack_thread_ts")
            or correlation.get("slack_thread_ts")
            or metadata.get("thread_ts")
            or correlation.get("thread_ts")
            or ""
        ),
        "slack_channel_id": (
            metadata.get("slack_channel_id")
            or correlation.get("slack_channel_id")
            or _trace_metadata_object(metadata.get("slack_context")).get("channel_id")
            or ""
        ),
        "slack_channel_name": (
            metadata.get("slack_channel_name")
            or correlation.get("slack_channel_name")
            or _trace_metadata_object(metadata.get("slack_context")).get("channel_name")
            or ""
        ),
        "agent": metadata.get("agent") or metadata.get("agent_name") or metadata.get("route") or "",
        "route": metadata.get("route") or "",
        "created_at": event.get("created_at") or "",
        "duration_ms": event.get("duration_ms")
        if event.get("duration_ms") not in (None, "")
        else _trace_metadata_object(metadata.get("execution")).get("duration_ms"),
        "categories": categories[:8],
        "scoring_status_label": "",
        "scoring_completed_at": "",
        "human_average": None,
        "orchestrator_judge_average": None,
        "orchestrator_judge_notes": "",
        "orchestrator_judge_run_comment": "",
        "agentic_summary": event.get("agentic_summary") or _trace_agentic_summary(event),
        "field_readiness": event.get("field_readiness") or _trace_field_readiness(event),
    }
    if database_path is not None and summary["case_id"]:
        try:
            status = eval_case_status(str(summary["case_id"]), database_path=database_path)
        except (OSError, ValueError, sqlite3.Error):
            status = {}
        case_status = (
            _case_dashboard_record(
                {"case_id": summary["case_id"]},
                database_path=database_path,
                status=status,
            )
            if status
            else {}
        )
        if case_status:
            summary["scoring_status_label"] = str(case_status.get("scoring_status_label") or "")
            summary["scoring_completed_at"] = str(case_status.get("scoring_completed_at") or "")
            summary["human_average"] = case_status.get("human_average")
            summary["orchestrator_judge_average"] = case_status.get("orchestrator_judge_average")
            summary["orchestrator_judge_notes"] = str(case_status.get("orchestrator_judge_notes") or "")
            summary["orchestrator_judge_run_comment"] = str(
                case_status.get("orchestrator_judge_run_comment") or ""
            )
        run_id = str(summary["run_id"] or "").strip()
        if not summary["slack_thread_ts"]:
            for slack_run in status.get("slack_runs") or []:
                if not isinstance(slack_run, dict):
                    continue
                identifiers = {
                    str(slack_run.get("run_id") or "").strip(),
                    str(slack_run.get("work_item_id") or "").strip(),
                }
                if not run_id or run_id in identifiers:
                    summary["slack_thread_ts"] = str(slack_run.get("slack_thread_ts") or "").strip()
                    break
    return summary


def _trace_event_join_key(event: dict[str, Any]) -> str:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    correlation = metadata.get("correlation") if isinstance(metadata.get("correlation"), dict) else {}
    for value in (
        event.get("group_id"),
        metadata.get("case_id"),
        correlation.get("case_id"),
        metadata.get("work_item_id"),
        correlation.get("work_item_id"),
        metadata.get("run_id"),
        correlation.get("run_id"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _starter_run_plan(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a cost-safe first Slack eval run plan for the dashboard."""

    selected = next(
        (
            case
            for case in cases
            if str(case.get("case_id") or "") == DEFAULT_START_CASE_ID
        ),
        None,
    )
    if selected is None:
        selected = next(
            (
                case
                for case in cases
                if str(case.get("agent") or "") == "business_research_analyst"
                and str(case.get("user_input") or "").strip()
            ),
            None,
        )
    if selected is None:
        selected = next(
            (
                case
                for case in cases
                if str(case.get("user_input") or "").strip()
            ),
            {},
        )

    case_id = str(selected.get("case_id") or "").strip()
    return {
        "channel": "#evals",
        "case_id": case_id,
        "display_case_id": selected.get("display_case_id") or case_id,
        "prompt_number": selected.get("display_prompt_number") or "",
        "agent": selected.get("agent") or "",
        "paste_text": selected.get("user_input") or "",
        "dashboard_url": eval_dashboard_case_url(case_id) if case_id else "",
        "human_review_url": eval_review_case_url(case_id) if case_id else "",
        "status_reply": "Submit Evaluation",
        "local_only_now": True,
        "thread_sequence": [
            {
                "speaker": "Human",
                "message": "Root agent ask in #evals using the selected committed prompt.",
                "stored_as": "Slack root message plus resolved case_id",
            },
            {
                "speaker": "KNI agent",
                "message": "Normal agent answer in-thread, matching the ai-agents-workflow style: answer first, source/retrieval notes where relevant, no raw workflow metadata as the main answer.",
                "stored_as": "Slack run response/result",
            },
            {
                "speaker": "KNI eval context",
                "message": "In-thread eval footer with case_id, run_id, Promptfoo machine-check summary when imported, human review form link, and eval dashboard link.",
                "stored_as": "Case/run links for dashboard and review",
            },
            {
                "speaker": "Human",
                "message": "Open the Slack-linked scoring form from the eval footer after the agent answer is ready.",
                "stored_as": "Reviewer is shown saved prompt, response, machine check, and score fields",
            },
            {
                "speaker": "Human",
                "message": "Press Submit Evaluation in Slack after entering dimension scores, safety, and human notes.",
                "stored_as": "Human review row and notes in local eval database",
            },
            {
                "speaker": "Dashboard refresh",
                "message": "Database, Runs & Scoring, and Analysis refresh from saved local rows.",
                "stored_as": "Updated dashboard state without rerunning the agent answer",
            },
        ],
        "expected_live_calls_when_enabled": [
            {
                "step": "Root prompt",
                "expected_call": "One accepted agent run in the Slack thread",
                "cost_guardrail": "Run only the selected case; no dashboard copy action calls the model",
            },
            {
                "step": "Retrieval",
                "expected_call": "Only if the accepted agent run needs source evidence",
                "cost_guardrail": "Use fixtures/cache first and cap live retrieval to that root run",
            },
            {
                "step": "Promptfoo summary",
                "expected_call": "No model call and no Promptfoo rerun",
                "cost_guardrail": "Read the latest imported machine-check row by case_id and post only the summary/link footer",
            },
            {
                "step": "Human review form",
                "expected_call": "No model call",
                "cost_guardrail": "Use the saved response and machine-check row; the reviewer fills the form directly",
            },
            {
                "step": "Submit Evaluation",
                "expected_call": "No model call",
                "cost_guardrail": "Write score fields and human notes to the local eval database, then refresh Database, Runs & Scoring, and Analysis",
            },
            {
                "step": "Analysis refresh",
                "expected_call": "No model call",
                "cost_guardrail": "Recalculate charts from included database rows",
            },
        ],
    }


def _case_has_recorded_response(case: dict[str, Any]) -> bool:
    return bool(
        str(
            case.get("scored_response_text")
            or case.get("response_text")
            or case.get("latest_slack_summary")
            or ""
        ).strip()
    )


def _case_review_target(case: dict[str, Any]) -> dict[str, str]:
    """Return the recorded response target a human scorecard should update."""

    slack_run_id = str(case.get("latest_slack_run_id") or "").strip()
    slack_thread_ts = str(case.get("latest_slack_thread_ts") or "").strip()
    slack_response = str(case.get("latest_slack_summary") or "").strip()
    if slack_response and (slack_run_id or slack_thread_ts):
        return {
            "target_type": "slack",
            "run_id": slack_run_id,
            "slack_thread_ts": slack_thread_ts,
            "agent": str(case.get("agent") or "").strip(),
        }
    promptfoo_eval_id = str(case.get("promptfoo_eval_id") or "").strip()
    promptfoo_response = str(case.get("response_text") or case.get("promptfoo_reason") or "").strip()
    if promptfoo_eval_id and promptfoo_response:
        return {
            "target_type": "promptfoo",
            "run_id": promptfoo_eval_id,
            "slack_thread_ts": "",
            "agent": str(case.get("agent") or "").strip(),
        }
    return {
        "target_type": "none",
        "run_id": "",
        "slack_thread_ts": "",
        "agent": str(case.get("agent") or "").strip(),
    }


def _case_scored_response_text(case: dict[str, Any]) -> str:
    target_type = str((case.get("review_target") or {}).get("target_type") or "").strip()
    if target_type == "slack":
        return str(case.get("latest_slack_summary") or case.get("response_text") or "").strip()
    if target_type == "promptfoo":
        return str(case.get("response_text") or case.get("promptfoo_reason") or "").strip()
    return str(case.get("response_text") or case.get("latest_slack_summary") or "").strip()


def _dimension_summary_label(dimension: str) -> str:
    value = str(dimension or "").strip()
    if value in WEB_RETRIEVAL_DIMENSIONS:
        return "web_retrieval_quality"
    if value in INFORMATION_QUALITY_DIMENSIONS:
        return "information_quality"
    return value


def _average_case_score(cases: list[dict[str, Any]], key: str) -> float | None:
    values = [float(case[key]) for case in cases if case.get(key) is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _agent_score_summary(cases: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    values: dict[str, list[float]] = {}
    for case in cases:
        if case.get(key) is None:
            continue
        agent = str(case.get("agent") or "unknown").strip() or "unknown"
        values.setdefault(agent, []).append(float(case[key]))
    return {
        agent: {"average": round(sum(scores) / len(scores), 2), "count": len(scores)}
        for agent, scores in sorted(values.items())
    }


def _coverage_summary(agent_counts: Counter[str]) -> dict[str, dict[str, int]]:
    agents = sorted(set(CORE_EVAL_AGENTS) | set(agent_counts))
    return {
        agent: {
            "count": int(agent_counts.get(agent, 0)),
            "target": int(
                EVAL_CASE_AGENT_TARGETS.get(agent, EVAL_CASE_DEFAULT_TARGET_PER_AGENT)
            ),
            "gap": max(
                0,
                int(EVAL_CASE_AGENT_TARGETS.get(agent, EVAL_CASE_DEFAULT_TARGET_PER_AGENT))
                - int(agent_counts.get(agent, 0)),
            ),
        }
        for agent in agents
    }


def _latest_eval_runs(database_path: Path, limit: int = 8) -> list[dict[str, Any]]:
    if not database_path.exists():
        return []
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT eval_id, created_at, imported_at, total, successes, failures, errors
                     , average_score, agent_scores_json
                FROM promptfoo_eval_runs
                ORDER BY imported_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [_eval_run_row(dict(row)) for row in rows]


def _eval_run_ledger(database_path: Path, limit: int = 80) -> list[dict[str, Any]]:
    """Return a chronological local ledger across eval-related tables."""

    if not database_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        try:
            promptfoo_rows = connection.execute(
                """
                SELECT eval_id, created_at, imported_at, total, successes,
                       failures, errors, average_score
                FROM promptfoo_eval_runs
                ORDER BY COALESCE(NULLIF(created_at, ''), imported_at) DESC,
                         imported_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        except sqlite3.OperationalError:
            promptfoo_rows = []
        try:
            slack_rows = connection.execute(
                """
                WITH latest_promptfoo AS (
                    SELECT p.case_id, p.eval_id
                    FROM promptfoo_case_results p
                    WHERE p.id = (
                        SELECT p2.id
                        FROM promptfoo_case_results p2
                        WHERE p2.case_id = p.case_id
                        ORDER BY p2.imported_at DESC, p2.id DESC
                        LIMIT 1
                    )
                ),
                excluded_cases AS (
                    SELECT latest.case_id, e.reason
                    FROM latest_promptfoo latest
                    JOIN promptfoo_analysis_exclusions e
                      ON e.eval_id = latest.eval_id AND e.case_id = latest.case_id
                    WHERE COALESCE(e.excluded, 0) = 1
                )
                SELECT s.case_id, s.run_id, s.agent, s.work_item_id, s.slack_channel_name,
                       s.slack_thread_ts, s.status, s.route, s.thread_fetch_status,
                       s.thread_message_count, s.warning_count, s.cost_profile,
                       s.source_count, s.visible_source_count, s.response_hash,
                       CASE WHEN excluded.case_id IS NOT NULL THEN 1 ELSE 0 END AS excluded_from_scoring,
                       COALESCE(excluded.reason, '') AS analysis_exclusion_reason,
                       CASE WHEN length(trim(COALESCE(result_summary, ''))) > 0
                            THEN 1 ELSE 0 END AS has_result_summary,
                       (
                           SELECT COUNT(*)
                           FROM human_eval_reviews h
                           WHERE h.case_id = s.case_id
                             AND COALESCE(h.review_kind, 'human') = 'human'
                             AND (
                                 (length(trim(COALESCE(s.run_id, ''))) > 0 AND h.run_id = s.run_id)
                                 OR (
                                     length(trim(COALESCE(s.slack_thread_ts, ''))) > 0
                                     AND h.slack_thread_ts = s.slack_thread_ts
                                 )
                             )
                       ) AS human_review_count,
                       (
                           SELECT COUNT(*)
                           FROM promptfoo_case_results p
                           WHERE p.case_id = s.case_id
                       ) AS promptfoo_result_count,
                       s.created_at
                FROM slack_eval_runs s
                LEFT JOIN excluded_cases excluded
                  ON excluded.case_id = s.case_id
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        except sqlite3.OperationalError:
            slack_rows = []
        try:
            human_rows = connection.execute(
                """
                WITH latest_promptfoo AS (
                    SELECT p.case_id, p.eval_id
                    FROM promptfoo_case_results p
                    WHERE p.id = (
                        SELECT p2.id
                        FROM promptfoo_case_results p2
                        WHERE p2.case_id = p.case_id
                        ORDER BY p2.imported_at DESC, p2.id DESC
                        LIMIT 1
                    )
                ),
                excluded_cases AS (
                    SELECT latest.case_id, e.reason
                    FROM latest_promptfoo latest
                    JOIN promptfoo_analysis_exclusions e
                      ON e.eval_id = latest.eval_id AND e.case_id = latest.case_id
                    WHERE COALESCE(e.excluded, 0) = 1
                )
                SELECT h.case_id, run_id, agent, reviewer, average_score, safety,
                       COALESCE(h.review_kind, 'human') AS review_kind,
                       CASE WHEN excluded.case_id IS NOT NULL THEN 1 ELSE 0 END AS excluded_from_scoring,
                       COALESCE(excluded.reason, '') AS analysis_exclusion_reason,
                       created_at
                FROM human_eval_reviews h
                LEFT JOIN excluded_cases excluded
                  ON excluded.case_id = h.case_id
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        except sqlite3.OperationalError:
            human_rows = []
        try:
            trace_rows = connection.execute(
                """
                WITH latest_promptfoo AS (
                    SELECT p.case_id, p.eval_id
                    FROM promptfoo_case_results p
                    WHERE p.id = (
                        SELECT p2.id
                        FROM promptfoo_case_results p2
                        WHERE p2.case_id = p.case_id
                        ORDER BY p2.imported_at DESC, p2.id DESC
                        LIMIT 1
                    )
                ),
                excluded_cases AS (
                    SELECT latest.case_id, e.reason
                    FROM latest_promptfoo latest
                    JOIN promptfoo_analysis_exclusions e
                      ON e.eval_id = latest.eval_id AND e.case_id = latest.case_id
                    WHERE COALESCE(e.excluded, 0) = 1
                )
                SELECT event_type, trace_id, span_id, name, group_id,
                       CASE WHEN excluded.case_id IS NOT NULL THEN 1 ELSE 0 END AS excluded_from_scoring,
                       COALESCE(excluded.reason, '') AS analysis_exclusion_reason,
                       duration_ms, metadata_json, created_at
                FROM eval_trace_events
                LEFT JOIN excluded_cases excluded
                  ON excluded.case_id = eval_trace_events.group_id
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        except sqlite3.OperationalError:
            trace_rows = []

    trace_agentic_by_key = _trace_agentic_summary_index(trace_rows)
    for row in promptfoo_rows:
        item = dict(row)
        total = int(item.get("total") or 0)
        successes = int(item.get("successes") or 0)
        failures = int(item.get("failures") or 0)
        errors = int(item.get("errors") or 0)
        rows.append(
            {
                "source": "promptfoo",
                "timestamp": item.get("created_at") or item.get("imported_at") or "",
                "case_id": "",
                "run_id": item.get("eval_id") or "",
                "agent": "",
                "status": "fail" if failures or errors else "pass",
                "score": item.get("average_score"),
                "details": f"{successes}/{total} passed; {failures} failed; {errors} errors",
                "review_checklist": _ledger_promptfoo_review_checklist(
                    total=total,
                    failures=failures,
                    errors=errors,
                    average_score=item.get("average_score"),
                ),
                "next_follow_up": (
                    "Inspect failed/error cases before importing another run."
                    if failures or errors
                    else "Compare with Slack, Orchestrator Review, and human-review rows for the same cases."
                ),
            }
        )
    for row in slack_rows:
        item = dict(row)
        source_count = int(item.get("source_count") or 0)
        visible_source_count = int(item.get("visible_source_count") or 0)
        warning_count = int(item.get("warning_count") or 0)
        case_id = str(item.get("case_id") or "")
        run_id = str(item.get("run_id") or item.get("work_item_id") or item.get("slack_thread_ts") or "")
        trace_agentic = _lookup_trace_agentic_summary(
            trace_agentic_by_key,
            case_id,
            str(item.get("run_id") or ""),
            str(item.get("work_item_id") or ""),
            str(item.get("slack_thread_ts") or ""),
        )
        details = [
            f"#{item.get('slack_channel_name') or 'evals'}",
            f"thread {item.get('thread_fetch_status') or 'tbd'}",
            f"{int(item.get('thread_message_count') or 0)} messages",
        ]
        if source_count or visible_source_count:
            details.append(f"{visible_source_count}/{source_count} visible sources")
        if warning_count:
            details.append(f"{warning_count} warnings")
        if item.get("cost_profile"):
            details.append(str(item["cost_profile"]))
        if trace_agentic and trace_agentic.get("signal"):
            details.append(f"trace {trace_agentic['signal']}")
        if item.get("excluded_from_scoring"):
            details.append(
                f"excluded from scoring: {item.get('analysis_exclusion_reason') or 'manual exclusion'}"
            )
        rows.append(
            {
                "source": "slack",
                "timestamp": item.get("created_at") or "",
                "case_id": case_id,
                "run_id": run_id,
                "agent": item.get("agent") or item.get("route") or "",
                "status": item.get("status") or "saved",
                "score": None,
                "details": "; ".join(details),
                "dashboard_url": eval_dashboard_case_url(case_id) if case_id else "",
                "review_url": eval_review_case_url(case_id) if case_id else "",
                "case_bundle_url": eval_case_bundle_url(case_id) if case_id else "",
                "excluded_from_scoring": bool(item.get("excluded_from_scoring")),
                "analysis_exclusion_reason": item.get("analysis_exclusion_reason") or "",
                "trace_agentic_summary": trace_agentic,
                "review_checklist": _ledger_slack_review_checklist(item),
                "next_follow_up": _ledger_slack_next_follow_up(item),
            }
        )
    for row in human_rows:
        item = dict(row)
        case_id = str(item.get("case_id") or "")
        review_kind = str(item.get("review_kind") or "human").strip() or "human"
        source = "orchestrator_judge" if review_kind == "orchestrator_judge" else "human"
        reviewer = item.get("reviewer") or "unknown"
        rows.append(
            {
                "source": source,
                "timestamp": item.get("created_at") or "",
                "case_id": case_id,
                "run_id": item.get("run_id") or "",
                "agent": item.get("agent") or "",
                "status": item.get("safety") or "reviewed",
                "score": item.get("average_score"),
                "details": (
                    f"Orchestrator Review complete; reviewer {reviewer}"
                    if source == "orchestrator_judge"
                    else f"reviewer {reviewer}"
                ),
                "dashboard_url": eval_dashboard_case_url(case_id) if case_id else "",
                "review_url": eval_review_case_url(case_id) if case_id else "",
                "case_bundle_url": eval_case_bundle_url(case_id) if case_id else "",
                "review_kind": review_kind,
                "scoring_completed_at": item.get("created_at") or "",
                "excluded_from_scoring": bool(item.get("excluded_from_scoring")),
                "analysis_exclusion_reason": item.get("analysis_exclusion_reason") or "",
                "review_checklist": _ledger_human_review_checklist(item),
                "next_follow_up": (
                    "Review failed safety or low dimension scores before analysis inclusion."
                    if str(item.get("safety") or "").strip() == "fail"
                    else "Compare this Orchestrator Review score against machine score, human score, and evidence coverage."
                    if source == "orchestrator_judge"
                    else "Compare this human score against machine score and evidence coverage."
                ),
            }
        )
    for row in trace_rows:
        item = dict(row)
        metadata = _json_object(str(item.get("metadata_json") or "{}"))
        event_payload = {
            **item,
            "metadata": metadata,
        }
        trace_agentic = _trace_agentic_summary(event_payload)
        case_id = str(item.get("group_id") or metadata.get("case_id") or "")
        rows.append(
            {
                "source": "trace",
                "timestamp": item.get("created_at") or "",
                "case_id": case_id,
                "run_id": item.get("trace_id") or item.get("span_id") or "",
                "agent": trace_agentic.get("route") or metadata.get("agent_name") or metadata.get("agent") or "",
                "status": item.get("event_type") or "",
                "score": None,
                "details": "; ".join(
                    part
                    for part in (
                        str(item.get("name") or ""),
                        f"{item.get('duration_ms')} ms" if item.get("duration_ms") is not None else "",
                        str(trace_agentic.get("signal") or ""),
                        str(trace_agentic.get("tools") or ""),
                        str(trace_agentic.get("retrieval") or ""),
                    )
                    if part and not str(part).endswith("metadata pending")
                ),
                "dashboard_url": eval_dashboard_case_url(case_id) if case_id else "",
                "review_url": eval_review_case_url(case_id) if case_id else "",
                "case_bundle_url": eval_case_bundle_url(case_id) if case_id else "",
                "excluded_from_scoring": bool(item.get("excluded_from_scoring")),
                "analysis_exclusion_reason": item.get("analysis_exclusion_reason") or "",
                "trace_agentic_summary": trace_agentic,
                "review_checklist": _ledger_trace_review_checklist(item, metadata),
                "next_follow_up": "Confirm trace metadata is redacted and linked to the eval case/run.",
            }
        )
    rows.sort(key=lambda item: _timestamp_sort_key(str(item.get("timestamp") or "")), reverse=True)
    return rows[: max(1, int(limit))]


def _trace_keys_for_ledger_event(item: dict[str, Any], metadata: dict[str, Any]) -> set[str]:
    correlation = metadata.get("correlation") if isinstance(metadata.get("correlation"), dict) else {}
    keys = {
        str(item.get("group_id") or ""),
        str(item.get("trace_id") or ""),
        str(item.get("span_id") or ""),
        str(metadata.get("case_id") or ""),
        str(metadata.get("run_id") or ""),
        str(metadata.get("work_item_id") or ""),
        str(metadata.get("slack_thread_ts") or metadata.get("thread_ts") or ""),
        str(correlation.get("case_id") or ""),
        str(correlation.get("run_id") or ""),
        str(correlation.get("work_item_id") or ""),
        str(correlation.get("slack_thread_ts") or correlation.get("thread_ts") or ""),
    }
    return {key.strip() for key in keys if key and key.strip()}


def _trace_agentic_summary_index(trace_rows: list[sqlite3.Row]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in trace_rows:
        item = dict(row)
        metadata = _json_object(str(item.get("metadata_json") or "{}"))
        summary = _trace_agentic_summary({**item, "metadata": metadata})
        for key in _trace_keys_for_ledger_event(item, metadata):
            existing = index.get(key)
            if existing is None or _trace_agentic_summary_rank(summary) > _trace_agentic_summary_rank(existing):
                index[key] = summary
    return index


def _trace_agentic_summary_rank(summary: dict[str, Any]) -> int:
    signal = str(summary.get("signal") or "")
    rank = 0
    if signal and signal != "metadata only":
        rank += 4
    for key in ("tools", "retrieval", "model", "orchestrator"):
        value = str(summary.get(key) or "")
        if value and not value.endswith("metadata pending"):
            rank += 2
    if str(summary.get("errors") or "") != "no warnings/retries recorded":
        rank += 1
    return rank


def _lookup_trace_agentic_summary(
    index: dict[str, dict[str, Any]],
    *keys: str,
) -> dict[str, Any]:
    for key in keys:
        normalized = str(key or "").strip()
        if normalized and normalized in index:
            return index[normalized]
    return {}


def _ledger_check(label: str, status: str, detail: str) -> dict[str, str]:
    return {"label": label, "status": status, "detail": detail}


def _ledger_promptfoo_review_checklist(
    *,
    total: int,
    failures: int,
    errors: int,
    average_score: Any,
) -> list[dict[str, str]]:
    return [
        _ledger_check(
            "machine_import",
            "complete" if total else "missing",
            f"{total} Promptfoo case rows imported.",
        ),
        _ledger_check(
            "machine_result",
            "attention" if failures or errors else "complete",
            f"{failures} failures and {errors} errors.",
        ),
        _ledger_check(
            "machine_average",
            "complete" if average_score is not None else "missing",
            "Average assertion score is available." if average_score is not None else "Average assertion score is missing.",
        ),
    ]


def _ledger_slack_review_checklist(row: dict[str, Any]) -> list[dict[str, str]]:
    thread_status = str(row.get("thread_fetch_status") or "").strip()
    message_count = int(row.get("thread_message_count") or 0)
    source_count = int(row.get("source_count") or 0)
    visible_source_count = int(row.get("visible_source_count") or 0)
    warning_count = int(row.get("warning_count") or 0)
    human_review_count = int(row.get("human_review_count") or 0)
    promptfoo_result_count = int(row.get("promptfoo_result_count") or 0)
    has_response = bool(str(row.get("response_hash") or "").strip() or int(row.get("has_result_summary") or 0))
    return [
        _ledger_check(
            "response_saved",
            "complete" if has_response else "missing",
            "Saved response hash or result summary exists." if has_response else "No saved response hash/result summary was found.",
        ),
        _ledger_check(
            "slack_thread_evidence",
            "complete" if thread_status and message_count > 0 else "missing",
            f"Thread fetch status {thread_status or 'tbd'} with {message_count} messages.",
        ),
        _ledger_check(
            "source_visibility",
            "complete" if source_count == 0 or visible_source_count > 0 else "attention",
            f"{visible_source_count}/{source_count} sources visible in Slack output.",
        ),
        _ledger_check(
            "warnings",
            "attention" if warning_count else "complete",
            f"{warning_count} Slack evidence warnings.",
        ),
        _ledger_check(
            "human_review",
            "complete" if human_review_count else "missing",
            f"{human_review_count} matching human scorecards.",
        ),
        _ledger_check(
            "promptfoo_import",
            "complete" if promptfoo_result_count else "missing",
            f"{promptfoo_result_count} matching Promptfoo machine rows.",
        ),
    ]


def _ledger_slack_next_follow_up(row: dict[str, Any]) -> str:
    checks = _ledger_slack_review_checklist(row)
    missing = [check["label"] for check in checks if check["status"] in {"missing", "attention"}]
    if not missing:
        return "Ready to compare Slack output, machine score, Orchestrator Review, and human review in Analysis."
    return "Resolve: " + ", ".join(missing) + "."


def _ledger_human_review_checklist(row: dict[str, Any]) -> list[dict[str, str]]:
    average_score = row.get("average_score")
    safety = str(row.get("safety") or "").strip()
    return [
        _ledger_check(
            "human_average",
            "complete" if average_score is not None else "missing",
            "Human average score is saved." if average_score is not None else "Human average score is missing.",
        ),
        _ledger_check(
            "safety",
            "attention" if safety == "fail" else "complete" if safety else "missing",
            f"Safety marked {safety or 'tbd'}.",
        ),
    ]


def _ledger_trace_review_checklist(
    row: dict[str, Any],
    metadata: dict[str, Any],
) -> list[dict[str, str]]:
    return [
        _ledger_check(
            "case_link",
            "complete" if (row.get("group_id") or metadata.get("case_id")) else "missing",
            "Trace event is linked to a case." if (row.get("group_id") or metadata.get("case_id")) else "Trace event is not linked to a case.",
        ),
        _ledger_check(
            "redacted_metadata",
            "complete",
            "Dashboard trace rows expose metadata only, not raw prompts or responses.",
        ),
    ]


def _promptfoo_analysis(database_path: Path, limit: int = 12) -> dict[str, Any]:
    empty = {
        "run_trends": [],
        "human_review_trends": [],
        "orchestrator_judge_review_trends": [],
        "agent_score_trends": {"agents": [], "series": {"all": []}},
        "case_trends": [],
        "human_case_trends": [],
        "orchestrator_judge_case_trends": [],
        "prompt_score_averages": [],
        "case_changes": [],
        "agent_stability": [],
        "dimension_failures": [],
        "fix_signal": {},
    }
    if not database_path.exists():
        return empty
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        try:
            run_rows = connection.execute(
                """
                SELECT eval_id, created_at, imported_at, total, successes, failures, errors,
                       average_score
                FROM promptfoo_eval_runs
                ORDER BY COALESCE(NULLIF(created_at, ''), imported_at) ASC, imported_at ASC
                """
            ).fetchall()
            case_rows = connection.execute(
                """
                SELECT c.eval_id, c.case_id, c.agent_under_test, c.eval_dimensions,
                       c.success, c.score, c.reason, c.imported_at,
                       r.created_at AS run_created_at, r.imported_at AS run_imported_at
                FROM promptfoo_case_results c
                LEFT JOIN promptfoo_eval_runs r ON r.eval_id = c.eval_id
                LEFT JOIN promptfoo_analysis_exclusions e
                  ON e.eval_id = c.eval_id AND e.case_id = c.case_id
                WHERE COALESCE(e.excluded, 0) = 0
                ORDER BY COALESCE(NULLIF(r.created_at, ''), r.imported_at, c.imported_at) ASC,
                         c.imported_at ASC, c.id ASC
                """
            ).fetchall()
            human_rows = connection.execute(
                """
                WITH latest_promptfoo AS (
                    SELECT p.case_id, p.eval_id
                    FROM promptfoo_case_results p
                    WHERE p.id = (
                        SELECT p2.id
                        FROM promptfoo_case_results p2
                        WHERE p2.case_id = p.case_id
                        ORDER BY p2.imported_at DESC, p2.id DESC
                        LIMIT 1
                    )
                ),
                excluded_cases AS (
                    SELECT latest.case_id
                    FROM latest_promptfoo latest
                    JOIN promptfoo_analysis_exclusions e
                      ON e.eval_id = latest.eval_id AND e.case_id = latest.case_id
                    WHERE COALESCE(e.excluded, 0) = 1
                )
                SELECT h.id, h.case_id, h.run_id, h.agent, h.average_score, h.safety,
                       COALESCE(h.review_kind, 'human') AS review_kind,
                       h.slack_thread_ts, h.created_at
                FROM human_eval_reviews h
                WHERE NOT EXISTS (
                    SELECT 1 FROM excluded_cases excluded
                    WHERE excluded.case_id = h.case_id
                )
                ORDER BY created_at ASC, id ASC
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return empty

    run_order = {str(row["eval_id"]): index for index, row in enumerate(run_rows)}
    review_rows = [dict(row) for row in human_rows]
    human_review_rows = _current_target_human_review_rows(
        review_rows,
        database_path=database_path,
        review_kind="human",
    )
    orchestrator_judge_review_rows = _current_target_human_review_rows(
        review_rows,
        database_path=database_path,
        review_kind="orchestrator_judge",
    )
    human_review_trends = _human_review_trends(human_review_rows)
    orchestrator_judge_review_trends = _human_review_trends(orchestrator_judge_review_rows)
    human_case_trends = _human_case_review_trends(human_review_rows)
    orchestrator_judge_case_trends = _human_case_review_trends(orchestrator_judge_review_rows)
    histories: dict[str, list[dict[str, Any]]] = {}
    agent_values: dict[str, list[dict[str, Any]]] = {}
    dimension_failures: Counter[str] = Counter()
    dimension_agents: dict[str, set[str]] = {}
    items_by_eval: dict[str, list[dict[str, Any]]] = {}

    for row in case_rows:
        item = _analysis_case_row(dict(row), run_order)
        items_by_eval.setdefault(item["eval_id"], []).append(item)
        histories.setdefault(item["case_id"], []).append(item)
        if not item["success"]:
            for dimension in item["dimensions"]:
                label = _dimension_summary_label(dimension)
                dimension_failures[label] += 1
                dimension_agents.setdefault(label, set()).add(item["agent"])

    run_trends = [
        _analysis_run_row_from_cases(dict(row), items_by_eval.get(str(row["eval_id"]), []))
        for row in run_rows
        if items_by_eval.get(str(row["eval_id"]))
    ][-limit:]
    daily_histories = {
        case_id: _daily_case_items(items)
        for case_id, items in histories.items()
    }
    daily_items = [item for items in daily_histories.values() for item in items]
    agent_score_trends = _agent_score_trends(
        daily_items,
        human_review_rows,
        orchestrator_judge_review_rows,
    )
    for items in daily_histories.values():
        for item in items:
            agent_values.setdefault(item["agent"], []).append(item)

    case_changes = [_case_change_summary(case_id, items) for case_id, items in daily_histories.items()]
    case_changes = [item for item in case_changes if item is not None]
    case_trends = [_case_trend_summary(case_id, items) for case_id, items in daily_histories.items()]
    case_trends = [item for item in case_trends if item is not None]
    case_trends.sort(key=lambda item: (item["agent"], item["case_id"]))
    prompt_score_averages = [_prompt_score_average(case_id, items) for case_id, items in daily_histories.items()]
    prompt_score_averages = [item for item in prompt_score_averages if item is not None]
    prompt_score_averages.sort(
        key=lambda item: (
            item["agent"],
            item["average_score"] if item["average_score"] is not None else -1,
            item["case_id"],
        )
    )
    case_changes.sort(
        key=lambda item: (
            {"regressed": 0, "improved": 1, "stable": 2, "new": 3}.get(item["status"], 9),
            -abs(float(item.get("score_delta") or 0)),
            item["case_id"],
        )
    )
    agent_stability = [_agent_stability_summary(agent, items) for agent, items in agent_values.items()]
    agent_stability.sort(key=lambda item: (-item["flaky_cases"], item["pass_rate"], item["agent"]))
    dimension_rows = [
        {
            "dimension": dimension,
            "failures": count,
            "agents": sorted(dimension_agents.get(dimension, set())),
        }
        for dimension, count in dimension_failures.most_common(12)
    ]
    return {
        "run_trends": run_trends,
        "human_review_trends": human_review_trends,
        "orchestrator_judge_review_trends": orchestrator_judge_review_trends,
        "agent_score_trends": agent_score_trends,
        "human_case_trends": human_case_trends,
        "orchestrator_judge_case_trends": orchestrator_judge_case_trends,
        "case_trends": case_trends,
        "prompt_score_averages": prompt_score_averages,
        "case_changes": case_changes[:40],
        "agent_stability": agent_stability,
        "dimension_failures": dimension_rows,
        "fix_signal": _fix_signal_summary(run_trends, case_changes),
    }


def _current_target_human_review_rows(
    rows: list[dict[str, Any]],
    *,
    database_path: Path,
    review_kind: str = "human",
) -> list[dict[str, Any]]:
    """Keep analysis review rows aligned with the current response target."""

    normalized_kind = str(review_kind or "human").strip() or "human"
    case_ids = [
        str(row.get("case_id") or "").strip()
        for row in rows
        if row.get("case_id")
        and (str(row.get("review_kind") or "human").strip() or "human") == normalized_kind
    ]
    if not case_ids:
        return []
    statuses = eval_case_statuses(case_ids, database_path=database_path)
    current_rows: list[dict[str, Any]] = []
    for row in rows:
        row_kind = str(row.get("review_kind") or "human").strip() or "human"
        if row_kind != normalized_kind:
            continue
        row_id = row.get("id")
        if row_id is None:
            continue
        status = statuses.get(str(row.get("case_id") or "").strip()) or {}
        review_key = (
            "latest_target_orchestrator_judge_review"
            if normalized_kind == "orchestrator_judge"
            else "latest_target_human_review"
        )
        review = status.get(review_key)
        if not isinstance(review, dict) or review.get("id") is None:
            continue
        if int(row_id or 0) != int(review.get("id") or 0):
            continue
        enriched = dict(row)
        enriched["review_target"] = _current_review_target_metadata(status)
        current_rows.append(enriched)
    return current_rows


def _current_review_target_metadata(status: dict[str, Any]) -> dict[str, Any]:
    latest_slack = (status.get("slack_runs") or [None])[0]
    if isinstance(latest_slack, dict) and latest_slack:
        return {
            "target_type": "slack",
            "run_id": str(latest_slack.get("run_id") or latest_slack.get("work_item_id") or ""),
            "agent": str(latest_slack.get("agent") or latest_slack.get("route") or ""),
            "slack_thread_ts": str(latest_slack.get("slack_thread_ts") or ""),
            "created_at": str(latest_slack.get("created_at") or ""),
            "storage_mode": str(latest_slack.get("storage_mode") or ""),
        }
    latest_promptfoo = status.get("latest_promptfoo")
    if isinstance(latest_promptfoo, dict) and latest_promptfoo:
        return {
            "target_type": "promptfoo",
            "run_id": str(latest_promptfoo.get("eval_id") or ""),
            "agent": str(latest_promptfoo.get("agent_under_test") or ""),
            "slack_thread_ts": "",
            "created_at": str(latest_promptfoo.get("imported_at") or latest_promptfoo.get("created_at") or ""),
            "storage_mode": str(latest_promptfoo.get("storage_mode") or ""),
        }
    return {"target_type": "unknown", "run_id": "", "agent": "", "slack_thread_ts": ""}


def _human_review_trends(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    daily_scores: dict[str, list[float]] = {}
    for row in rows:
        score = row.get("average_score")
        created_at = str(row.get("created_at") or "")
        if score is None or not created_at:
            continue
        day = created_at[:10]
        daily_scores.setdefault(day, []).append(float(score))
    return [
        {
            "date": day,
            "average_score": round(sum(scores) / len(scores), 3),
            "count": len(scores),
        }
        for day, scores in sorted(daily_scores.items())
    ]


def _agent_score_trends(
    machine_rows: list[dict[str, Any]],
    human_rows: list[dict[str, Any]],
    orchestrator_judge_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    agents: set[str] = set()
    machine_scores: dict[str, dict[str, list[float]]] = {"all": {}}
    human_scores: dict[str, dict[str, list[float]]] = {"all": {}}
    judge_scores: dict[str, dict[str, list[float]]] = {"all": {}}

    def add_score(
        buckets: dict[str, dict[str, list[float]]],
        agent: str,
        day: str,
        score: float,
    ) -> None:
        buckets.setdefault("all", {}).setdefault(day, []).append(score)
        buckets.setdefault(agent, {}).setdefault(day, []).append(score)

    for row in machine_rows:
        created_at = str(row.get("created_at") or "")
        score = row.get("score")
        if score is None or not created_at:
            continue
        day = created_at[:10]
        agent = str(row.get("agent") or "unknown").strip() or "unknown"
        agents.add(agent)
        add_score(machine_scores, agent, day, float(score) * 5)

    for row in human_rows:
        created_at = str(row.get("created_at") or "")
        score = row.get("average_score")
        if score is None or not created_at:
            continue
        day = created_at[:10]
        agent = str(row.get("agent") or "unknown").strip() or "unknown"
        agents.add(agent)
        add_score(human_scores, agent, day, float(score))

    for row in orchestrator_judge_rows:
        created_at = str(row.get("created_at") or "")
        score = row.get("average_score")
        if score is None or not created_at:
            continue
        day = created_at[:10]
        agent = str(row.get("agent") or "unknown").strip() or "unknown"
        agents.add(agent)
        add_score(judge_scores, agent, day, float(score))

    def build_series(key: str) -> list[dict[str, Any]]:
        days = sorted(
            set(machine_scores.get(key, {}))
            | set(human_scores.get(key, {}))
            | set(judge_scores.get(key, {}))
        )
        series: list[dict[str, Any]] = []
        for day in days:
            machine = machine_scores.get(key, {}).get(day, [])
            human = human_scores.get(key, {}).get(day, [])
            judge = judge_scores.get(key, {}).get(day, [])
            series.append(
                {
                    "date": day,
                    "machine_average": round(sum(machine) / len(machine), 3) if machine else None,
                    "machine_count": len(machine),
                    "human_average": round(sum(human) / len(human), 3) if human else None,
                    "human_count": len(human),
                    "orchestrator_judge_average": round(sum(judge) / len(judge), 3) if judge else None,
                    "orchestrator_judge_count": len(judge),
                }
            )
        return series

    series = {"all": build_series("all")}
    for agent in sorted(agents):
        series[agent] = build_series(agent)
    return {"agents": sorted(agents), "series": series}


def _human_case_review_trends(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_case_day: dict[str, dict[str, list[float]]] = {}
    review_counts: dict[str, dict[str, int]] = {}
    review_targets: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in rows:
        case_id = str(row.get("case_id") or "")
        score = row.get("average_score")
        created_at = str(row.get("created_at") or "")
        if not case_id or score is None or not created_at:
            continue
        day = created_at[:10]
        by_case_day.setdefault(case_id, {}).setdefault(day, []).append(float(score))
        review_counts.setdefault(case_id, {}).setdefault(day, 0)
        review_counts[case_id][day] += 1
        review_targets.setdefault(case_id, {}).setdefault(day, []).append(
            _human_review_target_metadata(row)
        )
    return [
        {
            "case_id": case_id,
            "days": [
                {
                    "date": day,
                    "average_score": round(sum(scores) / len(scores), 3),
                    "count": review_counts.get(case_id, {}).get(day, len(scores)),
                    "reviews": review_targets.get(case_id, {}).get(day, []),
                }
                for day, scores in sorted(days.items())
            ],
        }
        for case_id, days in sorted(by_case_day.items())
    ]


def _human_review_target_metadata(row: dict[str, Any]) -> dict[str, Any]:
    target = row.get("review_target") if isinstance(row.get("review_target"), dict) else {}
    run_id = str(target.get("run_id") or row.get("run_id") or "").strip()
    slack_thread_ts = str(target.get("slack_thread_ts") or row.get("slack_thread_ts") or "").strip()
    target_type = str(target.get("target_type") or "").strip()
    if not target_type and (slack_thread_ts or run_id.startswith("wi_") or run_id.startswith("manual_run:")):
        target_type = "slack"
    elif not target_type and run_id:
        target_type = "promptfoo"
    elif not target_type:
        target_type = "unknown"
    return {
        "review_id": row.get("id"),
        "target_type": target_type,
        "run_id": run_id,
        "agent": str(target.get("agent") or row.get("agent") or ""),
        "slack_thread_ts": slack_thread_ts,
        "target_created_at": str(target.get("created_at") or ""),
        "target_storage_mode": str(target.get("storage_mode") or ""),
        "created_at": str(row.get("created_at") or ""),
        "safety": str(row.get("safety") or ""),
        "average_score": row.get("average_score"),
        "review_kind": str(row.get("review_kind") or "human"),
    }


def _analysis_run_row(row: dict[str, Any]) -> dict[str, Any]:
    total = int(row.get("total") or 0)
    successes = int(row.get("successes") or 0)
    return {
        "eval_id": str(row.get("eval_id") or ""),
        "created_at": row.get("created_at") or row.get("imported_at") or "",
        "total": total,
        "successes": successes,
        "failures": int(row.get("failures") or 0),
        "errors": int(row.get("errors") or 0),
        "pass_rate": round(successes / total * 100, 1) if total else None,
        "average_score": row.get("average_score"),
    }


def _analysis_run_row_from_cases(row: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(items)
    successes = sum(1 for item in items if item["success"])
    scores = [float(item["score"]) for item in items]
    return {
        "eval_id": str(row.get("eval_id") or ""),
        "created_at": row.get("created_at") or row.get("imported_at") or "",
        "total": total,
        "successes": successes,
        "failures": total - successes,
        "errors": 0,
        "pass_rate": round(successes / total * 100, 1) if total else None,
        "average_score": round(sum(scores) / len(scores), 3) if scores else None,
    }


def _analysis_case_row(row: dict[str, Any], run_order: dict[str, int]) -> dict[str, Any]:
    eval_id = str(row.get("eval_id") or "")
    return {
        "eval_id": eval_id,
        "case_id": str(row.get("case_id") or ""),
        "agent": str(row.get("agent_under_test") or "unknown").strip() or "unknown",
        "dimensions": _split_dimensions(str(row.get("eval_dimensions") or "")),
        "success": bool(row.get("success")),
        "score": float(row.get("score") or 0),
        "reason": str(row.get("reason") or ""),
        "created_at": row.get("run_created_at") or row.get("run_imported_at") or row.get("imported_at") or "",
        "run_order": run_order.get(eval_id, 0),
    }


def _daily_case_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in items:
        day = str(item.get("created_at") or "")[:10] or str(item.get("created_at") or "")
        grouped.setdefault((item["case_id"], day), []).append(item)
    daily: list[dict[str, Any]] = []
    for (_case_id, day), group in grouped.items():
        ordered = sorted(group, key=lambda item: (item["run_order"], item["created_at"], item["eval_id"]))
        latest = ordered[-1]
        scores = [float(item["score"]) for item in ordered]
        success_count = sum(1 for item in ordered if item["success"])
        dimensions: list[str] = []
        for item in ordered:
            for dimension in item["dimensions"]:
                if dimension not in dimensions:
                    dimensions.append(dimension)
        daily.append(
            {
                "eval_id": latest["eval_id"],
                "eval_ids": [item["eval_id"] for item in ordered],
                "case_id": latest["case_id"],
                "agent": latest["agent"],
                "dimensions": dimensions,
                "success": success_count / len(ordered) >= 0.5,
                "success_rate": round(success_count / len(ordered) * 100, 1),
                "score": round(sum(scores) / len(scores), 3),
                "reason": latest.get("reason") or "",
                "created_at": day,
                "latest_created_at": latest["created_at"],
                "run_order": latest["run_order"],
                "run_count": len(ordered),
            }
        )
    return sorted(daily, key=lambda item: (item["run_order"], item["created_at"], item["eval_id"]))


def _case_trend_summary(case_id: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    ordered = sorted(items, key=lambda item: (item["run_order"], item["created_at"]))
    if not ordered:
        return None
    latest = ordered[-1]
    return {
        "case_id": case_id,
        "agent": latest["agent"],
        "dimensions": latest["dimensions"],
        "runs": [
            {
                "eval_id": item["eval_id"],
                "created_at": item["created_at"],
                "latest_created_at": item.get("latest_created_at") or item["created_at"],
                "success": item["success"],
                "success_rate": item.get("success_rate"),
                "score": item["score"],
                "run_count": item.get("run_count", 1),
            }
            for item in ordered
        ],
    }


def _prompt_score_average(case_id: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not items:
        return None
    scores = [float(item["score"]) for item in items]
    latest = sorted(items, key=lambda item: (item["run_order"], item["created_at"]))[-1]
    return {
        "case_id": case_id,
        "agent": latest["agent"],
        "dimensions": latest["dimensions"],
        "average_score": round(sum(scores) / len(scores), 3) if scores else None,
        "run_count": len(items),
        "latest_score": latest["score"],
        "latest_created_at": latest["created_at"],
        "latest_success": latest["success"],
    }


def _case_change_summary(case_id: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    ordered = sorted(items, key=lambda item: (item["run_order"], item["created_at"]))
    if not ordered:
        return None
    latest = ordered[-1]
    previous = ordered[-2] if len(ordered) > 1 else None
    total_runs = sum(int(item.get("run_count") or 1) for item in ordered)
    latest_run_count = int(latest.get("run_count") or 1)
    score_delta = None if previous is None else round(latest["score"] - previous["score"], 3)
    status = "new"
    if previous is not None:
        if previous["success"] is False and latest["success"] is True:
            status = "improved"
        elif previous["success"] is True and latest["success"] is False:
            status = "regressed"
        elif score_delta is not None and score_delta > 0.001:
            status = "improved"
        elif score_delta is not None and score_delta < -0.001:
            status = "regressed"
        else:
            status = "stable"
    return {
        "case_id": case_id,
        "agent": latest["agent"],
        "status": status,
        "runs": total_runs,
        "daily_points": len(ordered),
        "latest_run_count": latest_run_count,
        "latest_eval_id": latest["eval_id"],
        "previous_eval_id": previous["eval_id"] if previous else "",
        "latest_created_at": latest.get("latest_created_at") or latest["created_at"],
        "previous_created_at": (previous.get("latest_created_at") or previous["created_at"]) if previous else "",
        "latest_success": latest["success"],
        "previous_success": previous["success"] if previous else None,
        "latest_score": latest["score"],
        "previous_score": previous["score"] if previous else None,
        "score_delta": score_delta,
        "dimensions": latest["dimensions"],
    }


def _agent_stability_summary(agent: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [float(item["score"]) for item in items]
    success_count = sum(1 for item in items if item["success"])
    histories: dict[str, set[bool]] = {}
    for item in items:
        histories.setdefault(item["case_id"], set()).add(bool(item["success"]))
    return {
        "agent": agent,
        "runs": len(items),
        "pass_rate": round(success_count / len(items) * 100, 1) if items else 0,
        "average_score": round(sum(scores) / len(scores), 3) if scores else None,
        "score_range": round(max(scores) - min(scores), 3) if scores else 0,
        "flaky_cases": sum(1 for values in histories.values() if len(values) > 1),
    }


def _fix_signal_summary(
    run_trends: list[dict[str, Any]],
    case_changes: list[dict[str, Any]],
) -> dict[str, Any]:
    latest = run_trends[-1] if run_trends else {}
    previous = run_trends[-2] if len(run_trends) > 1 else {}
    return {
        "latest_eval_id": latest.get("eval_id") or "",
        "previous_eval_id": previous.get("eval_id") or "",
        "pass_rate_delta": _delta(latest.get("pass_rate"), previous.get("pass_rate")),
        "average_score_delta": _delta(latest.get("average_score"), previous.get("average_score")),
        "improved_cases": sum(1 for item in case_changes if item["status"] == "improved"),
        "regressed_cases": sum(1 for item in case_changes if item["status"] == "regressed"),
        "stable_cases": sum(1 for item in case_changes if item["status"] == "stable"),
    }


def _delta(latest: Any, previous: Any) -> float | None:
    if latest is None or previous is None:
        return None
    try:
        return round(float(latest) - float(previous), 3)
    except (TypeError, ValueError):
        return None


def _eval_run_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["agent_scores"] = _json_object(str(payload.pop("agent_scores_json") or ""))
    return payload


def _case_response_text(
    latest_promptfoo: dict[str, Any],
    latest_slack: dict[str, Any],
) -> str:
    output = latest_promptfoo.get("output") if isinstance(latest_promptfoo, dict) else {}
    if isinstance(output, dict):
        if output.get("storage_mode") == "api_redacted":
            summary = str(output.get("response_summary") or "").strip()
            if summary:
                return summary
        response = output.get("output") or output.get("response") or output.get("human_summary")
        if isinstance(response, str) and response.strip():
            parsed = _json_object(response)
            if parsed:
                summary = str(parsed.get("human_summary") or parsed.get("output") or "").strip()
                if summary:
                    return summary
            return response.strip()
    reason = str(latest_promptfoo.get("reason") or latest_promptfoo.get("failure_reason") or "").strip()
    return reason


def _json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dashboard_html(payload: dict[str, Any]) -> str:
    data_json = _json_script_payload(payload)
    summary = payload["summary"]
    dashboard_health = payload.get("dashboard_health") or {}
    data_quality = payload.get("data_quality") or {}
    dashboard_health_label = "online" if dashboard_health.get("dashboard_reachable") else "offline"
    dashboard_health_hint = (
        f"{dashboard_health.get('dashboard_url') or 'dashboard URL'}; "
        f"health {dashboard_health.get('health_url') or 'endpoint'}; "
        f"manager {'ready' if dashboard_health.get('manager_exists') else 'missing'}"
    )
    data_quality_label = f'{data_quality.get("pass_count", 0)}/{data_quality.get("total_checks", 0)} complete'
    data_quality_hint = (
        f'{data_quality.get("pending_count", data_quality.get("warning_count", 0))} pending before paid runs'
    )
    database_score_cols = "\n".join(
        '<col class="dimension-score-col">'
        for _dimension in SCORE_DIMENSIONS
        for _source in ("human", "orchestrator")
    )
    database_score_headers = "\n".join(
        "\n".join(
            (
                '<th><div class="metric-th">'
                f'<div class="metric-th-title">Human {html.escape(dimension.replace("_", " "))}</div>'
                '<div class="metric-th-detail">review form</div></div></th>',
                '<th><div class="metric-th">'
                f'<div class="metric-th-title">Orchestrator {html.escape(dimension.replace("_", " "))}</div>'
                '<div class="metric-th-detail">review form</div></div></th>',
            )
        )
        for dimension in SCORE_DIMENSIONS
    )
    title = "Keystone Eval Scoring"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f8;
      --panel: #ffffff;
      --ink: #1b1f23;
      --muted: #687076;
      --line: #d9dee3;
      --good: #287a47;
      --bad: #b42318;
      --warn: #c36a14;
      --accent: #3f6b4a;
      --accent-2: #8a6f2a;
      --soft: #f1f4f6;
      --soft-warn: #f2f4f5;
    }}
    * {{ box-sizing: border-box; }}
    html {{
      max-width: 100%;
      min-height: 100%;
      overflow-x: hidden;
      overflow-y: auto;
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      max-width: 100%;
      min-height: 100%;
      overflow-x: hidden;
      overflow-y: clip;
    }}
    header {{
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      padding: 20px 28px 18px;
    }}
    h1 {{ margin: 0 0 8px; font-size: 34px; line-height: 1.1; letter-spacing: 0; }}
    .subtle {{ color: var(--muted); }}
    .dashboard-intro {{
      max-width: 980px;
      color: #3f464c;
      font-size: 15px;
      line-height: 1.45;
    }}
    .dashboard-intro p {{ margin: 0 0 4px; }}
    .intro-details {{
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
    }}
    .intro-details summary {{
      cursor: pointer;
      width: fit-content;
      font-weight: 650;
    }}
    .intro-details .detail-content {{
      max-width: 920px;
      margin-top: 6px;
      display: grid;
      gap: 3px;
    }}
    main {{
      padding: 20px 28px 34px;
      width: 100%;
      max-width: 1480px;
      margin: 0 auto;
      overflow-x: clip;
    }}
    .kpis {{
      display: grid;
      grid-template-columns: repeat(6, minmax(140px, 1fr));
      gap: 10px;
      margin-bottom: 18px;
    }}
    .kpi, .panel, .case-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .kpi {{
      display: block;
      width: 100%;
      padding: 12px;
      min-height: 106px;
      text-align: left;
      min-width: 0;
    }}
    .kpi[data-drilldown] {{ cursor: pointer; }}
    .kpi[data-drilldown]:hover, .kpi.active {{
      border-color: #93ad8f;
      box-shadow: 0 0 0 2px rgba(63, 107, 74, 0.14);
    }}
    .kpi .label {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    .kpi .value {{ font-size: 25px; font-weight: 740; margin-top: 7px; }}
    .kpi .hint {{
      color: var(--muted);
      font-size: 12px;
      margin-top: 4px;
      overflow-wrap: anywhere;
    }}
    .grid {{
      display: grid;
      grid-template-columns: minmax(330px, 410px) minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }}
    .workspace {{
      display: grid;
      grid-template-columns: 180px minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }}
    .side-nav {{
      position: sticky;
      top: 14px;
      display: grid;
      gap: 6px;
    }}
    .side-nav button {{
      width: 100%;
      text-align: left;
      border-color: transparent;
      background: transparent;
      font-weight: 650;
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    .side-nav button.active {{
      background: #eef6ef;
      border-color: #b8d6bd;
      color: #2f5437;
    }}
    .view {{ display: none; }}
    .view.active {{ display: block; }}
    .grid > *, .workspace > *, .view, .panel {{ min-width: 0; }}
    .panel {{ padding: 14px; }}
    .section-head {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
      min-width: 0;
    }}
    .subsection {{
      padding-top: 14px;
      margin-top: 16px;
      border-top: 1px solid var(--line);
    }}
    .subsection:first-child {{
      padding-top: 0;
      margin-top: 0;
      border-top: 0;
    }}
    .subsection-title {{
      margin-bottom: 2px;
    }}
    h2 {{ font-size: 15px; margin: 0; }}
    .bar-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 130px 54px;
      gap: 8px;
      align-items: center;
      margin: 8px 0;
    }}
    .bar-row > *, .score-row > * {{ min-width: 0; }}
    .bar-track {{ height: 10px; background: #e8edf0; border-radius: 999px; overflow: hidden; }}
        .bar {{ height: 100%; background: #3b7045; }}
        .bar.gap {{ background: var(--warn); }}
        .bar.fail {{ background: #9d3b32; }}
        .bar.info {{ background: #58789e; }}
        .bar.neutral {{ background: #6b7280; }}
    .score-table {{
      display: grid;
      gap: 6px;
      margin-bottom: 16px;
    }}
    .score-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 72px 72px;
      gap: 8px;
      align-items: center;
      padding: 8px 0;
      border-bottom: 1px solid #edf0f2;
    }}
    .score-row.header {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      padding-top: 0;
    }}
    .run-list {{ display: grid; gap: 8px; margin-top: 8px; }}
    .run-card {{
      border: 1px solid var(--line);
      border-left: 4px solid var(--warn);
      border-radius: 7px;
      background: #fbfcfb;
      padding: 10px;
    }}
    .run-card.pass {{ border-left-color: var(--good); background: #f3faf5; }}
    .run-card.fail {{ border-left-color: var(--bad); background: #fff4f2; }}
    .run-card.warn {{ border-left-color: var(--warn); background: #f7f8f9; }}
    .run-card.warn .run-title {{ color: var(--warn); }}
    .run-head {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
      margin-bottom: 7px;
    }}
    .run-title {{ font-weight: 650; overflow-wrap: anywhere; }}
    .run-stats {{ display: flex; gap: 5px; flex-wrap: wrap; margin-bottom: 7px; }}
    .run-meter {{ height: 8px; border-radius: 999px; background: #e7e3dc; overflow: hidden; }}
    .run-meter > span {{ display: block; height: 100%; background: var(--good); }}
    .status-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(150px, 1fr));
      gap: 8px;
      margin-top: 8px;
    }}
    .status-box {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #f8faf9;
      padding: 8px;
      min-width: 0;
    }}
    .status-box .status-title {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      margin-bottom: 4px;
    }}
    .status-box .status-main {{
      font-weight: 700;
      overflow-wrap: anywhere;
    }}
    .status-box .status-date {{
      color: var(--muted);
      font-size: 12px;
      margin-top: 4px;
      overflow-wrap: anywhere;
    }}
    .workflow-grid {{
      display: grid;
      grid-template-columns: minmax(260px, 1fr) minmax(300px, 1.35fr);
      gap: 12px;
      margin-top: 10px;
    }}
    .workflow-steps {{
      grid-column: 1 / -1;
      display: grid;
      grid-template-columns: repeat(3, minmax(150px, 1fr));
      gap: 8px;
      align-content: start;
    }}
    .workflow-step {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fbfcfb;
      padding: 10px;
      min-width: 0;
    }}
    .workflow-step .label {{ color: var(--muted); font-size: 12px; font-weight: 650; }}
    .workflow-step .value {{ font-size: 20px; font-weight: 750; margin-top: 4px; }}
    .workflow-stage-bar {{
      height: 7px;
      border-radius: 999px;
      background: #e5eaee;
      overflow: hidden;
      margin: 8px 0 6px;
    }}
    .workflow-stage-fill {{
      height: 100%;
      border-radius: inherit;
      background: var(--accent);
      min-width: 2px;
    }}
    .workflow-chat {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #f8faf9;
      padding: 10px;
      display: grid;
      gap: 8px;
      min-width: 0;
    }}
    .chat-row {{
      display: grid;
      grid-template-columns: 86px minmax(0, 1fr);
      gap: 8px;
      align-items: start;
    }}
    .chat-speaker {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      padding-top: 7px;
    }}
    .chat-bubble {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fff;
      padding: 8px 10px;
      overflow-wrap: anywhere;
    }}
    .chat-bubble.system {{ background: #f2f5f3; }}
    .workflow-run-plan {{
      grid-column: 1 / -1;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fff;
      min-width: 0;
    }}
    .workflow-disclosure {{
      grid-column: 1 / -1;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fff;
      min-width: 0;
      overflow: hidden;
    }}
    .workflow-disclosure summary,
    .dashboard-details summary {{
      cursor: pointer;
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      padding: 10px 12px;
      font-weight: 700;
    }}
    .workflow-disclosure[open] summary {{
      border-bottom: 1px solid var(--line);
      margin-bottom: 10px;
    }}
    .workflow-disclosure summary .subtle,
    .dashboard-details summary .subtle {{
      font-weight: 500;
      font-size: 12px;
    }}
    .run-plan-grid {{
      display: grid;
      grid-template-columns: minmax(250px, 0.9fr) minmax(320px, 1.1fr);
      gap: 12px;
      padding: 0 12px 12px;
    }}
    .workflow-disclosure > .run-plan-box {{
      margin: 0 12px 12px;
    }}
    .workflow-thread .workflow-chat {{
      margin: 0 12px 12px;
    }}
    .run-plan-box {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #f8faf9;
      padding: 10px;
      min-width: 0;
    }}
    .run-plan-box .label {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 5px;
    }}
    .run-plan-list {{
      margin: 8px 0 0;
      padding-left: 18px;
    }}
    .run-plan-list li {{
      margin: 4px 0;
    }}
    .run-plan-prompt {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #eef2f4;
      padding: 9px 10px;
      overflow-wrap: anywhere;
    }}
    .workflow-contract {{
      background: #fbfcfb;
    }}
    .workflow-contract table {{
      min-width: 980px;
      table-layout: fixed;
    }}
    .workflow-contract th,
    .workflow-contract td {{
      padding: 8px 12px;
    }}
    .workflow-contract th:nth-child(1) {{ width: 16%; }}
    .workflow-contract th:nth-child(2) {{ width: 23%; }}
    .workflow-contract th:nth-child(3) {{ width: 18%; }}
    .workflow-contract th:nth-child(4) {{ width: 20%; }}
    .workflow-contract th:nth-child(5) {{ width: 8%; }}
    .workflow-contract th:nth-child(6) {{ width: 15%; }}
    .analysis-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(140px, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }}
    .analysis-card {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #f8faf9;
      padding: 10px;
      min-width: 0;
    }}
    .analysis-card .label {{ color: var(--muted); font-size: 12px; font-weight: 650; }}
    .trace-guide {{
      display: grid;
      grid-template-columns: repeat(4, minmax(120px, 1fr));
      gap: 8px;
      margin-bottom: 12px;
    }}
    .trace-guide-item {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fbfcfb;
      padding: 8px 9px;
      min-width: 0;
    }}
    .trace-guide-item strong {{
      display: block;
      margin-bottom: 3px;
      font-size: 13px;
    }}
    .trace-guide-item span {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.3;
    }}
    .trace-contract {{
      grid-template-columns: repeat(3, minmax(160px, 1fr));
    }}
    .trace-run-overview {{
      margin-bottom: 12px;
    }}
    .trace-run-card {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #f8faf9;
      padding: 12px;
      min-width: 0;
    }}
    .trace-run-top {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 10px;
    }}
    .trace-run-title {{
      font-size: 20px;
      font-weight: 750;
      line-height: 1.2;
      overflow-wrap: anywhere;
    }}
    .trace-run-subtitle {{
      color: var(--muted);
      font-size: 12px;
      margin-top: 3px;
      overflow-wrap: anywhere;
    }}
    .trace-run-meta {{
      display: grid;
      grid-template-columns: repeat(4, minmax(120px, 1fr));
      gap: 8px;
    }}
    .trace-run-meta-item {{
      border-top: 1px solid var(--line);
      padding-top: 8px;
      min-width: 0;
    }}
    .trace-run-meta-item .label {{
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0;
    }}
    .trace-run-meta-item .value {{
      font-weight: 700;
      margin-top: 3px;
      overflow-wrap: anywhere;
    }}
    .trace-timeline {{
      border: 1px solid var(--line);
      border-radius: 7px;
      overflow: hidden;
      background: #fff;
    }}
    .trace-timeline-row {{
      display: grid;
      grid-template-columns: minmax(125px, 0.8fr) minmax(220px, 1.6fr) minmax(170px, 1fr) minmax(120px, 0.8fr) minmax(110px, 0.7fr);
      gap: 10px;
      align-items: start;
      padding: 9px 10px;
      border-bottom: 1px solid var(--line);
      min-width: 0;
    }}
    .trace-timeline-row:last-child {{
      border-bottom: 0;
    }}
    .trace-timeline-row[data-trace-open] {{
      cursor: pointer;
    }}
    .trace-timeline-row[data-trace-open]:hover {{
      background: #f8faf9;
    }}
    .trace-timeline-row.selected {{
      background: #f4f8f5;
      box-shadow: inset 3px 0 0 var(--accent);
    }}
    .trace-timeline-head {{
      background: #f3f5f4;
      color: var(--muted);
      font-size: 11px;
      font-weight: 750;
      text-transform: uppercase;
      letter-spacing: 0;
    }}
    .trace-step-name {{
      font-weight: 700;
      overflow-wrap: anywhere;
    }}
    .trace-step-detail {{
      color: var(--muted);
      font-size: 12px;
      margin-top: 2px;
      overflow-wrap: anywhere;
    }}
    .trace-signal {{
      display: inline-flex;
      align-items: center;
      width: fit-content;
      max-width: 100%;
      border: 1px solid #cfd6dc;
      border-radius: 999px;
      background: #f8faf9;
      padding: 2px 7px;
      color: #4f5961;
      font-size: 11px;
      font-weight: 650;
      overflow-wrap: anywhere;
    }}
    .trace-row-actions {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-top: 6px;
    }}
    .trace-detail-panel {{
      display: grid;
      gap: 12px;
      margin-top: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 12px;
    }}
    .trace-detail-head {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .trace-detail-title {{
      font-size: 16px;
      font-weight: 750;
      overflow-wrap: anywhere;
    }}
    .trace-detail-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 8px;
    }}
    .trace-detail-cell {{
      border-top: 1px solid var(--line);
      padding-top: 7px;
      min-width: 0;
    }}
    .trace-readiness-list {{
      display: grid;
      gap: 6px;
    }}
    .trace-readiness-item {{
      display: grid;
      grid-template-columns: minmax(130px, 0.8fr) minmax(0, 1fr);
      gap: 8px;
      align-items: start;
      border-top: 1px solid var(--line);
      padding-top: 6px;
    }}
    .trace-json {{
      max-height: 360px;
      overflow: auto;
      white-space: pre-wrap;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #f6f8fa;
      padding: 10px;
      font-size: 11px;
      line-height: 1.45;
    }}
    .trace-storage-stack {{
      display: grid;
      gap: 14px;
      padding: 0 12px 12px;
    }}
    .compact-section-head {{
      margin-bottom: 8px;
    }}
    .trace-detail-disclosure {{
      margin-top: 12px;
    }}
    .label-with-info {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      min-width: 0;
    }}
    .info-dot {{
      display: inline-flex;
      position: relative;
      align-items: center;
      justify-content: center;
      width: 15px;
      height: 15px;
      border: 1px solid #b9c3c9;
      border-radius: 999px;
      color: var(--muted);
      background: #fff;
      font-size: 10px;
      font-weight: 700;
      line-height: 1;
      cursor: help;
      flex: 0 0 auto;
    }}
    .info-dot:hover {{ border-color: var(--accent); color: var(--accent); }}
    .info-dot::after {{
      content: attr(data-tooltip);
      position: absolute;
      top: calc(100% + 7px);
      left: 50%;
      transform: translateX(-50%) translateY(-2px);
      z-index: 50;
      width: min(260px, 70vw);
      padding: 8px 10px;
      border-radius: 6px;
      background: #1b1f23;
      color: #fff;
      box-shadow: 0 8px 18px rgba(27, 31, 35, 0.18);
      font-size: 12px;
      font-weight: 500;
      line-height: 1.35;
      text-align: left;
      white-space: normal;
      opacity: 0;
      pointer-events: none;
      transition: opacity 120ms ease, transform 120ms ease;
    }}
    .info-dot::before {{
      content: "";
      position: absolute;
      top: calc(100% + 3px);
      left: 50%;
      transform: translateX(-50%);
      z-index: 51;
      border: 5px solid transparent;
      border-bottom-color: #1b1f23;
      opacity: 0;
      pointer-events: none;
      transition: opacity 120ms ease;
    }}
    .info-dot:hover::after,
    .info-dot:focus-visible::after {{
      opacity: 1;
      transform: translateX(-50%) translateY(0);
    }}
    .info-dot:hover::before,
    .info-dot:focus-visible::before {{
      opacity: 1;
    }}
    .analysis-card .value {{ font-size: 22px; font-weight: 750; margin-top: 4px; }}
        .analysis-chart {{
          width: 100%;
          min-height: 190px;
          border: 1px solid var(--line);
      border-radius: 7px;
      background: #fbfcfb;
      padding: 10px;
      margin: 10px 0 14px;
        }}
        .analysis-chart svg {{ display: block; width: 100%; height: 180px; overflow: visible; }}
        .trace-diagnostic-chart {{
          min-height: 230px;
        }}
        .trace-diagnostic-chart svg {{
          height: 205px;
        }}
        .trace-chart-legend {{
          display: flex;
          flex-wrap: wrap;
          gap: 8px 14px;
          margin-top: 8px;
          font-size: 12px;
          color: var(--muted);
        }}
        .trace-chart-key {{
          display: inline-flex;
          align-items: center;
          gap: 6px;
          min-width: 0;
        }}
        .trace-chart-swatch {{
          width: 10px;
          height: 10px;
          border-radius: 2px;
          flex: 0 0 auto;
        }}
        .pill.trace-diagnostic-pill {{
          color: #4b5563;
          border-color: #d1d5db;
          background: #f3f4f6;
        }}
        .score-legend {{
          display: flex;
          flex-wrap: wrap;
          gap: 8px 14px;
          margin: 6px 0 0;
          color: var(--muted);
          font-size: 12px;
        }}
        .score-legend-item {{
          display: inline-flex;
          align-items: center;
          gap: 6px;
        }}
        .score-swatch {{
          width: 10px;
          height: 10px;
          border-radius: 2px;
          flex: 0 0 auto;
        }}
    .analysis-scaffold-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(180px, 1fr));
      gap: 10px;
      padding: 0 12px 12px;
    }}
    .dashboard-details {{
      display: block;
    }}
    .dashboard-details[open] summary {{
      border-bottom: 1px solid var(--line);
      margin-bottom: 10px;
    }}
    .secondary-analysis-stack {{
      display: grid;
      gap: 16px;
      padding: 0 12px 12px;
    }}
    .analysis-scaffold-card {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fbfcfb;
      padding: 10px;
      min-width: 0;
    }}
    .analysis-scaffold-card h3 {{
      margin: 0 0 4px;
      font-size: 13px;
    }}
    .scaffold-visual {{
      margin-top: 10px;
      min-height: 74px;
      display: grid;
      align-items: end;
      gap: 8px;
    }}
    .scaffold-line {{
      width: 100%;
      height: 72px;
      border: 1px solid #edf0f2;
      border-radius: 6px;
      background: #fff;
    }}
    .scaffold-bars {{
      display: grid;
      gap: 7px;
      margin-top: 9px;
    }}
    .scaffold-bar-row {{
      display: grid;
      grid-template-columns: minmax(96px, 140px) minmax(0, 1fr) 48px;
      gap: 7px;
      align-items: center;
      font-size: 12px;
    }}
    .scaffold-heatmap {{
      display: grid;
      grid-template-columns: repeat(6, minmax(18px, 1fr));
      gap: 5px;
      margin-top: 10px;
    }}
    .scaffold-heat-cell {{
      min-height: 22px;
      border: 1px solid #d9dee3;
      border-radius: 4px;
      background: #eef3f5;
    }}
    .scaffold-note {{
      color: var(--muted);
      font-size: 11px;
      margin-top: 7px;
    }}
    .mini-bars {{
      display: grid;
      gap: 6px;
      margin-top: 9px;
    }}
    .mini-bar-row {{
      display: grid;
      grid-template-columns: minmax(82px, 108px) minmax(0, 1fr) 48px;
      gap: 7px;
      align-items: center;
      font-size: 12px;
    }}
    .mini-bar-track {{
      height: 7px;
      border-radius: 999px;
      background: #e7edf0;
      overflow: hidden;
    }}
    .mini-bar-fill {{
      height: 100%;
      border-radius: inherit;
      background: var(--accent);
      min-width: 2px;
    }}
    .analysis-table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 8px;
    }}
    .analysis-table th, .analysis-table td {{
      border-bottom: 1px solid #edf0f2;
      padding: 8px 6px;
      text-align: left;
      vertical-align: top;
    }}
    .analysis-table th {{ color: var(--muted); font-size: 12px; font-weight: 650; }}
    .trace-table-wrap {{
      max-width: 100%;
      overflow-x: auto;
    }}
    .analysis-table.trace-table {{
      table-layout: fixed;
      min-width: 0;
    }}
    .analysis-table.trace-table th,
    .analysis-table.trace-table td {{
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .ledger-list {{
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }}
    .followup-list {{
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }}
    .followup-item {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: start;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      min-width: 0;
    }}
    .followup-title {{
      display: flex;
      gap: 7px;
      align-items: center;
      flex-wrap: wrap;
      min-width: 0;
    }}
    .followup-title strong {{ overflow-wrap: anywhere; }}
    .followup-detail {{
      margin-top: 5px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .run-score-strip {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 6px;
    }}
    .run-score-channel {{
      display: grid;
      gap: 2px;
      min-width: 86px;
      padding: 5px 7px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #f8fafb;
    }}
    .run-score-channel.pass {{
      border-color: #b7dcc3;
      background: #f3faf5;
    }}
    .run-score-channel.fail {{
      border-color: #f0b8ad;
      background: #fff4f2;
    }}
    .run-score-channel .label {{
      font-size: 10px;
      font-weight: 700;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0;
    }}
    .run-score-channel .value {{
      color: var(--ink);
      font-size: 12px;
      font-weight: 750;
      line-height: 1.2;
    }}
    .followup-actions {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .followup-actions a {{
      text-decoration: none;
    }}
    .ledger-toolbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 10px;
    }}
    .ledger-toggle {{
      border: 1px solid #cfd8df;
      background: #fff;
      color: var(--ink);
      border-radius: 6px;
      padding: 5px 9px;
      font-size: 12px;
      font-weight: 650;
      line-height: 1.2;
      cursor: pointer;
    }}
    .ledger-toggle:hover {{ border-color: #9fb2bf; background: #f7faf8; }}
    .ledger-event {{
      display: grid;
      grid-template-columns: 142px minmax(0, 1fr);
      gap: 12px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }}
    .ledger-time {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }}
    .ledger-main {{ display: grid; gap: 5px; min-width: 0; }}
    .ledger-title-row {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
      min-width: 0;
    }}
    .ledger-title {{
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
      min-width: 0;
    }}
    .ledger-title strong {{ font-size: 13px; overflow-wrap: anywhere; }}
    .ledger-copy {{
      border: 1px solid #cfd8df;
      background: #fff;
      color: var(--ink);
      border-radius: 6px;
      padding: 4px 8px;
      font-size: 12px;
      font-weight: 650;
      line-height: 1.2;
      cursor: pointer;
      white-space: nowrap;
    }}
    .ledger-copy:hover {{ border-color: #9fb2bf; background: #f7faf8; }}
    .copy-fallback[hidden] {{ display: none; }}
    .copy-fallback {{
      position: fixed;
      right: 18px;
      bottom: 18px;
      z-index: 50;
      display: grid;
      gap: 8px;
      width: min(620px, calc(100vw - 36px));
      max-height: min(70vh, 720px);
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      box-shadow: 0 18px 48px rgba(27, 31, 35, 0.18);
    }}
    .copy-fallback-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }}
    .copy-fallback textarea {{
      width: 100%;
      min-height: 220px;
      max-height: 48vh;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      font: 12px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }}
    .ledger-meta {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
    }}
    .ledger-detail {{ color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }}
    .prompt-average-groups {{
      display: grid;
      grid-template-columns: repeat(2, minmax(260px, 1fr));
      gap: 14px;
      margin-top: 10px;
    }}
    .prompt-average-group {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fbfcfb;
      padding: 10px;
      min-width: 0;
    }}
    .prompt-average-group h3 {{
      margin: 0 0 8px;
      font-size: 13px;
    }}
    .prompt-average-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 108px 48px;
      gap: 8px;
      align-items: center;
      margin: 7px 0;
    }}
    .metric-lines {{ display: grid; gap: 3px; }}
    .metric-line {{ overflow-wrap: anywhere; }}
    .case-actions {{ display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }}
    .analysis-toggle.excluded {{ border-color: #d4a373; color: var(--warn); }}
    .prompt-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 4px;
    }}
    .prompt-copy {{
      min-height: 28px;
      padding: 4px 8px;
      font-size: 12px;
    }}
    .copy-source {{
      position: fixed;
      left: -9999px;
      top: 0;
      width: 1px;
      height: 1px;
      opacity: 0;
      pointer-events: none;
    }}
    .segmented {{ display: inline-flex; border: 1px solid var(--line); border-radius: 7px; overflow: hidden; }}
    .segmented button {{
      border: 0;
      border-radius: 0;
      min-height: 30px;
      background: #fff;
      padding: 5px 9px;
    }}
    .segmented button.active {{ background: #edf4ea; color: #2f5437; font-weight: 650; }}
    .filters {{ display: flex; gap: 8px; margin: 0 0 12px; flex-wrap: wrap; }}
    input, select {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      background: #fff;
      color: var(--ink);
      min-height: 36px;
      max-width: 100%;
    }}
    input {{ min-width: 280px; flex: 1; }}
    .case-list {{ display: grid; gap: 12px; min-width: 0; }}
    .prompt-list {{ display: grid; gap: 10px; min-width: 0; }}
    .case-card {{ padding: 14px; min-width: 0; width: 100%; }}
        .prompt-card {{
          background: var(--panel);
          border: 1px solid var(--line);
          border-radius: 8px;
          padding: 12px;
        }}
        .analysis-handoff {{
          display: flex;
          gap: 8px;
          align-items: center;
          flex-wrap: wrap;
          margin-top: 10px;
        }}
        .secondary-action {{
          display: inline-flex;
          align-items: center;
          min-height: 30px;
          padding: 5px 9px;
          border: 1px solid var(--line);
          border-radius: 6px;
          background: #fff;
          color: var(--ink);
          text-decoration: none;
          font: inherit;
          cursor: pointer;
        }}
        .db-actions {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
        .db-table-wrap {{
          width: 100%;
          max-width: 100%;
          min-width: 0;
          overflow-x: auto;
          border: 1px solid var(--line);
          border-radius: 8px;
          background: var(--panel);
        }}
        table {{ border-collapse: collapse; min-width: 1120px; width: 100%; }}
        table.analysis-table {{ min-width: 0; }}
        table.eval-db-table {{
          table-layout: fixed;
          min-width: 5260px;
        }}
        table.eval-db-table col.prompt-id {{ width: 68px; }}
        table.eval-db-table col.case-col {{ width: 240px; }}
        table.eval-db-table col.agent-col {{ width: 165px; }}
        table.eval-db-table col.time-col {{ width: 155px; }}
        table.eval-db-table col.source-col {{ width: 115px; }}
        table.eval-db-table col.run-col {{ width: 250px; }}
        table.eval-db-table col.prompt-col {{ width: 440px; }}
        table.eval-db-table col.response-col {{ width: 500px; }}
        table.eval-db-table col.summary-col {{ width: 140px; }}
        table.eval-db-table col.score-col {{ width: 230px; }}
        table.eval-db-table col.dimension-score-col {{ width: 104px; }}
        table.eval-db-table col.evidence-col {{ width: 260px; }}
        table.eval-db-table col.notes-col {{ width: 220px; }}
        table.eval-db-table td.mono {{
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
          overflow-wrap: normal;
          word-break: normal;
        }}
        .score-metric-strip {{
          display: flex;
          flex-wrap: wrap;
          gap: 4px;
          max-height: 74px;
          overflow: hidden;
        }}
        .score-chip {{
          display: inline-flex;
          align-items: center;
          border: 1px solid var(--line);
          border-radius: 999px;
          background: #fbfcfb;
          padding: 1px 6px;
          color: var(--muted);
          font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
          font-size: 11px;
          white-space: nowrap;
        }}
        table.eval-db-table td.score-dimension-cell {{
          text-align: center;
          white-space: nowrap;
        }}
        table.eval-db-table td:nth-child(2),
        table.eval-db-table td:nth-child(3) {{
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
          overflow-wrap: normal;
        }}
        table.eval-db-table td:nth-child(4),
        table.eval-db-table td:nth-child(5),
        table.eval-db-table td:nth-child(6) {{
          white-space: nowrap;
        }}
        th, td {{
          border-bottom: 1px solid #edf0f2;
          padding: 7px 10px;
          text-align: left;
          vertical-align: top;
        }}
        th {{
          color: var(--muted);
          font-size: 12px;
          font-weight: 650;
          background: #f7f9fa;
          position: sticky;
          top: 0;
        }}
        .metric-th {{
          display: grid;
          gap: 3px;
          line-height: 1.25;
        }}
        .metric-th-title {{
          color: var(--ink);
          font-weight: 700;
        }}
        .metric-th-detail {{
          color: var(--muted);
          font-size: 11px;
          font-weight: 500;
          overflow-wrap: anywhere;
        }}
        .db-empty {{ color: var(--muted); }}
        td.prompt-cell {{
          line-height: 1.35;
        }}
        td.response-cell {{
          line-height: 1.35;
        }}
        .db-cell-text {{
          display: -webkit-box;
          -webkit-box-orient: vertical;
          -webkit-line-clamp: 2;
          overflow: hidden;
          overflow-wrap: anywhere;
          word-break: normal;
          line-height: 1.35;
        }}
        td.prompt-cell .db-cell-text {{
          -webkit-line-clamp: 3;
        }}
        td.response-cell .db-cell-text {{
          -webkit-line-clamp: 4;
          white-space: pre-wrap;
        }}
        a.pill {{ color: inherit; text-decoration: none; }}
    .case-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 10px;
      margin-bottom: 12px;
    }}
    .case-id {{ font-weight: 650; overflow-wrap: anywhere; }}
    .case-meta {{ display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }}
    .case-body {{ display: grid; gap: 10px; }}
    .case-body > section + section {{
      border-top: 1px solid #e4e8eb;
      padding-top: 10px;
    }}
    .detail-label {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      margin-bottom: 4px;
    }}
    .text-block {{
      background: var(--soft);
      border: 1px solid #dde5ea;
      border-radius: 6px;
      padding: 9px 10px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}
    .scoring-contract {{
      border: 1px solid #dde5ea;
      border-radius: 6px;
      background: #fbfcfb;
      padding: 8px 10px;
      display: grid;
      gap: 6px;
    }}
    .scoring-contract-row {{
      display: grid;
      grid-template-columns: minmax(140px, 220px) minmax(0, 1fr);
      gap: 10px;
      align-items: start;
    }}
    .scoring-contract-key {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      overflow-wrap: anywhere;
    }}
    .scoring-contract-value {{
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    .scoring-contract-value .pill {{
      margin: 0;
      background: #f1f4f6;
      border-color: #d6d9dc;
      color: #4f5b62;
    }}
    @media (max-width: 760px) {{
      .scoring-contract-row {{ grid-template-columns: 1fr; gap: 3px; }}
    }}
    .score-grid {{ display: flex; flex-wrap: wrap; gap: 5px; }}
    .review-form {{
      border-top: 1px solid var(--line);
      margin-top: 2px;
      padding-top: 10px;
    }}
    .review-form[aria-disabled="true"] {{
      opacity: 0.72;
    }}
    .review-form button:disabled,
    .review-form select:disabled,
    .review-form textarea:disabled {{
      cursor: not-allowed;
    }}
    .score-controls {{
      display: grid;
      grid-template-columns: repeat(4, minmax(150px, 1fr));
      gap: 8px;
      margin-bottom: 10px;
    }}
    .score-control label {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      margin-bottom: 3px;
    }}
    .score-control select {{ width: 100%; }}
    textarea {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      min-height: 70px;
      width: 100%;
      resize: vertical;
      font: inherit;
    }}
    .form-actions {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin-top: 8px;
      flex-wrap: wrap;
    }}
    button {{
      border: 1px solid #9a927f;
      border-radius: 6px;
      background: #f7f9fa;
      color: var(--ink);
      min-height: 34px;
      padding: 7px 11px;
      font: inherit;
      cursor: pointer;
    }}
    button:hover {{ background: #eef3f5; }}
    .review-status {{ color: var(--muted); }}
    .pill {{
      display: inline-flex;
      align-items: center;
      width: fit-content;
      max-width: 100%;
      min-height: 22px;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 12px;
      line-height: 1.2;
      border: 1px solid var(--line);
      margin: 0 4px 4px 0;
      white-space: nowrap;
    }}
    .pass {{ color: var(--good); border-color: #a7d7bd; background: #eef8f2; }}
    .fail {{ color: var(--bad); border-color: #f0b8b2; background: #fff1f0; }}
    .warn {{ color: var(--warn); border-color: #d6d9dc; background: var(--soft-warn); }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }}
    .truncate {{ overflow: hidden; text-overflow: ellipsis; }}
    @media (max-width: 980px) {{
      main, header {{ padding-left: 16px; padding-right: 16px; }}
      .kpis {{ grid-template-columns: repeat(3, minmax(130px, 1fr)); }}
      .workspace {{ grid-template-columns: 1fr; }}
      .side-nav {{
        position: static;
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }}
      .side-nav button {{ text-align: center; }}
      .grid {{ grid-template-columns: 1fr; }}
      .section-head {{ align-items: flex-start; flex-wrap: wrap; }}
      .score-row {{ grid-template-columns: minmax(0, 1fr) 62px 62px; }}
      .bar-row {{ grid-template-columns: minmax(0, 1fr) 100px 54px; }}
      .prompt-average-groups {{ grid-template-columns: 1fr; }}
      .prompt-average-row {{ grid-template-columns: minmax(0, 1fr) 92px 44px; }}
      .score-controls {{ grid-template-columns: repeat(2, minmax(140px, 1fr)); }}
      .status-grid {{ grid-template-columns: 1fr; }}
      .analysis-grid {{ grid-template-columns: repeat(2, minmax(130px, 1fr)); }}
      .trace-contract {{ grid-template-columns: 1fr; }}
      .trace-run-meta {{ grid-template-columns: repeat(2, minmax(120px, 1fr)); }}
      .trace-timeline-row {{ grid-template-columns: minmax(0, 1fr); gap: 4px; }}
      .trace-timeline-head {{ display: none; }}
      .workflow-steps {{ grid-template-columns: repeat(2, minmax(140px, 1fr)); }}
      input, select {{ min-width: 0; width: 100%; }}
      .case-head {{ display: block; }}
    }}
    @media (max-width: 640px) {{
      .kpis {{ grid-template-columns: repeat(2, minmax(130px, 1fr)); }}
      .trace-run-top {{ display: block; }}
      .trace-run-meta {{ grid-template-columns: 1fr; }}
      .workflow-steps {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <div class="dashboard-intro">
      <p>Track eval prompts, machine checks, Slack runs, human reviews, and before/after movement from prompt or tool fixes.</p>
      <details class="intro-details">
        <summary>Scoring notes</summary>
        <div class="detail-content">
          <span>Promptfoo scores are backend assertion checks; manual human and Orchestrator Review scorecards are separate quality reviews.</span>
          <span>Saved reviews create evidence for fixes and regression cases, but do not retrain agents automatically.</span>
          <span>Source database: {html.escape(str(payload["database_path"]))}</span>
        </div>
      </details>
    </div>
  </header>
  <main>
        <section class="kpis">
          {_kpi("Agent Coverage", f'{summary["coverage_complete_agents"]}/{len(summary["coverage"])} agents ready', f'{summary["coverage_target_label"]}; {summary["coverage_gap_total"]} gaps')}
          {_kpi("Total Cases", summary["total_cases"], f'{summary["seed_case_total"]} committed; {summary["non_seed_cases"]} ad hoc')}
          {_kpi("Data Quality", data_quality_label, data_quality_hint)}
          {_kpi("Scoring Health", dashboard_health_label, dashboard_health_hint)}
              {_kpi("Machine Pass Rate", f'{summary["promptfoo_pass_rate"]}%', f'{summary["promptfoo_evaluated"]} checked; {summary["promptfoo_pending"]} pending')}
              {_kpi("Machine Avg / 5", _score_out_of_five(summary["machine_average"], machine=True), f'{summary["machine_scored"]} scored cases', "machine")}
          {_kpi("Slack Runs", summary["slack_run_rows"], f'{summary["slack_linked"]} linked cases; {summary["slack_retry_cases"]} retry-heavy')}
          {_kpi("Slack Evidence", summary["slack_evidence_ready"], f'{summary["slack_with_warnings"]} runs with warnings')}
          {_kpi("Human Avg / 5", _score_out_of_five(summary["human_average"]), f'{summary["human_reviewed"]} manual review{"s" if summary["human_reviewed"] != 1 else ""}', "human")}
          {_kpi("Orchestrator Review Avg / 5", _score_out_of_five(summary["orchestrator_judge_average"]), f'{summary["orchestrator_judge_reviewed"]} saved review forms', "human")}
        </section>
    <section class="workspace">
          <nav class="side-nav" aria-label="Scoring sections">
            <button type="button" data-view="overview" class="active">Overview</button>
            <button type="button" data-view="prompts">Prompts</button>
            <button type="button" data-view="runs">Runs & scoring</button>
            <button type="button" data-view="database">Database</button>
            <button type="button" data-view="traces">Traces</button>
            <button type="button" data-view="analysis">Analysis</button>
          </nav>
      <div class="view-stack">
        <section id="view-overview" class="view active">
          <div class="panel">
            <section class="subsection">
              <div class="section-head">
                <div>
                  <h2 class="subsection-title">{_label_with_info("Slack Eval Conversation Flow")}</h2>
                  <div class="subtle">Cost-safe #evals loop. Stage bars use saved database rows; no Slack or OpenAI call runs from this view.</div>
                </div>
              </div>
              <div id="workflow-visualization" class="workflow-grid"></div>
            </section>
            <section class="subsection">
              <div class="section-head">
                <div>
                  <h2 class="subsection-title">{_label_with_info("Overview Latest Run")}</h2>
                  <div class="subtle">Newest saved run or scoring event from the same merged case model used by Runs & Scoring.</div>
                </div>
              </div>
              <div id="overview-latest-run"></div>
            </section>
            <section class="subsection">
              <div class="section-head">
                <div>
                  <h2 class="subsection-title">{_label_with_info("API Spend Readiness Gates")}</h2>
                  <div class="subtle">Local data checks that should pass before paid Slack/API eval runs are trusted.</div>
                </div>
              </div>
              <div id="data-quality-gates"></div>
            </section>
            <section class="subsection">
              <div class="section-head" id="agent-score-drilldown" tabindex="-1">
                <div>
                      <h2 class="subsection-title" id="agent-score-title">{_label_with_info("Agent Scores")}</h2>
                      <div class="subtle" id="agent-score-note">Machine checks, manual human scorecards, and Orchestrator Review scorecards are tracked separately.</div>
                </div>
                <div class="segmented" aria-label="Agent score metric">
                  <button type="button" data-score-view="machine" class="active">Machine</button>
                  <button type="button" data-score-view="human">Human</button>
                  <button type="button" data-score-view="orchestrator_judge">Orchestrator Review</button>
                </div>
              </div>
              <div id="agent-score-table" class="score-table"></div>
            </section>
            <section class="subsection">
              <div class="section-head">
                <div>
                  <h2 class="subsection-title">{_label_with_info("Prompt Coverage")}</h2>
              <div class="subtle">Committed eval prompt count by agent; targets are {summary["coverage_target_label"]}.</div>
                </div>
                <span class="subtle">{summary["coverage_gap_total"]} remaining</span>
              </div>
              <div id="agent-coverage-bars"></div>
            </section>
            <section class="subsection">
              <h2 class="subsection-title">{_label_with_info("Evaluation Dimensions")}</h2>
              <div class="subtle">Top quality areas across committed eval prompts.</div>
              <div id="dimension-bars"></div>
            </section>
          </div>
        </section>
        <section id="view-prompts" class="view">
          <div class="panel">
            <div class="section-head">
              <div>
                <h2>{_label_with_info("Prompt Library")}</h2>
                <div class="subtle">Committed Slack eval prompts. Use Copy prompt to paste a case into #evals.</div>
              </div>
            </div>
            <div class="filters">
              <input id="prompt-search" placeholder="Search prompt, case, agent, dimension">
              <select id="prompt-agent-filter"><option value="">All agents</option></select>
            </div>
            <div id="prompt-rows" class="prompt-list"></div>
          </div>
        </section>
            <section id="view-runs" class="view">
            <div class="panel">
              <div class="section-head">
                <div>
                  <h2>{_label_with_info("Runs & Scoring")}</h2>
                  <div class="subtle">Case-level scoring surface for Promptfoo checks, Slack runs, review forms, and analysis inclusion.</div>
                </div>
              </div>
              <div class="filters">
                <input id="search" placeholder="Search case, agent, dimension, ask, run id">
                <select id="agent-filter"><option value="">All agents</option></select>
                <select id="state-filter">
                  <option value="">All states</option>
                  <option value="pass">Auto check pass</option>
                  <option value="fail">Auto check fail</option>
                  <option value="pending">Machine eval pending</option>
                  <option value="human">Human reviewed</option>
                  <option value="slack">Slack test completed</option>
                </select>
              </div>
              <section class="subsection">
                <div class="section-head">
                  <div>
                    <h2 class="subsection-title">{_label_with_info("Latest Saved Runs")}</h2>
                    <div class="subtle">Newest saved Slack, machine, human, and Orchestrator Review activity from the merged case model. This is read-only and does not run Slack or APIs.</div>
                  </div>
                </div>
                <div id="latest-saved-runs"></div>
              </section>
              <section class="subsection">
                <div class="section-head">
                  <div>
                    <h2 class="subsection-title">{_label_with_info("Eval Follow-up Queue")}</h2>
                    <div class="subtle">Current cases needing the next manual step, ordered by oldest saved run first; pending no-run cases follow. Built from saved checklist gaps; no Slack or API call runs here.</div>
                  </div>
                </div>
                <div id="eval-follow-up-queue"></div>
              </section>
              <section class="subsection">
                <div class="section-head">
                  <div>
                    <h2 class="subsection-title">{_label_with_info("Eval Run Ledger")}</h2>
                    <div class="subtle">Chronological local events across Slack runs, Promptfoo imports, human reviews, and trace summaries.</div>
                  </div>
                </div>
                <div id="eval-run-ledger"></div>
              </section>
              <div id="case-rows" class="case-list"></div>
            </div>
        </section>
            <section id="view-analysis" class="view">
              <div class="panel">
                <div class="section-head">
                  <div>
                    <h2>{_label_with_info("Promptfoo Run Analysis")}</h2>
                  <div class="subtle">DB-backed comparison of included runs by time, case, agent, and dimension.</div>
                  </div>
                </div>
                <div id="analysis-summary" class="analysis-grid"></div>
                <section class="subsection">
                  <div class="section-head">
                    <div>
                      <h2 class="subsection-title">{_label_with_info("Average Score Across Time")}</h2>
                      <div class="subtle">All agents by default; choose an agent to compare daily machine, human, and Orchestrator Review averages. Same-day prompt retries are averaged before rollup.</div>
                    </div>
                    <select id="analysis-agent-trend-filter"><option value="all">All agents</option></select>
                  </div>
                  <div id="analysis-run-chart" class="analysis-chart"></div>
                  <div id="analysis-agent-trend-table"></div>
                </section>
                <section class="subsection">
                  <div class="section-head">
                    <div>
                      <h2 class="subsection-title">{_label_with_info("Single Prompt Trend")}</h2>
                      <div class="subtle">Daily machine, human, and Orchestrator Review lines for one prompt; duplicate same-day runs are averaged.</div>
                    </div>
                    <select id="analysis-case-filter"><option value="">Select prompt</option></select>
                  </div>
                  <div id="analysis-case-trend-chart" class="analysis-chart"></div>
                  <div id="analysis-case-trend-table"></div>
                </section>
                <section class="subsection">
                  <h2 class="subsection-title">{_label_with_info("Prompt Average Scores by Agent")}</h2>
                  <div class="subtle">Prompt-level machine averages grouped by agent.</div>
                  <div id="analysis-prompt-average-chart"></div>
                </section>
                <details class="subsection dashboard-details">
                  <summary>
                    <span>Secondary diagnostics</span>
                    <span class="subtle">agent stability, failures, and case movement</span>
                  </summary>
                  <div class="secondary-analysis-stack">
                    <section>
                      <h2 class="subsection-title">{_label_with_info("Agent Stability")}</h2>
                      <div class="subtle">Pass rate, average score, score range, and flaky cases.</div>
                      <div id="analysis-agent-stability"></div>
                    </section>
                    <section>
                      <h2 class="subsection-title">{_label_with_info("Failure Clusters by Dimension")}</h2>
                      <div class="subtle">Failed assertions grouped by eval dimension.</div>
                      <div id="analysis-dimension-failures"></div>
                    </section>
                    <section>
                      <h2 class="subsection-title">{_label_with_info("Case Changes Across Runs")}</h2>
                      <div class="subtle">Latest timestamp and score movement versus the previous run.</div>
                      <div id="analysis-case-changes"></div>
                    </section>
                  </div>
                </details>
                <details class="subsection dashboard-details">
                  <summary>
                    <span>Planned chart backlog</span>
                    <span class="subtle">optional visuals once more run/review data exists</span>
                  </summary>
                  <div id="analysis-chart-scaffolds" class="analysis-scaffold-grid"></div>
                </details>
              </div>
            </section>
            <section id="view-traces" class="view">
              <div class="panel">
                <div class="section-head">
                  <div>
                    <h2>{_label_with_info("Trace Explorer")}</h2>
                    <div class="subtle">OpenAI-style run timeline plus Keystone eval joins, review scores, diagnostics, and privacy state from sanitized local trace summaries. This view does not call OpenAI or Slack.</div>
                  </div>
                </div>
                <section class="subsection">
                  <div class="section-head compact-section-head">
                    <div>
                      <h2 class="subsection-title">{_label_with_info("Trace Health")}</h2>
                      <div class="subtle">Join coverage, manual/API split, diagnostic signal load, and privacy posture for the saved trace set.</div>
                    </div>
                  </div>
                  <div id="trace-analytics" class="analysis-grid trace-health-grid"></div>
                </section>
                <section class="subsection">
                  <h2 class="subsection-title">{_label_with_info("Trace Diagnostics")}</h2>
                  <div class="subtle">Current cleanup signals from sanitized trace metadata. Missing model or retrieval metadata usually indicates instrumentation gaps, not automatic model failure.</div>
                  <div id="trace-diagnostic-categories"></div>
                  <details class="dashboard-details trace-detail-disclosure">
                    <summary>
                      <span>Diagnostic trends and affected runs</span>
                      <span class="subtle">chart, day table, and case rollups</span>
                    </summary>
                    <div id="trace-diagnostic-trend-chart" class="analysis-chart trace-diagnostic-chart"></div>
                    <div id="trace-diagnostic-trends"></div>
                    <div id="trace-diagnostic-followups"></div>
                  </details>
                </section>
                <section class="subsection">
                  <h2 class="subsection-title">{_label_with_info("Current Run Trace")}</h2>
                  <div class="subtle">Newest run-level trace, joined to case/run identifiers and reduced to the review signals needed for debugging.</div>
                  <div id="trace-reader-guide" class="trace-guide trace-contract"></div>
                  <div id="trace-latest-run" class="trace-run-overview"></div>
                  <div id="trace-current-timeline"></div>
                </section>
                <section class="subsection">
                  <h2 class="subsection-title">{_label_with_info("Trace Event Log")}</h2>
                  <div class="subtle">Recent sanitized workflow events as trace steps. Select a row to inspect the full capped trace packet in the dashboard; Copy keeps the same packet available for Codex review.</div>
                  <div id="trace-events"></div>
                  <div id="trace-event-detail"></div>
                </section>
                <details class="subsection dashboard-details trace-detail-disclosure">
                  <summary>
                    <span>{_label_with_info("Storage & Instrumentation")}</span>
                    <span class="subtle">processor readiness, DB freshness, kept/dropped fields, and implementation notes</span>
                  </summary>
                  <div class="trace-storage-stack">
                    <section>
                      <h2 class="subsection-title">{_label_with_info("Trace Processor Readiness")}</h2>
                      <div class="subtle">Local self-tracing plan for future #evals API runs, including current processor mode and safe capture state.</div>
                      <div id="trace-summary" class="analysis-grid"></div>
                    </section>
                    <section>
                      <h2 class="subsection-title">{_label_with_info("Local DB Freshness")}</h2>
                      <div class="subtle">No-render ingestion counters for the local eval database.</div>
                      <div id="trace-db-freshness" class="analysis-grid"></div>
                    </section>
                    <section>
                      <h2 class="subsection-title">{_label_with_info("What We Store Locally")}</h2>
                      <div id="trace-fields"></div>
                    </section>
                    <section>
                      <h2 class="subsection-title">{_label_with_info("Implementation Contract")}</h2>
                      <div id="trace-implementation"></div>
                    </section>
                  </div>
                </details>
              </div>
            </section>
            <section id="view-database" class="view">
              <div class="panel">
                <div class="section-head">
                  <div>
                    <h2>{_label_with_info("Eval Case Database")}</h2>
                    <div class="subtle">Read-only inventory of case identity, run state, explicit review scores, and evidence. Use Review form or Case bundle for prompts, responses, comments, and rationales.</div>
                  </div>
                  <div class="db-actions">
                    <a class="pill" href="/api/eval-cases.csv">Download CSV</a>
                    <a class="pill" href="/api/eval-cases">JSON</a>
                  </div>
                </div>
                <div class="filters">
                  <input id="database-search" placeholder="Search case, agent, run id, or status">
                  <select id="database-agent-filter"><option value="">All agents</option></select>
                  <select id="database-state-filter">
                    <option value="">All cases</option>
                    <option value="slack">Completed Slack runs</option>
                    <option value="machine">Imported machine checks</option>
                    <option value="human">Human reviewed</option>
                    <option value="pending">Pending runs/checks</option>
                  </select>
                </div>
                <div class="db-table-wrap">
                  <table class="eval-db-table">
                    <colgroup>
                      <col class="prompt-id">
                      <col class="case-col">
                      <col class="agent-col">
                      <col class="time-col">
                      <col class="source-col">
                      <col class="run-col">
                      <col class="summary-col">
                      <col class="summary-col">
                      {database_score_cols}
                      <col class="summary-col">
                      <col class="evidence-col">
                      <col class="summary-col">
                      <col class="actions-col">
                    </colgroup>
                    <thead>
                      <tr>
                        <th><div class="metric-th"><div class="metric-th-title">Prompt ID</div><div class="metric-th-detail">agent sequence</div></div></th>
                        <th><div class="metric-th"><div class="metric-th-title">Case</div><div class="metric-th-detail">case id / prompt instance</div></div></th>
                        <th><div class="metric-th"><div class="metric-th-title">Agent</div><div class="metric-th-detail">owner</div></div></th>
                        <th><div class="metric-th"><div class="metric-th-title">Latest run time</div><div class="metric-th-detail">Slack, machine, or review</div></div></th>
                        <th>Run source</th>
                        <th>Run id / thread / score</th>
                        <th>Machine</th>
                        <th>Human review</th>
                        {database_score_headers}
                        <th>Orchestrator Review</th>
                        <th>Evidence</th>
                        <th>Analysis</th>
                        <th>Details</th>
                      </tr>
                    </thead>
                    <tbody id="database-rows"></tbody>
                  </table>
                </div>
              </div>
            </section>
          </div>
        </section>
  </main>
  <script id="eval-data" type="application/json">{data_json}</script>
  <script>
    const data = JSON.parse(document.getElementById('eval-data').textContent);
    const cases = data.cases;
    const databaseInventory = data.database_inventory || {{rows: []}};
    const scoreDimensions = data.score_dimensions || [];
    const orchestratorJudge = data.orchestrator_judge || {{}};
    const search = document.getElementById('search');
    const agentFilter = document.getElementById('agent-filter');
    const stateFilter = document.getElementById('state-filter');
        const promptSearch = document.getElementById('prompt-search');
        const promptAgentFilter = document.getElementById('prompt-agent-filter');
        const databaseSearch = document.getElementById('database-search');
        const databaseAgentFilter = document.getElementById('database-agent-filter');
        const databaseStateFilter = document.getElementById('database-state-filter');
    const analysisCaseFilter = document.getElementById('analysis-case-filter');
        const analysisAgentTrendFilter = document.getElementById('analysis-agent-trend-filter');
    let ledgerExpanded = false;
    const ledgerDefaultLimit = 12;
    const params = new URLSearchParams(window.location.search);
    if (params.get('case')) {{
      search.value = params.get('case');
    }}
    for (const agent of Object.keys(data.summary.agents).sort()) {{
      const option = document.createElement('option');
      option.value = agent;
      option.textContent = agent;
          agentFilter.appendChild(option);
          promptAgentFilter.appendChild(option.cloneNode(true));
          databaseAgentFilter.appendChild(option.cloneNode(true));
          analysisAgentTrendFilter.appendChild(option.cloneNode(true));
    }}
    if (params.get('agent')) {{
      agentFilter.value = params.get('agent');
    }}
    const labelHelp = {{
      'Agent Coverage': 'How many agent roles already have the target seed-prompt count. This is coverage, not score quality.',
      'Prompt Coverage': 'Prompt inventory by agent. Use it to find agents below the seed target before starting live #evals runs.',
      'Machine Pass Rate': 'Share of imported Promptfoo checks that passed. Pending cases are not counted until a machine-check row exists.',
      'Machine Avg / 5': 'Average Promptfoo assertion score for scored cases only, normalized to the dashboard 0-5 scale.',
      'Slack Runs': 'Saved #evals run rows. The hint shows distinct linked cases and retry-heavy cases separately.',
      'Dashboard Health': 'Whether the local dashboard endpoint and launch manager are reachable from this machine.',
      'Data Quality': 'Preflight gates completed before paid evals: prompt text, run IDs, timestamps, responses, evidence, reviews, traces, and ledger rows.',
      'Slack Evidence': 'Saved Slack runs with thread evidence. Warnings mean the run is present but missing some audit metadata.',
      'Human Avg / 5': 'Average score from submitted manual human scorecards. Orchestrator Review scorecards are tracked separately.',
      'Orchestrator Review Avg / 5': 'Average score from saved Orchestrator Review forms, not the score for the latest imported machine run.',
      'Total Cases': 'Total local eval case rows: committed seed prompts plus any ad hoc cases created during Slack testing.',
      'Paid-run readiness': 'Count of local preflight gates still open before paid Slack/OpenAI eval runs should be trusted.',
      'Human review queue': 'Recorded responses that can be scored now because prompt, response, and run context are already saved.',
      'Review blocked': 'Cases that still need a saved response before the human score form can produce a meaningful review.',
      'Processor mode': 'Trace setting for future API evals. eval_summary stores sanitized timing and routing metadata only.',
      'Saved events': 'Sanitized trace or workflow events already stored locally for dashboard joins and debugging.',
      'Sensitive capture': 'Effective trace privacy setting. This should stay disabled so raw prompts, outputs, and tool I/O are not stored.',
      'Primary use': 'What trace rows are for here: timing, routing, join keys, and refresh debugging without raw content.',
      'Pass-rate change': 'Newest Promptfoo pass rate minus the previous comparable imported run.',
      'Assertion avg change': 'Change in average imported machine-check score on the 0-5 scale between comparable runs.',
      'Case movement': 'Comparable cases that improved, regressed, or stayed flat between the latest two imported runs.',
      'Start with': 'Recommended committed case for the next manual #evals test. It gives the thread a known case id and review link.',
      'Paste into Slack': 'Exact root prompt to paste into #evals. The dashboard does not call the agent from this copy step.',
      'Expected live calls when enabled': 'Where live work would happen later: the root agent run, optional retrieval, review save, and dashboard refresh.',
      'Expected in-thread communication flow': 'Expected #evals thread order from root ask through agent answer, eval footer, Submit Evaluation, and dashboard update.',
      'Slack Eval Conversation Flow': 'End-to-end #evals workflow status from prompt library to saved Slack run, response, scorecard, and analysis row.',
      'Prompt library': 'Committed seed prompts available to start #evals threads. This is the denominator for most workflow counts.',
      'Slack runs': 'Cases with a saved #evals thread run. These should appear after the agent responds in Slack.',
      'Responses': 'Cases with saved response text from Slack or Promptfoo. These are eligible for review scoring.',
      'Scorecards': 'Manual human and Orchestrator Review scorecards saved from the review-form shape, including scores, safety, and notes.',
      'Analysis': 'Cases currently counted in analysis views after excluding duplicates or known problem runs.',
      'Source checks': 'Cases tagged for retrieval or evidence quality, useful for auditing source-backed answers.',
      'API Spend Readiness Gates': 'Local readiness checks that should pass before paying for live Slack/OpenAI eval runs.',
      'Overview Latest Run': 'Newest saved run or score event from the merged dashboard case model, with machine, Orchestrator Review, and human score channels.',
      'Agent Scores': 'Per-agent score table. Machine mode uses Promptfoo imports; review modes use manual human and Orchestrator Review scorecards.',
      'Promptfoo Assertion Avg / 5 by Agent': 'Average Promptfoo assertion score per agent. It is a machine-check signal, not a human quality judgment.',
      'Slack Human Review Avg / 5 by Agent': 'Average submitted human-review score per agent from Slack-linked scorecards.',
      'Orchestrator Review Avg / 5 by Agent': 'Average Orchestrator Review score per agent from saved review-form scorecards.',
      'Evaluation Dimensions': 'Prompt tags showing which behaviors the eval set exercises, such as retrieval, safety, synthesis, and output format.',
      'Prompt Library': 'Searchable list of committed eval prompts that can be copied into #evals as root messages.',
      'Latest Saved Runs': 'Newest saved Slack, machine, human, and Orchestrator Review activity from the merged case model. No Slack or API calls run here.',
      'Runs & Scoring': 'Case-level workspace for latest run state, machine checks, review scorecards, evidence, and analysis inclusion.',
      'Eval Follow-up Queue': 'Prioritized current cases that need a run, evidence fix, machine import, review scorecard, or analysis decision.',
      'Eval Run Ledger': 'Newest-first event feed across Slack runs, Promptfoo imports, review scorecards, and trace events.',
      'Promptfoo Run Analysis': 'Trend and comparison views built from imported Promptfoo rows, manual human reviews, and Orchestrator Review scorecards.',
      'Average Score Across Time': 'Daily average machine, manual human, and Orchestrator Review scores. Multiple same-day rows are averaged for trend readability.',
      'Single Prompt Trend': 'Score history for one selected prompt, useful for seeing whether a specific fix helped.',
      'Prompt Average Scores by Agent': 'Prompt-level machine averages grouped by agent so weak prompts are easier to target.',
      'Agent Stability': 'How consistent each agent is across scored cases: pass rate, average, range, and uneven results.',
      'Failure Clusters by Dimension': 'Failed machine checks grouped by prompt tags to show which behavior areas need fixes.',
          'Case Changes Across Runs': 'Latest score movement for each case compared with its previous imported run.',
          'Trace Explorer': 'OpenAI-style run timeline plus Keystone eval joins, review scores, diagnostics, and privacy state from sanitized local trace summaries.',
          'Trace Health': 'Group-level trace coverage, source split, diagnostics, and privacy posture for saved run summaries.',
          'Trace Diagnostics': 'Chartable run-diagnostic categories from sanitized trace metadata, such as missing model metadata, retrieval gaps, retries, extraction issues, approval gates, and tool failures.',
          'Current Run Trace': 'Newest run-level trace reduced to case/run joins, route, review scores, step timeline, and cleanup signals.',
          'Trace Event Log': 'Recent sanitized workflow events shown as trace steps. Copy exposes capped metadata for local review.',
          'Storage & Instrumentation': 'Processor readiness, database freshness, retained fields, dropped sensitive fields, and implementation notes.',
          'Trace Processor Readiness': 'Sanitized trace-capture readiness for future API evals; disabled is normal for current no-API runs.',
          'Local DB Freshness': 'No-render row counts and latest timestamps from the local eval database tables that feed the dashboard.',
          'Join health': 'How many saved run summaries can be joined back to a case, Slack run, or WorkItem.',
          'Run source split': 'Whether trace rows came from local no-API manual summaries or future SDK/API run summaries.',
          'Diagnostic signals': 'How many chartable diagnostic categories are present across saved trace summaries.',
          'Top cleanup signal': 'Largest current diagnostic bucket by saved event count.',
          'Privacy guardrail': 'Whether raw prompt, response, Slack message, tool I/O, secrets, or PHI capture is disabled.',
          'Slack runs table': 'Rows saved from #evals Slack runs, including run ids, evidence, warnings, and response hashes.',
          'Trace events table': 'Sanitized trace and workflow events used for join checks and diagnostic rollups.',
          'Promptfoo cases table': 'Imported case-level Promptfoo assertion results used for machine scores and analysis.',
          'Promptfoo runs table': 'Imported Promptfoo run-level summaries used for run trends and machine averages.',
          'Human reviews table': 'Submitted local human and Orchestrator Review scorecards used for review trends and quality comparison.',
          'What We Store Locally': 'Trace and workflow fields kept for joins/timing, plus sensitive fields intentionally dropped.',
          'Implementation Contract': 'Environment settings and integration rules needed before sanitized trace capture is enabled.',
      'Eval Case Database': 'Searchable case table combining seed prompts, newest runs first, review status, evidence, and notes.',
    }};
    function infoDot(label, helpText) {{
      const text = helpText || labelHelp[label] || '';
      return text ? `<span class="info-dot" tabindex="0" data-tooltip="${{escapeHtml(text)}}" aria-label="${{escapeHtml(text)}}">i</span>` : '';
    }}
    function labelWithInfo(label, helpText) {{
      return `<span class="label-with-info"><span>${{escapeHtml(label)}}</span>${{infoDot(label, helpText)}}</span>`;
    }}
    function pill(text, cls) {{ return `<span class="pill ${{cls || ''}}">${{escapeHtml(text)}}</span>`; }}
    function escapeHtml(value) {{
      return String(value ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
    }}
    function scoreValue(value) {{
      if (value === null || value === undefined || value === '') return '-';
      const number = Number(value);
      return Number.isFinite(number) ? `${{number.toFixed(1)}}/5` : '-';
    }}
    function stateMatches(item) {{
      const state = stateFilter.value;
      if (!state) return true;
      if (state === 'pass') return item.promptfoo_success === true;
      if (state === 'fail') return item.promptfoo_success === false;
      if (state === 'pending') return item.promptfoo_success === null;
      if (state === 'human') return item.human_average !== null;
      if (state === 'slack') return Number(item.slack_run_count || 0) > 0;
      return true;
    }}
    function databaseStateMatches(item) {{
      const state = databaseStateFilter.value;
      if (!state) return true;
      const hasSlack = Number(item.slack_run_count || 0) > 0;
      const hasMachine = item.promptfoo_success !== null;
      const hasHuman = item.human_average !== null;
      if (state === 'slack') return hasSlack;
      if (state === 'machine') return hasMachine;
      if (state === 'human') return hasHuman;
      if (state === 'pending') return !hasSlack && !hasMachine && !hasHuman;
      return true;
    }}
    function caseHasRuntimeEvidence(item) {{
      return Number(item.slack_run_count || 0) > 0
        || item.promptfoo_success !== null
        || item.human_average !== null
        || item.orchestrator_judge_average !== null
        || Boolean(item.latest_run_id || item.latest_slack_run_id || item.latest_slack_thread_ts)
        || Boolean(item.scoring_completed_at || item.orchestrator_judge_created_at || item.human_created_at);
    }}
    function latestRunMillis(item) {{
      const value = item.latest_run_at
        || item.scoring_completed_at
        || item.orchestrator_judge_created_at
        || item.latest_slack_created_at
        || item.updated_at
        || item.human_created_at
        || '';
      const time = Date.parse(value);
      return Number.isFinite(time) ? time : 0;
    }}
    function caseStableKey(item) {{
      return [
        item.agent || '',
        String(item.display_prompt_number || item.prompt_number || '').padStart(3, '0'),
        item.display_case_id || item.case_id || '',
      ].join(' ');
    }}
    function latestFirstCases(items) {{
      return items.slice().sort((a, b) => {{
        const aTime = latestRunMillis(a);
        const bTime = latestRunMillis(b);
        if (aTime || bTime) return bTime - aTime || caseStableKey(a).localeCompare(caseStableKey(b));
        return caseStableKey(a).localeCompare(caseStableKey(b));
      }});
    }}
        function setActiveView(view, scoreNav = '') {{
          for (const button of document.querySelectorAll('[data-view]')) {{
            const buttonView = button.getAttribute('data-view');
            const buttonScore = button.getAttribute('data-score-nav') || '';
            button.classList.toggle('active', buttonView === view && buttonScore === scoreNav);
          }}
          for (const panel of document.querySelectorAll('.view')) {{
            panel.classList.toggle('active', panel.id === `view-${{view}}`);
          }}
        }}
    function renderBars(target, entries, limit = 0) {{
      const sorted = Object.entries(entries)
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      const visible = limit ? sorted.slice(0, limit) : sorted;
      const remainder = limit && sorted.length > limit ? sorted.slice(limit) : [];
      const remainderTotal = remainder.reduce((sum, [, count]) => sum + Number(count || 0), 0);
      const max = Math.max(1, ...visible.map(([, count]) => Number(count || 0)));
      const rows = visible.map(([label, count]) => `
          <div class="bar-row">
            <div class="truncate" title="${{escapeHtml(label)}}">${{escapeHtml(label)}}</div>
            <div class="bar-track"><div class="bar" style="width:${{Math.round(count / max * 100)}}%"></div></div>
            <div class="mono">${{count}}</div>
          </div>
        `).join('');
      const more = remainder.length
        ? `<div class="subtle">+${{remainder.length}} more dimensions · ${{remainderTotal}} tagged prompts</div>`
        : '';
      document.getElementById(target).innerHTML = rows + more;
    }}
    function renderCoverageBars() {{
      const entries = Object.entries(data.summary.coverage || {{}});
      const max = Math.max(1, ...entries.map(([, stats]) => Number(stats.target || 0)));
      document.getElementById('agent-coverage-bars').innerHTML = entries
        .sort((a, b) => b[1].count - a[1].count || a[0].localeCompare(b[0]))
        .map(([agent, stats]) => {{
          const count = Number(stats.count || 0);
          const target = Number(stats.target || 0);
          const gap = Number(stats.gap || 0);
          return `
            <div class="bar-row">
              <div class="truncate" title="${{escapeHtml(agent)}}">${{escapeHtml(agent)}}</div>
              <div class="bar-track"><div class="bar ${{gap ? 'gap' : ''}}" style="width:${{Math.min(100, Math.round(count / max * 100))}}%"></div></div>
              <div class="mono">${{count}}/${{target}}</div>
            </div>
            ${{gap ? `<div style="margin: -4px 0 8px 0">${{pill(`${{gap}} short`, 'warn')}}</div>` : ''}}
          `;
        }}).join('');
    }}
    let activeScoreView = 'machine';
    function scoreViewMeta(view) {{
      if (view === 'human') {{
        return {{
          title: 'Slack Human Review Avg / 5 by Agent',
          note: 'Average 0-5 score from manual #evals human scorecards.',
          scores: data.summary.human_agent_scores || {{}},
          empty: 'No manual Slack human-review scores have been saved yet.',
          machine: false,
        }};
      }}
      if (view === 'orchestrator_judge') {{
        return {{
          title: 'Orchestrator Review Avg / 5 by Agent',
          note: 'Average 0-5 score from Orchestrator Review scorecards.',
          scores: data.summary.orchestrator_judge_agent_scores || {{}},
          empty: 'No Orchestrator Review scores have been saved yet.',
          machine: false,
        }};
      }}
      return {{
              title: 'Promptfoo Assertion Avg / 5 by Agent',
              note: 'Average backend assertion score from imported Promptfoo runs, normalized to 0-5. This can be easy if assertions are broad.',
        scores: data.summary.machine_agent_scores || {{}},
        empty: 'No Promptfoo machine scores are recorded yet.',
        machine: true,
      }};
    }}
    function renderAgentScoreTable(view = activeScoreView, shouldFocus = false) {{
      activeScoreView = view;
      const meta = scoreViewMeta(view);
      document.getElementById('agent-score-title').innerHTML = labelWithInfo(meta.title);
      document.getElementById('agent-score-note').textContent = meta.note;
      for (const button of document.querySelectorAll('[data-score-view]')) {{
        button.classList.toggle('active', button.getAttribute('data-score-view') === view);
      }}
      for (const card of document.querySelectorAll('[data-drilldown]')) {{
        card.classList.toggle('active', card.getAttribute('data-drilldown') === view);
      }}
      const agents = Object.keys(data.summary.coverage || {{}}).sort();
      const rows = agents.map(agent => {{
        const score = meta.scores[agent] || {{}};
        const average = score.average === undefined ? '-' : scoreOutOfFive(score.average, meta.machine);
        const count = score.count || 0;
        return `
          <div class="score-row">
            <div class="truncate" title="${{escapeHtml(agent)}}">${{escapeHtml(agent)}}</div>
            <div class="mono">${{escapeHtml(average)}}</div>
            <div class="mono">${{count}}</div>
          </div>
        `;
      }}).join('');
      document.getElementById('agent-score-table').innerHTML = rows
        ? `<div class="score-row header"><div>Agent</div><div>Avg</div><div>Cases</div></div>${{rows}}`
        : `<div class="subtle">${{escapeHtml(meta.empty)}}</div>`;
      if (shouldFocus) {{
        document.getElementById('agent-score-drilldown').scrollIntoView({{ block: 'start', behavior: 'smooth' }});
        document.getElementById('agent-score-drilldown').focus({{ preventScroll: true }});
      }}
    }}
    function scoreOutOfFive(value, machine = false) {{
      if (value === null || value === undefined || value === '-') return '-';
      const number = Number(value);
      if (!Number.isFinite(number)) return '-';
      const scaled = machine ? number * 5 : number;
      return `${{scaled.toFixed(1)}}/5`;
    }}
    function scorePills(scores) {{
      const entries = Object.entries(scores || {{}});
      if (!entries.length) return pill('no human scores yet', 'warn');
      return entries
        .sort((a, b) => a[0].localeCompare(b[0]))
        .map(([label, value]) => pill(`${{label}} ${{value}}`, ''))
        .join('');
    }}
    const scoreShortLabels = {{
      accuracy: 'acc',
      relevance: 'rel',
      explainability: 'exp',
      readability: 'read',
      source_quality: 'src',
      search_quality: 'search',
      synthesis_quality: 'synth',
      uniqueness: 'uniq',
      format_quality: 'fmt',
      instruction_following: 'instr',
      usefulness: 'use',
    }};
    function scoreMetricStrip(scores) {{
      const dimensions = data.score_dimensions || Object.keys(scores || {{}});
      const chips = dimensions
        .filter(dimension => scores && scores[dimension] !== null && scores[dimension] !== undefined && scores[dimension] !== '')
        .map(dimension => {{
          const label = scoreShortLabels[dimension] || dimension;
          const number = Number(scores[dimension]);
          const value = Number.isFinite(number) ? number.toFixed(1) : String(scores[dimension]);
          return `<span class="score-chip" title="${{escapeHtml(dimension)}}">${{escapeHtml(label)}} ${{escapeHtml(value)}}</span>`;
        }})
        .join('');
      return chips || '<span class="db-empty">no score columns</span>';
    }}
    function scoreDimensionCell(scores, dimension) {{
      const value = scores ? scores[dimension] : undefined;
      if (value === null || value === undefined || value === '') {{
        return `<td class="mono score-dimension-cell"><span class="db-empty">tbd</span></td>`;
      }}
      const number = Number(value);
      const text = Number.isFinite(number) ? number.toFixed(1) : String(value);
      return `<td class="mono score-dimension-cell" title="${{escapeHtml(dimension)}}">${{escapeHtml(text)}}</td>`;
    }}
    function formatDateTime(value) {{
      if (!value) return '';
      const parsed = new Date(value);
      if (Number.isNaN(parsed.getTime())) return String(value);
      return parsed.toLocaleString([], {{
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      }});
    }}
    function caseTimelinePills(item) {{
      const chips = [];
      chips.push(item.updated_at
        ? pill(`Promptfoo ${{formatDateTime(item.updated_at)}}`, item.promptfoo_success === false ? 'fail' : 'pass')
        : pill('Promptfoo not run yet', 'warn'));
      chips.push(Number(item.slack_run_count || 0) > 0
        ? pill(`Slack ${{formatDateTime(item.latest_slack_created_at) || 'linked'}}`, 'pass')
        : pill('Slack test not run yet', 'warn'));
      chips.push(item.human_created_at
        ? pill(`Human ${{formatDateTime(item.human_created_at)}}`, item.human_safety === 'fail' ? 'fail' : 'pass')
        : pill('Human unreviewed', 'warn'));
      chips.push(item.orchestrator_judge_created_at
        ? pill(`Orchestrator Review ${{formatDateTime(item.orchestrator_judge_created_at)}}`, item.orchestrator_judge_safety === 'fail' ? 'fail' : 'pass')
        : pill('Orchestrator Review unscored', 'warn'));
      return chips.join('');
    }}
    function statusBox(title, main, date, detail, cls = '') {{
      const statusClass = cls ? ` ${{cls}}` : '';
      return `<div class="status-box${{statusClass}}">
        <div class="status-title">${{escapeHtml(title)}}</div>
        <div class="status-main">${{escapeHtml(main)}}</div>
        ${{date ? `<div class="status-date">${{escapeHtml(date)}}</div>` : ''}}
        ${{detail ? `<div class="status-date mono">${{escapeHtml(detail)}}</div>` : ''}}
      </div>`;
    }}
    function mergedScoringStatus(item) {{
      const machineMain = item.promptfoo_success === true
        ? `Pass ${{scoreOutOfFive(item.promptfoo_score, true)}}`
        : item.promptfoo_success === false
          ? `Fail ${{scoreOutOfFive(item.promptfoo_score, true)}}`
          : 'Not run';
      const machineDate = item.updated_at ? formatDateTime(item.updated_at) : '';
          const slackMain = Number(item.slack_run_count || 0) > 0
            ? `${{item.slack_run_count}} saved run${{Number(item.slack_run_count || 0) === 1 ? '' : 's'}}`
            : 'Not tested in Slack';
          const slackDate = item.latest_slack_created_at ? formatDateTime(item.latest_slack_created_at) : '';
          const slackDetail = [
            item.latest_slack_run_id || '',
            item.latest_slack_thread_ts ? `thread ${{item.latest_slack_thread_ts}}` : '',
          ].filter(Boolean).join(' · ');
          const evidenceMain = Number(item.slack_run_count || 0) > 0
            ? `${{item.latest_slack_visible_source_count || 0}}/${{item.latest_slack_source_count || 0}} visible sources`
            : 'No Slack evidence';
          const evidenceDetail = [
            item.latest_slack_thread_fetch_status ? `thread ${{item.latest_slack_thread_fetch_status}}` : '',
            item.latest_slack_cost_profile ? `cost ${{item.latest_slack_cost_profile}}` : '',
            item.latest_slack_sdk_cache_hit_rate !== null && item.latest_slack_sdk_cache_hit_rate !== undefined ? `cache ${{Math.round(Number(item.latest_slack_sdk_cache_hit_rate) * 100)}}%` : '',
            Number(item.latest_slack_warning_count || 0) > 0 ? `${{item.latest_slack_warning_count}} warning${{Number(item.latest_slack_warning_count || 0) === 1 ? '' : 's'}}` : '',
          ].filter(Boolean).join(' · ');
          const humanMain = item.human_average !== null
            ? `${{scoreOutOfFive(item.human_average)}} ${{item.human_safety ? `· ${{item.human_safety}}` : ''}}`
            : 'Unreviewed';
      const humanDate = item.human_created_at ? formatDateTime(item.human_created_at) : '';
      const judgeMain = item.orchestrator_judge_average !== null && item.orchestrator_judge_average !== undefined
        ? `${{scoreOutOfFive(item.orchestrator_judge_average)}} ${{item.orchestrator_judge_safety ? `· ${{item.orchestrator_judge_safety}}` : ''}}`
        : 'Unscored';
      const judgeDate = item.orchestrator_judge_created_at ? formatDateTime(item.orchestrator_judge_created_at) : '';
      const orchestratorComment = item.orchestrator_judge_run_comment || item.orchestrator_judge_notes || '';
      const scoringMain = item.scoring_completed_at
        ? `Review scoring complete · ${{formatDateTime(item.scoring_completed_at)}}`
        : hasRecordedResponse(item) ? 'Missing scorecard' : 'Waiting on response';
      const scoringDetail = Array.isArray(item.scoring_review_kinds) && item.scoring_review_kinds.length
        ? item.scoring_review_kinds.join(' + ')
        : '';
      return `<div class="status-grid">
            ${{statusBox('Promptfoo assertion', machineMain, machineDate, item.promptfoo_eval_id || '', item.promptfoo_success === false ? 'fail' : item.promptfoo_success === true ? 'pass' : 'warn')}}
            ${{statusBox('Slack test run', slackMain, slackDate, slackDetail, Number(item.slack_run_count || 0) > 0 ? 'pass' : 'warn')}}
            ${{statusBox('Slack evidence', evidenceMain, '', evidenceDetail, Number(item.slack_run_count || 0) > 0 ? (Number(item.latest_slack_warning_count || 0) > 0 ? 'warn' : 'pass') : 'warn')}}
            ${{statusBox('Scoring status', scoringMain, '', scoringDetail, item.scoring_completed_at ? 'pass' : 'warn')}}
            ${{statusBox('Human review', humanMain, humanDate, item.human_notes || '', item.human_safety === 'fail' ? 'fail' : item.human_average !== null ? 'pass' : 'warn')}}
            ${{statusBox('Orchestrator Review', judgeMain, judgeDate, orchestratorComment, item.orchestrator_judge_safety === 'fail' ? 'fail' : item.orchestrator_judge_average !== null && item.orchestrator_judge_average !== undefined ? 'pass' : 'warn')}}
          </div>`;
    }}
    function agentScorePills(agentScores) {{
      const entries = Object.entries(agentScores || {{}});
      if (!entries.length) return pill('agent averages not recorded', 'warn');
      return entries
        .sort((a, b) => a[0].localeCompare(b[0]))
        .map(([agent, stats]) => pill(`${{agent}} ${{scoreOutOfFive(stats.average_score, true)}} (${{stats.case_count ?? 0}})`, ''))
        .join('');
    }}
    function renderEvalRuns() {{
      const target = document.getElementById('eval-run-scores');
      if (!target) return;
      const rows = data.eval_runs || [];
      target.innerHTML = rows.length ? rows.map(run => {{
        const total = Number(run.total || 0);
        const successes = Number(run.successes || 0);
        const failures = Number(run.failures || 0);
        const errors = Number(run.errors || 0);
        const passRate = total ? Math.round(successes / total * 100) : 0;
        const state = failures || errors ? 'fail' : (run.average_score === null || run.average_score === undefined ? 'warn' : 'pass');
        const avg = run.average_score === null || run.average_score === undefined
          ? pill('run avg not recorded', 'warn')
              : pill(`assertion avg ${{scoreOutOfFive(run.average_score, true)}}`, state === 'fail' ? 'warn' : 'pass');
        return `<div class="run-card ${{state}}">
          <div class="run-head">
            <div>
              <div class="run-title mono">${{escapeHtml(run.eval_id || '')}}</div>
              <div class="subtle">${{escapeHtml(run.created_at || run.imported_at || '')}}</div>
            </div>
            <div>${{avg}}</div>
          </div>
          <div class="run-stats">
            ${{pill(`${{successes}}/${{total}} passed`, state === 'fail' ? 'warn' : 'pass')}}
            ${{pill(`${{passRate}}% pass rate`, state === 'fail' ? 'warn' : 'pass')}}
            ${{failures ? pill(`${{failures}} failed`, 'fail') : ''}}
            ${{errors ? pill(`${{errors}} errors`, 'fail') : ''}}
          </div>
          <div class="run-meter"><span style="width:${{passRate}}%"></span></div>
          <div class="score-grid" style="margin-top:7px">${{agentScorePills(run.agent_scores)}}</div>
        </div>`;
      }}).join('') : '<div class="subtle">No imported Promptfoo eval runs yet.</div>';
    }}
    function ledgerScoreText(row) {{
      const source = String(row.source || '');
      if (row.score === null || row.score === undefined || row.score === '') return 'score tbd';
      return `score ${{scoreOutOfFive(row.score, source === 'promptfoo')}}`;
    }}
    function ledgerSourceLabel(row) {{
      const source = String(row.source || '');
      if (source === 'orchestrator_judge') return 'Orchestrator Review';
      if (source === 'human') return 'human scoring';
      return source || 'event';
    }}
    function ensureCopyFallback() {{
      let panel = document.getElementById('copy-fallback');
      if (panel) return panel;
      panel = document.createElement('div');
      panel.id = 'copy-fallback';
      panel.className = 'copy-fallback';
      panel.hidden = true;
      panel.innerHTML = `
        <div class="copy-fallback-head">
          <strong>Copy manually</strong>
          <button class="ledger-copy" type="button" data-copy-fallback-close>Close</button>
        </div>
        <textarea aria-label="Copy fallback text" data-copy-fallback-text></textarea>
        <div class="subtle">Clipboard access was blocked. The text is selected so it can be copied manually.</div>`;
      document.body.appendChild(panel);
      panel.querySelector('[data-copy-fallback-close]').addEventListener('click', () => {{
        panel.hidden = true;
      }});
      return panel;
    }}
    function showCopyFallback(text) {{
      const panel = ensureCopyFallback();
      const textarea = panel.querySelector('[data-copy-fallback-text]');
      textarea.value = text || '';
      panel.hidden = false;
      textarea.focus();
      textarea.select();
    }}
    async function writeClipboardReviewText(button, text) {{
      try {{
        await navigator.clipboard.writeText(text || '');
        button.textContent = 'Copied';
        return true;
      }} catch (error) {{
        showCopyFallback(text || '');
        button.textContent = 'Manual copy';
        return false;
      }}
    }}
    function ledgerReviewText(row) {{
      const payload = {{
        source: String(row.source || ''),
        status: String(row.status || ''),
        timestamp: String(row.timestamp || ''),
        display_time: formatDateTime(row.timestamp) || String(row.timestamp || ''),
        case_id: String(row.case_id || ''),
        run_id: String(row.run_id || ''),
        agent: String(row.agent || ''),
        score: ledgerScoreText(row),
        details: String(row.details || ''),
        dashboard_url: String(row.dashboard_url || ''),
        review_url: String(row.review_url || ''),
        case_bundle_url: String(row.case_bundle_url || ''),
        review_checklist: Array.isArray(row.review_checklist) ? row.review_checklist : [],
        next_follow_up: String(row.next_follow_up || ''),
      }};
      return [
        'Please review this Keystone eval run ledger entry and identify any scoring, evidence, dashboard, or workflow follow-up needed. Use the review_checklist and next_follow_up fields first.',
        '',
        '```json',
        JSON.stringify(payload, null, 2),
        '```',
      ].join('\\n');
    }}
    async function copyLedgerReview(button) {{
      const original = button.textContent;
      const index = Number(button.getAttribute('data-ledger-index') || -1);
      const row = (data.run_ledger || [])[index];
      if (!row) {{
        button.textContent = 'Missing';
        setTimeout(() => {{ button.textContent = original; }}, 1200);
        return;
      }}
      try {{
        await writeClipboardReviewText(button, ledgerReviewText(row));
      }} catch (error) {{
        button.textContent = 'Manual copy';
      }}
      setTimeout(() => {{ button.textContent = original; }}, 1200);
    }}
    function filteredRunCases() {{
      const query = search.value.trim().toLowerCase();
      return latestFirstCases(cases.filter(item => {{
        const haystack = [
          item.case_id, item.display_case_id, item.agent, (item.dimensions || []).join(' '),
          item.user_input, item.response_text, item.latest_slack_summary,
          item.latest_run_id, item.latest_slack_run_id, item.latest_slack_thread_ts,
          item.latest_slack_context_policy, item.latest_slack_thread_fetch_status,
          item.latest_slack_cost_profile, item.human_notes,
          item.orchestrator_judge_notes, item.orchestrator_judge_run_comment
        ].join(' ').toLowerCase();
        return (!query || haystack.includes(query))
          && (!agentFilter.value || item.agent === agentFilter.value)
          && stateMatches(item);
      }}));
    }}
    function renderLatestSavedRuns() {{
      const target = document.getElementById('latest-saved-runs');
      if (!target) return;
      const rows = filteredRunCases()
        .filter(caseHasRuntimeEvidence)
        .filter(item => latestRunMillis(item) > 0)
        .slice(0, 8);
      if (!rows.length) {{
        target.innerHTML = '<div class="subtle">No saved runs or review events match the current filters.</div>';
        return;
      }}
      target.innerHTML = `<div class="followup-list">${{rows.map(item => {{
        const runAt = item.latest_run_at || item.scoring_completed_at || item.orchestrator_judge_created_at || item.latest_slack_created_at || item.updated_at || item.human_created_at || '';
        const runContext = [
          item.latest_run_source ? `source ${{item.latest_run_source}}` : '',
          item.latest_run_id || item.latest_slack_run_id ? `run ${{item.latest_run_id || item.latest_slack_run_id}}` : '',
          item.latest_slack_thread_ts ? `thread ${{item.latest_slack_thread_ts}}` : '',
          runAt ? formatDateTime(runAt) || runAt : '',
        ].filter(Boolean).join(' · ');
        const evidenceContext = [
          Number(item.slack_run_count || 0) > 0 ? `${{item.slack_run_count}} Slack run${{Number(item.slack_run_count || 0) === 1 ? '' : 's'}}` : '',
          item.latest_slack_thread_fetch_status ? `thread ${{item.latest_slack_thread_fetch_status}}` : '',
          Number(item.latest_slack_source_count || 0) || Number(item.latest_slack_visible_source_count || 0) ? `${{item.latest_slack_visible_source_count || 0}}/${{item.latest_slack_source_count || 0}} visible sources` : '',
        ].filter(Boolean).join(' · ');
        const orchestratorComment = item.orchestrator_judge_run_comment || item.orchestrator_judge_notes || '';
        const statusLabel = item.scoring_status_label || (item.latest_run_source ? 'run saved' : 'saved activity');
        const actions = [
          item.dashboard_url ? `<a class="pill" href="${{escapeHtml(item.dashboard_url)}}">Scoring</a>` : '',
          item.review_url ? `<a class="pill" href="${{escapeHtml(item.review_url)}}">Review form</a>` : '',
          item.case_id ? `<button class="ledger-copy" type="button" data-copy-latest-case="${{escapeHtml(item.case_id)}}" title="Prompt, response, scores, evidence, and next review follow-up.">Copy eval review packet</button>` : '',
        ].filter(Boolean).join('');
        return `<article class="followup-item">
          <div>
            <div class="followup-title">
              ${{pill(statusLabel, item.scoring_status === 'complete' ? 'pass' : 'warn')}}
              <strong>${{escapeHtml(item.display_case_id || item.case_id || 'case tbd')}}</strong>
              <span class="subtle">${{escapeHtml(item.agent || '')}}</span>
            </div>
            ${{runContext ? `<div class="followup-detail mono">${{escapeHtml(runContext)}}</div>` : ''}}
            ${{runScoreStrip(item)}}
            ${{evidenceContext ? `<div class="followup-detail">${{escapeHtml(evidenceContext)}}</div>` : ''}}
            ${{orchestratorComment ? `<div class="followup-detail"><strong>Orchestrator Review:</strong> ${{escapeHtml(orchestratorComment)}}</div>` : ''}}
          </div>
          <div class="followup-actions">${{actions}}</div>
        </article>`;
      }}).join('')}}</div>`;
      for (const button of target.querySelectorAll('[data-copy-latest-case]')) {{
        button.addEventListener('click', () => copyCaseBundle(button));
      }}
    }}
    function renderFollowUpQueue() {{
      const target = document.getElementById('eval-follow-up-queue');
      if (!target) return;
      const rows = data.follow_up_queue || [];
      if (!rows.length) {{
        target.innerHTML = '<div class="subtle">No current follow-up gaps. Saved runs with responses, machine checks, human reviews, evidence, and analysis inclusion are ready for comparison.</div>';
        return;
      }}
      target.innerHTML = `<div class="followup-list">${{rows.map(row => {{
        const status = String(row.status || 'missing');
        const labels = Array.isArray(row.label_display) && row.label_display.length
          ? row.label_display
          : Array.isArray(row.labels) ? row.labels : [];
        const statusClass = status === 'attention' ? 'warn' : 'fail';
        const followUpSummary = String(row.follow_up_summary || '');
        const caseLabel = String(row.case_label || row.case_id || 'case tbd');
        const displayCaseId = String(row.display_case_id || '');
        const promptNumber = String(row.display_prompt_number || '');
        const caseMeta = displayCaseId && displayCaseId !== caseLabel
          ? `prompt ${{promptNumber || displayCaseId}}`
          : '';
        const runContext = [
          row.latest_run_source ? `source ${{row.latest_run_source}}` : '',
          row.latest_run_id ? `run ${{row.latest_run_id}}` : '',
          row.latest_slack_thread_ts ? `thread ${{row.latest_slack_thread_ts}}` : '',
          row.latest_run_at ? formatDateTime(row.latest_run_at) || row.latest_run_at : '',
        ].filter(Boolean).join(' · ');
        const orchestratorComment = row.orchestrator_judge_run_comment || row.orchestrator_judge_notes || '';
        const reviewNotes = [
          orchestratorComment ? `Orchestrator Review: ${{orchestratorComment}}` : '',
          row.human_notes ? `Human review: ${{row.human_notes}}` : '',
        ].filter(Boolean).join(' ');
        const promptExcerpt = String(row.prompt_excerpt || '');
        const responseExcerpt = String(row.response_excerpt || '');
        const actions = [
          row.dashboard_url ? `<a class="pill" href="${{escapeHtml(row.dashboard_url)}}">Scoring</a>` : '',
          row.review_url ? `<a class="pill" href="${{escapeHtml(row.review_url)}}">Review form</a>` : '',
          row.case_id ? `<button class="ledger-copy" type="button" data-copy-followup-case="${{escapeHtml(row.case_id)}}" title="Prompt, response, scores, evidence, and next review follow-up.">Copy eval review packet</button>` : '',
        ].filter(Boolean).join('');
        return `<article class="followup-item">
          <div>
            <div class="followup-title">
              ${{pill(status, statusClass)}}
              <strong>${{escapeHtml(caseLabel)}}</strong>
              ${{caseMeta ? `<span class="subtle">${{escapeHtml(caseMeta)}}</span>` : ''}}
              <span class="subtle">${{escapeHtml(row.agent || '')}}</span>
              ${{labels.slice(0, 4).map(label => pill(label, statusClass)).join(' ')}}
              ${{labels.length > 4 ? `<span class="subtle">+${{labels.length - 4}} more gaps</span>` : ''}}
            </div>
            ${{followUpSummary ? `<div class="followup-detail"><strong>${{escapeHtml(followUpSummary)}}</strong></div>` : ''}}
            <div class="followup-detail">Next: ${{escapeHtml(row.next_follow_up || row.detail || '')}}</div>
            ${{runContext ? `<div class="followup-detail mono">${{escapeHtml(runContext)}}</div>` : ''}}
            ${{runScoreStrip(row)}}
            ${{reviewNotes ? `<div class="followup-detail">${{escapeHtml(reviewNotes)}}</div>` : ''}}
            ${{promptExcerpt ? `<div class="followup-detail"><strong>Prompt:</strong> ${{escapeHtml(promptExcerpt)}}${{Number(row.prompt_chars || 0) > promptExcerpt.length ? '...' : ''}}</div>` : ''}}
            ${{responseExcerpt ? `<div class="followup-detail"><strong>Response:</strong> ${{escapeHtml(responseExcerpt)}}${{Number(row.response_chars || 0) > responseExcerpt.length ? '...' : ''}}</div>` : ''}}
          </div>
          <div class="followup-actions">${{actions}}</div>
        </article>`;
      }}).join('')}}</div>`;
      for (const button of target.querySelectorAll('[data-copy-followup-case]')) {{
        button.addEventListener('click', () => copyCaseBundle(button));
      }}
    }}
    function renderRunLedger() {{
      const target = document.getElementById('eval-run-ledger');
      if (!target) return;
      const rows = data.run_ledger || [];
      if (!rows.length) {{
        target.innerHTML = '<div class="subtle">No run ledger events are recorded yet.</div>';
        return;
      }}
      const visibleRows = ledgerExpanded ? rows : rows.slice(0, ledgerDefaultLimit);
      const hiddenCount = Math.max(0, rows.length - visibleRows.length);
      const toolbar = rows.length > ledgerDefaultLimit
        ? `<div class="ledger-toolbar">
            <div class="subtle">${{ledgerExpanded ? `Showing all ${{rows.length}} ledger events.` : `Showing newest ${{visibleRows.length}} of ${{rows.length}} ledger events.`}}</div>
            <button class="ledger-toggle" type="button" data-ledger-toggle>${{ledgerExpanded ? 'Collapse ledger' : `Show ${{hiddenCount}} more`}}</button>
          </div>`
        : `<div class="ledger-toolbar"><div class="subtle">Showing ${{rows.length}} ledger event${{rows.length === 1 ? '' : 's'}}.</div></div>`;
      target.innerHTML = `${{toolbar}}<div class="ledger-list">${{visibleRows.map((row) => {{
        const index = rows.indexOf(row);
        const status = String(row.status || '');
        const source = String(row.source || '');
        const statusClass = status === 'fail' || status.includes('fail') ? 'fail' : source === 'trace' ? 'warn' : 'pass';
        const score = ledgerScoreText(row);
        const titleParts = [
          row.case_id || 'case tbd',
          row.agent ? `agent ${{row.agent}}` : '',
        ].filter(Boolean).join(' · ');
        const metaParts = [
          row.run_id ? `run ${{row.run_id}}` : 'run tbd',
          score,
        ].filter(Boolean);
        const traceSummary = row.trace_agentic_summary || {{}};
        const traceParts = [
          traceSummary.signal ? `signal ${{traceSummary.signal}}` : '',
          traceSummary.route ? `route ${{traceSummary.route}}` : '',
          traceSummary.tools && !String(traceSummary.tools).endsWith('metadata pending') ? `tools ${{traceSummary.tools}}` : '',
          traceSummary.retrieval && !String(traceSummary.retrieval).endsWith('metadata pending') ? `retrieval ${{traceSummary.retrieval}}` : '',
          traceSummary.model && !String(traceSummary.model).endsWith('metadata pending') ? `model ${{traceSummary.model}}` : '',
        ].filter(Boolean);
        return `<article class="ledger-event">
          <div class="ledger-time">${{escapeHtml(formatDateTime(row.timestamp) || row.timestamp || 'time tbd')}}</div>
          <div class="ledger-main">
            <div class="ledger-title-row">
              <div class="ledger-title">
                ${{pill(ledgerSourceLabel(row), source === 'trace' ? 'warn' : 'pass')}}
                ${{pill(status || 'saved', statusClass)}}
                <strong>${{escapeHtml(titleParts)}}</strong>
              </div>
              <button class="ledger-copy" type="button" data-ledger-index="${{index}}" data-copy-ledger-event="${{escapeHtml(row.run_id || row.case_id || row.timestamp || index)}}" aria-label="Copy this ledger entry for Codex review">Copy</button>
            </div>
            <div class="ledger-meta">${{metaParts.map(part => `<span>${{escapeHtml(part)}}</span>`).join('<span aria-hidden="true"> · </span>')}}</div>
            <div class="ledger-detail">${{escapeHtml(row.details || '')}}</div>
            ${{traceParts.length ? `<div class="ledger-detail"><strong>Trace:</strong> ${{traceParts.map(part => escapeHtml(part)).join(' · ')}}</div>` : ''}}
          </div>
        </article>`;
      }}).join('')}}</div>`;
      const toggle = target.querySelector('[data-ledger-toggle]');
      if (toggle) {{
        toggle.addEventListener('click', () => {{
          ledgerExpanded = !ledgerExpanded;
          renderRunLedger();
        }});
      }}
      for (const button of target.querySelectorAll('[data-copy-ledger-event]')) {{
        button.addEventListener('click', () => copyLedgerReview(button));
      }}
    }}
    function formatPercent(value) {{
      if (value === null || value === undefined) return '-';
      const number = Number(value);
      return Number.isFinite(number) ? `${{number.toFixed(1)}}%` : '-';
    }}
    function signedNumber(value, suffix = '', digits = 1) {{
      if (value === null || value === undefined) return '-';
      const number = Number(value);
      if (!Number.isFinite(number)) return '-';
      const sign = number > 0 ? '+' : '';
      return `${{sign}}${{number.toFixed(digits)}}${{suffix}}`;
    }}
    function analysisCaseLabel(caseId) {{
      return cases.find(item => item.case_id === caseId)?.display_case_id || caseId;
    }}
    function scoreLine(scores, limit = 4) {{
      const entries = Object.entries(scores || {{}})
        .filter(([, value]) => value !== null && value !== undefined && value !== '')
        .sort((a, b) => a[0].localeCompare(b[0]));
      if (!entries.length) return '';
      const visible = entries.slice(0, limit).map(([label, value]) => `${{label}} ${{value}}`);
      const hidden = entries.length - visible.length;
      return hidden ? `${{visible.join(' · ')}} · +${{hidden}} more` : visible.join(' · ');
    }}
    function dbMetric(value, fallback = 'tbd') {{
      if (value === null || value === undefined || value === '') {{
        return `<span class="db-empty">${{escapeHtml(fallback)}}</span>`;
      }}
      return escapeHtml(value);
    }}
    function scoreValue(value, machine = false) {{
      const formatted = scoreOutOfFive(value, machine);
      return formatted === '-' ? `<span class="db-empty">tbd</span>` : escapeHtml(formatted);
    }}
    function scoreValueText(value, machine = false) {{
      const formatted = scoreOutOfFive(value, machine);
      return formatted === '-' ? 'tbd' : formatted;
    }}
    function runScoreChannel(label, value, cls = 'warn', machine = false) {{
      return `<div class="run-score-channel ${{cls}}">
        <div class="label">${{escapeHtml(label)}}</div>
        <div class="value">${{escapeHtml(scoreValueText(value, machine))}}</div>
      </div>`;
    }}
    function runScoreStrip(item) {{
      const hasMachine = item.promptfoo_success !== null && item.promptfoo_success !== undefined;
      const hasOrchestrator = item.orchestrator_judge_average !== null && item.orchestrator_judge_average !== undefined;
      const hasHuman = item.human_average !== null && item.human_average !== undefined;
      const machineClass = item.promptfoo_success === false ? 'fail' : hasMachine ? 'pass' : 'warn';
      const orchestratorClass = item.orchestrator_judge_safety === 'fail' ? 'fail' : hasOrchestrator ? 'pass' : 'warn';
      const humanClass = item.human_safety === 'fail' ? 'fail' : hasHuman ? 'pass' : 'warn';
      return `<div class="run-score-strip">
        ${{runScoreChannel('Machine', hasMachine ? item.promptfoo_score : null, machineClass, true)}}
        ${{runScoreChannel('Orchestrator', hasOrchestrator ? item.orchestrator_judge_average : null, orchestratorClass)}}
        ${{runScoreChannel('Human', hasHuman ? item.human_average : null, humanClass)}}
      </div>`;
    }}
    function analysisCard(label, value, detail = '') {{
      return `<div class="analysis-card">
        <div class="label">${{labelWithInfo(label)}}</div>
        <div class="value">${{escapeHtml(value)}}</div>
        ${{detail ? `<div class="subtle">${{escapeHtml(detail)}}</div>` : ''}}
      </div>`;
    }}
    function miniBar(label, value, max, detail = '') {{
      const safeMax = Math.max(1, Number(max || 0));
      const safeValue = Math.max(0, Number(value || 0));
      const pct = Math.max(0, Math.min(100, safeValue / safeMax * 100));
      return `<div class="mini-bar-row">
        <div class="truncate" title="${{escapeHtml(label)}}">${{escapeHtml(label)}}</div>
        <div class="mini-bar-track" aria-hidden="true"><div class="mini-bar-fill" style="width:${{pct.toFixed(1)}}%"></div></div>
        <div class="mono" title="${{escapeHtml(detail)}}">${{escapeHtml(String(value ?? 0))}}</div>
      </div>`;
    }}
    function scaffoldBarRows(rows) {{
      if (!rows.length) return '<div class="subtle">Waiting for data.</div>';
      const max = Math.max(1, ...rows.map(row => Number(row.value || 0)));
      return `<div class="scaffold-bars">${{rows.map(row => {{
        const pct = Math.max(0, Math.min(100, Number(row.value || 0) / max * 100));
        return `<div class="scaffold-bar-row">
          <div class="truncate" title="${{escapeHtml(row.label)}}">${{escapeHtml(row.label)}}</div>
          <div class="mini-bar-track" aria-hidden="true"><div class="mini-bar-fill" style="width:${{pct.toFixed(1)}}%"></div></div>
          <div class="mono">${{escapeHtml(row.display ?? String(row.value ?? 0))}}</div>
        </div>`;
      }}).join('')}}</div>`;
    }}
    function scaffoldLine(points, label = 'trend') {{
      const rows = points.filter(value => value !== null && value !== undefined && Number.isFinite(Number(value)));
      if (!rows.length) return '<div class="subtle">Waiting for trend data.</div>';
      const width = 220;
      const height = 72;
      const pad = 8;
      const min = Math.min(...rows.map(Number), 0);
      const max = Math.max(...rows.map(Number), 1);
      const denom = Math.max(1, rows.length - 1);
      const span = Math.max(0.001, max - min);
      const line = rows.map((value, index) => {{
        const x = pad + (index / denom) * (width - pad * 2);
        const y = pad + (1 - (Number(value) - min) / span) * (height - pad * 2);
        return `${{x.toFixed(1)}},${{y.toFixed(1)}}`;
      }}).join(' ');
      return `<svg class="scaffold-line" viewBox="0 0 ${{width}} ${{height}}" role="img" aria-label="${{escapeHtml(label)}}">
        <line x1="${{pad}}" y1="${{height - pad}}" x2="${{width - pad}}" y2="${{height - pad}}" stroke="#edf0f2" />
        <polyline points="${{line}}" fill="none" stroke="#2f6f41" stroke-width="3" stroke-linejoin="round" stroke-linecap="round" />
        ${{rows.map((value, index) => {{
          const x = pad + (index / denom) * (width - pad * 2);
          const y = pad + (1 - (Number(value) - min) / span) * (height - pad * 2);
          return `<circle cx="${{x.toFixed(1)}}" cy="${{y.toFixed(1)}}" r="3" fill="#1b1f23"><title>${{escapeHtml(String(value))}}</title></circle>`;
        }}).join('')}}
      </svg>`;
    }}
    function scaffoldHeatmap(labels, values) {{
      const max = Math.max(1, ...values.map(value => Number(value || 0)));
      const cells = labels.slice(0, 18).map((label, index) => {{
        const value = Number(values[index] || 0);
        const opacity = Math.max(0.12, Math.min(0.9, value / max));
        return `<span class="scaffold-heat-cell" title="${{escapeHtml(label)}}: ${{escapeHtml(String(value))}}" style="background:rgba(47,111,65,${{opacity.toFixed(2)}})"></span>`;
      }}).join('');
      return cells ? `<div class="scaffold-heatmap" aria-label="Dimension coverage heatmap">${{cells}}</div>` : '<div class="subtle">Waiting for dimension data.</div>';
    }}
    function scaffoldCard(title, detail, body = '') {{
      return `<div class="analysis-scaffold-card">
        <h3>${{escapeHtml(title)}}</h3>
        <div class="subtle">${{escapeHtml(detail)}}</div>
        <div class="scaffold-visual">${{body || '<div class="subtle">Waiting for data.</div>'}}</div>
      </div>`;
    }}
    function workflowStep(label, value, detail = '', numerator = null, denominator = null) {{
      const hasBar = numerator !== null && denominator !== null && Number(denominator) > 0;
      const percent = hasBar
        ? Math.max(0, Math.min(100, Number(numerator || 0) / Number(denominator || 1) * 100))
        : 0;
      return `<div class="workflow-step">
        <div class="label">${{labelWithInfo(label)}}</div>
        <div class="value">${{escapeHtml(value)}}</div>
        ${{hasBar ? `<div class="workflow-stage-bar" aria-hidden="true"><div class="workflow-stage-fill" style="width:${{percent.toFixed(1)}}%"></div></div>` : ''}}
        ${{detail ? `<div class="subtle">${{escapeHtml(detail)}}</div>` : ''}}
      </div>`;
    }}
    function workflowGuardrailRow(step, currentMode, futureTrigger, costGuardrail, liveNow, dashboardState) {{
      return `<tr>
        <td>${{escapeHtml(step)}}</td>
        <td>${{escapeHtml(currentMode)}}</td>
        <td>${{escapeHtml(futureTrigger)}}</td>
        <td>${{escapeHtml(costGuardrail)}}</td>
        <td>${{liveNow ? pill('Yes', 'warn') : '<span class="db-empty">No</span>'}}</td>
        <td>${{escapeHtml(dashboardState)}}</td>
      </tr>`;
    }}
    function workflowInteractionContract(fallbackCounts = {{}}) {{
      const readiness = data.workflow_readiness || {{}};
      const interactions = Array.isArray(readiness.interactions) && readiness.interactions.length
        ? readiness.interactions
        : [
            {{
              interaction: 'Prompt copy',
              current_behavior: 'Local prompt library row only',
              future_live_trigger: 'Human pastes root message into #evals',
              cost_guardrail: 'No API call from dashboard copy',
              stored_evidence: `${{fallbackCounts.total || 0}} local cases`,
            }},
            {{
              interaction: 'Agent thread reply',
              current_behavior: 'Previewed from saved response/result text',
              future_live_trigger: 'Slack app mention in the selected thread',
              cost_guardrail: 'One agent run per accepted Slack root prompt',
              stored_evidence: `${{fallbackCounts.slackRuns || 0}} saved Slack runs`,
            }},
            {{
              interaction: 'Promptfoo machine summary',
              current_behavior: 'Shows latest imported assertion status and score by case_id',
              future_live_trigger: 'Resolved Slack case has an imported Promptfoo result',
              cost_guardrail: 'Use imported Promptfoo result by case_id; do not rerun Promptfoo from Slack thread',
              stored_evidence: `${{fallbackCounts.analysisReady || 0}} included machine rows`,
            }},
            {{
              interaction: 'Retrieval/source evidence',
              current_behavior: 'Shows tags and recorded evidence already in the eval database',
              future_live_trigger: 'Accepted agent run needs source retrieval',
              cost_guardrail: 'Use dry-run fixtures/cache first; cap live retrieval to accepted root run',
              stored_evidence: `${{fallbackCounts.retrievalTagged || 0}} retrieval/source-tagged cases`,
            }},
            {{
              interaction: 'Slack review form open',
              current_behavior: 'Review controls stay disabled until a response exists',
              future_live_trigger: 'Human opens the Slack-linked score form from the eval thread',
              cost_guardrail: 'No model call; form uses saved case, run id, thread, and response context',
              stored_evidence: `${{fallbackCounts.recordedResponses || 0}} recorded responses`,
            }},
            {{
              interaction: 'Submit Evaluation',
              current_behavior: 'Manual submission and Orchestrator Review write score fields and notes to the local eval database as separate review kinds',
              future_live_trigger: 'Human presses Submit Evaluation after scoring in Slack, or enabled Orchestrator Review scores the saved run',
              cost_guardrail: 'Manual submit uses no model call and no Slack post; Orchestrator Review runs only when explicitly enabled, then refreshes Database, Runs & Scoring, and Analysis from saved rows',
              stored_evidence: `${{fallbackCounts.reviewScorecards || fallbackCounts.humanReviews || 0}} saved scorecards`,
            }},
            {{
              interaction: 'Analysis inclusion',
              current_behavior: 'Included unless marked duplicate/problem run',
              future_live_trigger: 'Operator toggles analysis inclusion',
              cost_guardrail: 'No rerun; recalculates from database rows',
              stored_evidence: `${{fallbackCounts.analysisReady || 0}} included machine rows`,
            }},
          ];
      return `<details class="workflow-disclosure workflow-contract" aria-label="Slack eval interaction readiness and cost guardrails">
        <summary>
          <span>Interaction guardrails</span>
          <span class="subtle">Slack/API touchpoints and cost controls</span>
        </summary>
        <div class="db-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Interaction</th>
                <th>Current dashboard behavior</th>
                <th>Future live trigger</th>
                <th>Cost guardrail</th>
                <th>Live now</th>
                <th>Stored evidence</th>
              </tr>
            </thead>
            <tbody>
              ${{interactions.map(item => workflowGuardrailRow(
                item.interaction || '',
                item.current_behavior || '',
                item.future_live_trigger || '',
                item.cost_guardrail || '',
                item.live_api_call_now === true,
                item.stored_evidence || ''
              )).join('')}}
            </tbody>
          </table>
        </div>
      </details>`;
    }}
    function workflowStarterRunPlan() {{
      const plan = data.workflow_readiness?.starter_run_plan || {{}};
      const liveSteps = Array.isArray(plan.expected_live_calls_when_enabled)
        ? plan.expected_live_calls_when_enabled
        : [];
      const caseLabel = plan.display_case_id || plan.case_id || 'Select a committed prompt';
      const agent = plan.agent || 'agent pending';
      const promptNumber = plan.prompt_number ? ` · prompt ${{plan.prompt_number}}` : '';
      const pasteText = plan.paste_text || 'Choose a Prompt Library case before starting a Slack eval run.';
      const dashboardUrl = plan.dashboard_url || '';
      const reviewUrl = plan.human_review_url || '';
      const threadSequence = Array.isArray(plan.thread_sequence) ? plan.thread_sequence : [];
      return `<details class="workflow-disclosure workflow-run-plan" aria-label="Suggested Slack test run">
        <summary>
          <span>Suggested Slack test run</span>
          <span class="subtle">${{escapeHtml(caseLabel)}} · local-only</span>
        </summary>
        <div class="run-plan-grid">
          <div class="run-plan-box">
            <div class="label">${{labelWithInfo('Start with')}}</div>
            <div class="mono">${{escapeHtml(caseLabel)}}</div>
            <div class="subtle">${{escapeHtml(agent)}}${{escapeHtml(promptNumber)}}</div>
            ${{dashboardUrl ? `<div class="subtle">Case scoring: <span class="mono">${{escapeHtml(dashboardUrl)}}</span></div>` : ''}}
            ${{reviewUrl ? `<div class="subtle">Human review form: <span class="mono">${{escapeHtml(reviewUrl)}}</span></div>` : ''}}
            <ol class="run-plan-list">
              <li>Paste the prompt into <span class="mono">${{escapeHtml(plan.channel || '#evals')}}</span> as the root message.</li>
              <li>Keep all follow-ups in the same Slack thread.</li>
              <li>Expect an in-thread eval footer with Promptfoo machine-check status, review form link, and scoring link.</li>
              <li>Open the Slack-linked scoring form from the eval footer; no extra agent response is needed.</li>
              <li>Use <strong>Score with Orchestrator Review</strong> when enabled to fill the same form-shaped scorecard for the saved #evals output.</li>
              <li>Press <strong>Submit Evaluation</strong> in Slack after manual scoring; saved human and Orchestrator Review scorecards update Database, Runs & Scoring, and Analysis as separate review kinds.</li>
            </ol>
          </div>
          <div class="run-plan-box">
            <div class="label">${{labelWithInfo('Paste into Slack')}}</div>
            <div class="run-plan-prompt">${{escapeHtml(pasteText)}}</div>
            <div class="label" style="margin-top:10px;">${{labelWithInfo('Expected live calls when enabled')}}</div>
            <ul class="run-plan-list">
              ${{liveSteps.map(step => `<li><strong>${{escapeHtml(step.step || '')}}</strong>: ${{escapeHtml(step.expected_call || '')}} <span class="subtle">${{escapeHtml(step.cost_guardrail || '')}}</span></li>`).join('')}}
            </ul>
          </div>
        </div>
        ${{threadSequence.length ? `<div class="run-plan-box" style="margin-top:10px;">
          <div class="label">${{labelWithInfo('Expected in-thread communication flow')}}</div>
          <ol class="run-plan-list">
            ${{threadSequence.map(item => `<li><strong>${{escapeHtml(item.speaker || '')}}</strong>: ${{escapeHtml(item.message || '')}} <span class="subtle">${{escapeHtml(item.stored_as || '')}}</span></li>`).join('')}}
          </ol>
        </div>` : ''}}
      </details>`;
    }}
    function renderWorkflowVisualization() {{
      const target = document.getElementById('workflow-visualization');
      if (!target) return;
      const total = cases.length;
      const promptLibrary = Number(data.summary?.seed_case_total || total || 0);
      const slackRuns = cases.filter(item => Number(item.slack_run_count || 0) > 0).length;
      const recordedResponses = cases.filter(item => hasRecordedResponse(item)).length;
      const humanReviews = cases.filter(item => item.human_average !== null && item.human_average !== undefined).length;
      const orchestratorReviews = cases.filter(item => item.orchestrator_judge_average !== null && item.orchestrator_judge_average !== undefined).length;
      const reviewScorecards = cases.filter(item => (
        item.human_average !== null && item.human_average !== undefined
      ) || (
        item.orchestrator_judge_average !== null && item.orchestrator_judge_average !== undefined
      )).length;
      const analysisReady = cases.filter(item => item.promptfoo_eval_id && !item.analysis_excluded).length;
      const retrievalTagged = cases.filter(item => (item.dimensions || []).some(dimension => [
        'retrieval',
        'retrieval_boundary',
        'retrieval_policy',
        'search',
        'search_quality',
        'source_quality',
        'source_relevance',
        'source_sufficiency',
        'source_visibility',
      ].includes(String(dimension || '')))).length;
      target.innerHTML = `
        <div class="workflow-steps">
          ${{workflowStep('Prompt library', `${{promptLibrary}} cases`, 'copy into #evals', promptLibrary, Math.max(total, promptLibrary))}}
          ${{workflowStep('Slack runs', `${{slackRuns}}/${{total}}`, 'saved thread run', slackRuns, total)}}
          ${{workflowStep('Responses', `${{recordedResponses}}/${{total}}`, 'unlock review', recordedResponses, total)}}
          ${{workflowStep('Scorecards', `${{reviewScorecards}}/${{total}}`, `${{humanReviews}} manual · ${{orchestratorReviews}} Orchestrator`, reviewScorecards, total)}}
          ${{workflowStep('Analysis', `${{analysisReady}}/${{total}}`, 'included rows', analysisReady, total)}}
          ${{workflowStep('Source checks', `${{retrievalTagged}}/${{promptLibrary}}`, 'retrieval/evidence tags', retrievalTagged, promptLibrary)}}
        </div>
        <details class="workflow-disclosure workflow-thread" aria-label="Slack eval thread preview">
          <summary>
            <span>Thread preview</span>
            <span class="subtle">what should appear in #evals after one agent run</span>
          </summary>
          <div class="workflow-chat">
            <div class="chat-row">
              <div class="chat-speaker">Human</div>
              <div class="chat-bubble">Paste a committed Prompt Library case into <span class="mono">#evals</span>.</div>
            </div>
            <div class="chat-row">
              <div class="chat-speaker">KNI</div>
              <div class="chat-bubble system">Reply once in-thread with the agent result, case id, run id, and scoring link.</div>
            </div>
            <div class="chat-row">
              <div class="chat-speaker">Promptfoo</div>
              <div class="chat-bubble system">Append imported machine-check status plus scoring and review links; no Promptfoo rerun.</div>
            </div>
            <div class="chat-row">
              <div class="chat-speaker">Review</div>
              <div class="chat-bubble system">Score from the linked form, or run Orchestrator Review scoring when enabled, using the saved prompt, response, and machine check.</div>
            </div>
            <div class="chat-row">
              <div class="chat-speaker">Scoring</div>
              <div class="chat-bubble system">Update runs, database, and analysis from saved rows without extra model calls.</div>
            </div>
          </div>
        </details>
        ${{workflowStarterRunPlan()}}
        ${{workflowInteractionContract({{ total, slackRuns, recordedResponses, humanReviews, orchestratorReviews, reviewScorecards, analysisReady, retrievalTagged }})}}`;
    }}
    function renderOverviewLatestRun() {{
      const target = document.getElementById('overview-latest-run');
      if (!target) return;
      const item = latestFirstCases(cases).find(row => latestRunMillis(row) > 0);
      if (!item) {{
        target.innerHTML = '<div class="subtle">No saved runs are available yet.</div>';
        return;
      }}
      const latestAt = item.latest_run_at || item.scoring_completed_at || item.orchestrator_judge_created_at || item.latest_slack_created_at || item.updated_at || item.human_created_at || '';
      const runContext = [
        item.latest_run_source ? `source ${{item.latest_run_source}}` : '',
        item.latest_run_id || item.latest_slack_run_id || item.promptfoo_eval_id ? `run ${{item.latest_run_id || item.latest_slack_run_id || item.promptfoo_eval_id}}` : '',
        item.latest_slack_thread_ts ? `thread ${{item.latest_slack_thread_ts}}` : '',
        latestAt ? formatDateTime(latestAt) || latestAt : '',
      ].filter(Boolean).join(' · ');
      const evidence = [
        Number(item.slack_run_count || 0) > 0 ? `${{item.slack_run_count}} Slack run${{Number(item.slack_run_count || 0) === 1 ? '' : 's'}}` : 'Slack not run',
        Number(item.latest_slack_source_count || 0) || Number(item.latest_slack_visible_source_count || 0) ? `${{item.latest_slack_visible_source_count || 0}}/${{item.latest_slack_source_count || 0}} visible sources` : '',
        item.scoring_status_label || '',
      ].filter(Boolean).join(' · ');
      const actions = [
        item.dashboard_url ? `<a class="pill" href="${{escapeHtml(item.dashboard_url)}}">Scoring</a>` : '',
        item.review_url ? `<a class="pill" href="${{escapeHtml(item.review_url)}}">Review form</a>` : '',
      ].filter(Boolean).join('');
      target.innerHTML = `<article class="followup-item">
        <div>
          <div class="followup-title">
            ${{pill(item.scoring_status_label || 'latest run', item.scoring_status === 'complete' ? 'pass' : 'warn')}}
            <strong>${{escapeHtml(item.display_case_id || item.case_id || 'case tbd')}}</strong>
            <span class="subtle">${{escapeHtml(item.agent || '')}}</span>
          </div>
          ${{runContext ? `<div class="followup-detail mono">${{escapeHtml(runContext)}}</div>` : ''}}
          ${{runScoreStrip(item)}}
          <div class="followup-detail">${{escapeHtml(evidence)}}</div>
        </div>
        <div class="followup-actions">${{actions}}</div>
      </article>`;
    }}
    function renderDataQualityGates() {{
      const target = document.getElementById('data-quality-gates');
      if (!target) return;
      const quality = data.data_quality || {{}};
      const checks = quality.checks || [];
      const reviewQueue = quality.human_review_queue || [];
      const blocked = quality.review_blocked_cases || [];
      const reviewQueueCount = Number(quality.human_review_queue_count ?? reviewQueue.length);
      const blockedCount = Number(quality.review_blocked_count ?? blocked.length);
      const readinessLabel = quality.ready_for_paid_api_runs
        ? pill('ready for paid runs', 'pass')
        : pill(`${{quality.pending_count || 0}} pending`, Number(quality.blocker_count || 0) > 0 ? 'fail' : 'warn');
      target.innerHTML = `
        <div class="analysis-grid">
          <div class="analysis-card">
            <div class="label">${{labelWithInfo('Paid-run readiness')}}</div>
            <div class="value">${{readinessLabel}}</div>
            <div class="subtle">${{quality.blocker_count || 0}} blockers; ${{quality.warning_count || 0}} other pending checks</div>
          </div>
          <div class="analysis-card">
            <div class="label">${{labelWithInfo('Human review queue')}}</div>
            <div class="value">${{reviewQueueCount}}</div>
            <div class="subtle">responses saved but unreviewed; open Runs & Scoring for case packets</div>
          </div>
          <div class="analysis-card">
            <div class="label">${{labelWithInfo('Review blocked')}}</div>
            <div class="value">${{blockedCount}}</div>
            <div class="subtle">cases missing recorded response; open Runs & Scoring for next actions</div>
          </div>
        </div>
        <div class="db-table-wrap" style="margin-top:10px">
          <table class="analysis-table">
            <thead><tr><th>Gate</th><th>Status</th><th>Coverage</th><th>Needed before paid runs</th></tr></thead>
            <tbody>${{checks.map(check => {{
              const statusClass = check.status === 'complete' ? 'pass' : (check.severity === 'blocker' ? 'fail' : 'warn');
              const coverage = check.total ? `${{check.passed}}/${{check.total}}` : `${{check.passed}}`;
              const actionPrefix = check.status === 'complete' ? 'Complete' : 'Needed';
              return `<tr>
                <td>${{escapeHtml(check.label || '')}}</td>
                <td>${{pill(check.status || 'unknown', statusClass)}}</td>
                <td class="mono">${{escapeHtml(coverage)}}</td>
                <td>${{escapeHtml(`${{actionPrefix}}: ${{check.detail || ''}}`)}}</td>
              </tr>`;
            }}).join('')}}</tbody>
          </table>
        </div>`;
    }}
    function renderAnalysisChartScaffolds() {{
      const target = document.getElementById('analysis-chart-scaffolds');
      if (!target) return;
      const total = cases.length;
      const machineRows = cases.filter(item => item.promptfoo_eval_id).length;
      const slackRuns = cases.filter(item => Number(item.slack_run_count || 0) > 0).length;
      const recordedResponses = cases.filter(item => hasRecordedResponse(item)).length;
      const humanReviews = cases.filter(item => item.human_average !== null && item.human_average !== undefined).length;
      const orchestratorReviews = cases.filter(item => item.orchestrator_judge_average !== null && item.orchestrator_judge_average !== undefined).length;
      const reviewScorecards = cases.filter(item => (
        item.human_average !== null && item.human_average !== undefined
      ) || (
        item.orchestrator_judge_average !== null && item.orchestrator_judge_average !== undefined
      )).length;
      const analysisIncluded = cases.filter(item => item.promptfoo_eval_id && !item.analysis_excluded).length;
      const excludedRuns = cases.filter(item => item.analysis_excluded).length;
      const retrievalTagged = cases.filter(item => (item.dimensions || []).some(dimension => [
        'retrieval',
        'retrieval_boundary',
        'retrieval_policy',
        'search',
        'search_quality',
        'source_quality',
        'source_relevance',
        'source_sufficiency',
        'source_visibility',
      ].includes(String(dimension || '')))).length;
      const promptAverageRows = data.analysis?.prompt_score_averages || [];
      const scoreBuckets = [
        {{ label: '0-2.9', value: 0 }},
        {{ label: '3.0-3.9', value: 0 }},
        {{ label: '4.0-4.4', value: 0 }},
        {{ label: '4.5-5.0', value: 0 }},
      ];
      for (const row of promptAverageRows) {{
        const score = Number(row.average_score || 0) * 5;
        if (score < 3) scoreBuckets[0].value += 1;
        else if (score < 4) scoreBuckets[1].value += 1;
        else if (score < 4.5) scoreBuckets[2].value += 1;
        else scoreBuckets[3].value += 1;
      }}
      const agentRows = (data.analysis?.agent_stability || []).map(row => ({{
        label: row.agent || 'unknown',
        value: row.average_score === null || row.average_score === undefined ? 0 : Number(row.average_score) * 5,
        display: scoreOutOfFive(row.average_score, true),
      }}));
      const dimensionEntries = Object.entries(data.summary?.dimensions || {{}})
        .sort((a, b) => Number(b[1]) - Number(a[1]) || a[0].localeCompare(b[0]));
      const runTrendValues = (data.analysis?.run_trends || []).map(row => row.pass_rate);
      const humanTrendValues = (data.analysis?.human_review_trends || []).map(row => (
        row.average_score === null || row.average_score === undefined ? null : Number(row.average_score) / 5 * 100
      ));
      const judgeTrendValues = (data.analysis?.orchestrator_judge_review_trends || []).map(row => (
        row.average_score === null || row.average_score === undefined ? null : Number(row.average_score) / 5 * 100
      ));
      target.innerHTML = [
        scaffoldCard(
          'Review completion funnel',
          'Stage bars for committed cases, saved Slack responses, and completed review scorecards.',
          scaffoldBarRows([
            {{ label: 'Committed cases', value: total }},
            {{ label: 'Slack responses', value: recordedResponses }},
            {{ label: 'Review scorecards', value: reviewScorecards }},
          ])
        ),
        scaffoldCard(
          'Machine vs review coverage',
          'Grouped bars for Promptfoo machine checks beside saved human and Orchestrator Review scorecards.',
          scaffoldBarRows([
            {{ label: 'Machine checks', value: machineRows }},
            {{ label: 'Human reviews', value: humanReviews }},
            {{ label: 'Orchestrator Reviews', value: orchestratorReviews }},
            {{ label: 'Included rows', value: analysisIncluded }},
          ])
        ),
        scaffoldCard(
          'Cost-safe run volume',
          'One AI agent call per root eval; Promptfoo lookup, form save, and analysis are database-backed.',
          scaffoldBarRows([
            {{ label: 'Root Slack runs', value: slackRuns }},
            {{ label: 'Retrieval-tagged', value: retrievalTagged }},
            {{ label: 'Excluded runs', value: excludedRuns }},
          ])
        ),
        scaffoldCard(
          'Prompt score distribution',
          'Histogram scaffold for prompt-level average machine scores by included run day.',
          scaffoldBarRows(scoreBuckets.map(row => ({{ ...row, display: String(row.value) }})))
        ),
        scaffoldCard(
          'Agent score comparison',
          'Horizontal ranking scaffold for average machine score by agent, with review lines added when scorecards exist.',
          scaffoldBarRows(agentRows)
        ),
        scaffoldCard(
          'Dimension readiness heatmap',
          'Matrix scaffold for quality dimensions such as retrieval, source quality, synthesis, safety, and format.',
          scaffoldHeatmap(
            dimensionEntries.map(([label]) => label),
            dimensionEntries.map(([, value]) => Number(value || 0)),
          )
        ),
        scaffoldCard(
          'Machine trend line',
          'Line scaffold for Promptfoo pass rate over imported runs.',
          scaffoldLine(runTrendValues, 'Promptfoo pass-rate trend')
        ),
        scaffoldCard(
          'Review trend line',
          'Line scaffold for daily manual human and Orchestrator Review averages once reviews exist.',
          humanTrendValues.some(value => value !== null && value !== undefined) || judgeTrendValues.some(value => value !== null && value !== undefined)
            ? scaffoldLine(humanTrendValues.some(value => value !== null && value !== undefined) ? humanTrendValues : judgeTrendValues, 'Review trend')
            : '<div class="subtle">Waiting for saved review forms.</div><div class="scaffold-note">The line appears after at least one human or Orchestrator Review scorecard; the daily trend is best after 4+ reviews per day.</div>'
        ),
      ].join('');
    }}
    function renderAnalysisSummary() {{
      const analysis = data.analysis || {{}};
      const trends = analysis.run_trends || [];
      const fix = analysis.fix_signal || {{}};
      const avgDelta = fix.average_score_delta === null || fix.average_score_delta === undefined
        ? '-'
        : signedNumber(Number(fix.average_score_delta || 0) * 5, ' /5', 2);
      document.getElementById('analysis-summary').innerHTML = [
        analysisCard('Pass-rate change', signedNumber(fix.pass_rate_delta, ' pp'), fix.previous_eval_id ? `vs ${{fix.previous_eval_id}}` : 'needs two runs'),
        analysisCard('Assertion avg change', avgDelta, 'normalized to 0-5'),
        analysisCard('Case movement', `${{fix.improved_cases || 0}} improved · ${{fix.regressed_cases || 0}} regressed`, `${{fix.stable_cases || 0}} stable cases`),
      ].join('');
    }}
    function linePoints(rows, valueAccessor, width, height, pad) {{
      const values = rows.map((row, index) => ({{ row, index, value: valueAccessor(row) }}))
        .filter(item => item.value !== null && item.value !== undefined && Number.isFinite(Number(item.value)));
      if (!values.length) return '';
      const denom = Math.max(1, rows.length - 1);
      return values.map(item => {{
        const x = pad.left + (item.index / denom) * (width - pad.left - pad.right);
        const y = pad.top + (1 - Number(item.value) / 100) * (height - pad.top - pad.bottom);
        return `${{x.toFixed(1)}},${{y.toFixed(1)}}`;
      }}).join(' ');
    }}
    function indexedLinePoints(rows, indexAccessor, valueAccessor, width, height, pad) {{
      const values = rows.map(row => ({{ row, index: Number(indexAccessor(row)), value: valueAccessor(row) }}))
        .filter(item => Number.isFinite(item.index) && item.value !== null && item.value !== undefined && Number.isFinite(Number(item.value)));
      if (!values.length) return '';
      const maxIndex = Math.max(1, ...values.map(item => item.index));
      return values.map(item => {{
        const x = pad.left + (item.index / maxIndex) * (width - pad.left - pad.right);
        const y = pad.top + (1 - Number(item.value) / 100) * (height - pad.top - pad.bottom);
        return `${{x.toFixed(1)}},${{y.toFixed(1)}}`;
      }}).join(' ');
    }}
    function renderAnalysisRunChart() {{
      const trend = data.analysis?.agent_score_trends || {{ agents: [], series: {{ all: [] }} }};
      const selected = analysisAgentTrendFilter?.value || 'all';
      const rows = (trend.series?.[selected] || trend.series?.all || []).slice();
      const target = document.getElementById('analysis-run-chart');
      const table = document.getElementById('analysis-agent-trend-table');
      if (!rows.length) {{
        target.innerHTML = '<div class="subtle">No included Promptfoo or human review score history yet.</div>';
        table.innerHTML = '';
        return;
      }}
      const width = 760;
      const height = 190;
      const pad = {{ left: 44, right: 14, top: 18, bottom: 32 }};
      const machinePoints = linePoints(
        rows,
        row => row.machine_average === null || row.machine_average === undefined ? null : Number(row.machine_average) / 5 * 100,
        width,
        height,
        pad,
      );
      const humanPoints = linePoints(
        rows,
        row => row.human_average === null || row.human_average === undefined ? null : Number(row.human_average) / 5 * 100,
        width,
        height,
        pad,
      );
      const judgePoints = linePoints(
        rows,
        row => row.orchestrator_judge_average === null || row.orchestrator_judge_average === undefined ? null : Number(row.orchestrator_judge_average) / 5 * 100,
        width,
        height,
        pad,
      );
      const machineCircles = rows.map((row, index) => {{
        if (row.machine_average === null || row.machine_average === undefined) return '';
        const x = pad.left + (index / Math.max(1, rows.length - 1)) * (width - pad.left - pad.right);
        const y = pad.top + (1 - Number(row.machine_average) / 5) * (height - pad.top - pad.bottom);
        return `<circle cx="${{x.toFixed(1)}}" cy="${{y.toFixed(1)}}" r="4" fill="#2f6f41"><title>${{escapeHtml(row.date)}} · machine ${{scoreValue(row.machine_average)}} · ${{row.machine_count || 0}} prompt${{Number(row.machine_count || 0) === 1 ? '' : 's'}}</title></circle>`;
      }}).join('');
      const humanCircles = rows.map((row, index) => {{
        if (row.human_average === null || row.human_average === undefined) return '';
        const x = pad.left + (index / Math.max(1, rows.length - 1)) * (width - pad.left - pad.right);
        const y = pad.top + (1 - Number(row.human_average) / 5) * (height - pad.top - pad.bottom);
        return `<circle cx="${{x.toFixed(1)}}" cy="${{y.toFixed(1)}}" r="4" fill="#8a6f2a"><title>${{escapeHtml(row.date)}} · human ${{scoreValue(row.human_average)}} · ${{row.human_count || 0}} review${{Number(row.human_count || 0) === 1 ? '' : 's'}}</title></circle>`;
      }}).join('');
      const judgeCircles = rows.map((row, index) => {{
        if (row.orchestrator_judge_average === null || row.orchestrator_judge_average === undefined) return '';
        const x = pad.left + (index / Math.max(1, rows.length - 1)) * (width - pad.left - pad.right);
        const y = pad.top + (1 - Number(row.orchestrator_judge_average) / 5) * (height - pad.top - pad.bottom);
        return `<circle cx="${{x.toFixed(1)}}" cy="${{y.toFixed(1)}}" r="4" fill="#315f9b"><title>${{escapeHtml(row.date)}} · Orchestrator Review ${{scoreValue(row.orchestrator_judge_average)}} · ${{row.orchestrator_judge_count || 0}} review form${{Number(row.orchestrator_judge_count || 0) === 1 ? '' : 's'}}</title></circle>`;
      }}).join('');
      target.innerHTML = `<svg viewBox="0 0 ${{width}} ${{height}}" role="img" aria-label="Average machine, human, and Orchestrator Review score across time">
        <line x1="${{pad.left}}" y1="${{pad.top}}" x2="${{pad.left}}" y2="${{height - pad.bottom}}" stroke="#d9dee3" />
        <line x1="${{pad.left}}" y1="${{height - pad.bottom}}" x2="${{width - pad.right}}" y2="${{height - pad.bottom}}" stroke="#d9dee3" />
        ${{[0, 2.5, 5].map(value => {{
          const y = pad.top + (1 - value / 5) * (height - pad.top - pad.bottom);
          return `<line x1="${{pad.left}}" y1="${{y}}" x2="${{width - pad.right}}" y2="${{y}}" stroke="#edf0f2" /><text x="4" y="${{y + 4}}" fill="#687076" font-size="11">${{value}}/5</text>`;
        }}).join('')}}
        <polyline points="${{machinePoints}}" fill="none" stroke="#2f6f41" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" />
        ${{humanPoints ? `<polyline points="${{humanPoints}}" fill="none" stroke="#8a6f2a" stroke-width="2.5" stroke-dasharray="2 4" stroke-linejoin="round" stroke-linecap="round" />` : ''}}
        ${{judgePoints ? `<polyline points="${{judgePoints}}" fill="none" stroke="#315f9b" stroke-width="2.5" stroke-dasharray="6 3" stroke-linejoin="round" stroke-linecap="round" />` : ''}}
        ${{machineCircles}}
        ${{humanCircles}}
        ${{judgeCircles}}
        <text x="${{pad.left}}" y="${{height - 8}}" fill="#687076" font-size="11">${{escapeHtml(rows[0].date || '')}}</text>
        <text x="${{width - pad.right}}" y="${{height - 8}}" fill="#687076" font-size="11" text-anchor="end">${{escapeHtml(rows[rows.length - 1].date || '')}}</text>
        <text x="${{width - 210}}" y="18" fill="#2f6f41" font-size="12">machine avg</text>
        ${{humanPoints ? `<text x="${{width - 105}}" y="18" fill="#8a6f2a" font-size="12">human avg</text>` : ''}}
        ${{judgePoints ? `<text x="${{width - 15}}" y="18" fill="#315f9b" font-size="12" text-anchor="end">Orchestrator Review</text>` : ''}}
      </svg>
      <div class="score-legend" aria-label="Average score color legend">
        <span class="score-legend-item"><span class="score-swatch" style="background:#2f6f41"></span>Machine average</span>
        <span class="score-legend-item"><span class="score-swatch" style="background:#8a6f2a"></span>Human average</span>
        <span class="score-legend-item"><span class="score-swatch" style="background:#315f9b"></span>Orchestrator Review average</span>
      </div>
      ${{humanPoints || judgePoints ? '' : '<div class="subtle">No saved human or Orchestrator Review average trend yet for this selection.</div>'}}`;
      table.innerHTML = `<table class="analysis-table">
        <thead><tr><th>Date</th><th>Machine avg</th><th>Machine prompts</th><th>Human avg</th><th>Human reviews</th><th>Orchestrator Review avg</th><th>Orchestrator Review forms</th></tr></thead>
        <tbody>${{rows.map(row => `<tr>
          <td class="mono">${{escapeHtml(row.date || '')}}</td>
          <td class="mono">${{scoreValue(row.machine_average)}}</td>
          <td class="mono">${{row.machine_count || '<span class="db-empty">tbd</span>'}}</td>
          <td class="mono">${{scoreValue(row.human_average)}}</td>
          <td class="mono">${{row.human_count || '<span class="db-empty">tbd</span>'}}</td>
          <td class="mono">${{scoreValue(row.orchestrator_judge_average)}}</td>
          <td class="mono">${{row.orchestrator_judge_count || '<span class="db-empty">tbd</span>'}}</td>
        </tr>`).join('')}}</tbody>
      </table>`;
    }}
    function renderAnalysisCaseSelector() {{
      const rows = data.analysis?.case_trends || [];
      analysisCaseFilter.innerHTML = '<option value="">Select prompt</option>' + rows.map(row => (
        `<option value="${{escapeHtml(row.case_id)}}">${{escapeHtml(analysisCaseLabel(row.case_id))}}</option>`
      )).join('');
      if (!analysisCaseFilter.value && rows.length) {{
        analysisCaseFilter.value = rows[0].case_id;
      }}
    }}
    function renderAnalysisCaseTrend() {{
      const rows = data.analysis?.case_trends || [];
      const selected = analysisCaseFilter.value || rows[0]?.case_id || '';
      const item = rows.find(row => row.case_id === selected);
      const chart = document.getElementById('analysis-case-trend-chart');
      const table = document.getElementById('analysis-case-trend-table');
      if (!item || !(item.runs || []).length) {{
        chart.innerHTML = '<div class="subtle">No Promptfoo case history is available yet.</div>';
        table.innerHTML = '';
        return;
      }}
      const runs = item.runs || [];
      const humanTrend = (data.analysis?.human_case_trends || []).find(row => row.case_id === selected);
      const humanDays = humanTrend?.days || [];
      const judgeTrend = (data.analysis?.orchestrator_judge_case_trends || []).find(row => row.case_id === selected);
      const judgeDays = judgeTrend?.days || [];
      const dateKey = value => String(value || '').slice(0, 10);
      const allDays = [...new Set([
        ...runs.map(row => dateKey(row.created_at)),
        ...humanDays.map(row => dateKey(row.date)),
        ...judgeDays.map(row => dateKey(row.date)),
      ].filter(Boolean))].sort();
      const dayIndex = new Map(allDays.map((day, index) => [day, index]));
      const width = 760;
      const height = 170;
      const pad = {{ left: 44, right: 14, top: 18, bottom: 32 }};
      const machinePoints = indexedLinePoints(
        runs,
        row => dayIndex.get(dateKey(row.created_at)) ?? 0,
        row => Number(row.score || 0) * 100,
        width,
        height,
        pad,
      );
      const humanPoints = indexedLinePoints(
        humanDays,
        row => dayIndex.get(dateKey(row.date)) ?? 0,
        row => row.average_score === null || row.average_score === undefined ? null : Number(row.average_score) / 5 * 100,
        width,
        height,
        pad,
      );
      const judgePoints = indexedLinePoints(
        judgeDays,
        row => dayIndex.get(dateKey(row.date)) ?? 0,
        row => row.average_score === null || row.average_score === undefined ? null : Number(row.average_score) / 5 * 100,
        width,
        height,
        pad,
      );
      const xForDay = day => {{
        const maxIndex = Math.max(1, allDays.length - 1);
        return pad.left + ((dayIndex.get(day) ?? 0) / maxIndex) * (width - pad.left - pad.right);
      }};
      const circles = runs.map((row, index) => {{
        const x = xForDay(dateKey(row.created_at));
        const y = pad.top + (1 - Number(row.score || 0)) * (height - pad.top - pad.bottom);
        const fill = row.success ? '#2f6f41' : '#b42318';
        return `<circle cx="${{x.toFixed(1)}}" cy="${{y.toFixed(1)}}" r="4" fill="${{fill}}"><title>${{escapeHtml(row.eval_id)}} · ${{scoreOutOfFive(row.score, true)}} · ${{row.success ? 'pass' : 'fail'}} · ${{row.run_count || 1}} run${{Number(row.run_count || 1) === 1 ? '' : 's'}}</title></circle>`;
      }}).join('');
      const humanCircles = humanDays.map(row => {{
        const x = xForDay(dateKey(row.date));
        const y = pad.top + (1 - Number(row.average_score || 0) / 5) * (height - pad.top - pad.bottom);
        return `<circle cx="${{x.toFixed(1)}}" cy="${{y.toFixed(1)}}" r="4" fill="#8a6f2a"><title>human avg ${{scoreOutOfFive(row.average_score)}} · ${{row.count || 1}} review${{Number(row.count || 1) === 1 ? '' : 's'}}</title></circle>`;
      }}).join('');
      const judgeCircles = judgeDays.map(row => {{
        const x = xForDay(dateKey(row.date));
        const y = pad.top + (1 - Number(row.average_score || 0) / 5) * (height - pad.top - pad.bottom);
        return `<circle cx="${{x.toFixed(1)}}" cy="${{y.toFixed(1)}}" r="4" fill="#315f9b"><title>Orchestrator Review avg ${{scoreOutOfFive(row.average_score)}} · ${{row.count || 1}} form${{Number(row.count || 1) === 1 ? '' : 's'}}</title></circle>`;
      }}).join('');
      chart.innerHTML = `<svg viewBox="0 0 ${{width}} ${{height}}" role="img" aria-label="Single prompt score across time">
        <line x1="${{pad.left}}" y1="${{pad.top}}" x2="${{pad.left}}" y2="${{height - pad.bottom}}" stroke="#d9dee3" />
        <line x1="${{pad.left}}" y1="${{height - pad.bottom}}" x2="${{width - pad.right}}" y2="${{height - pad.bottom}}" stroke="#d9dee3" />
        ${{[0, 2.5, 5].map(value => {{
          const y = pad.top + (1 - value / 5) * (height - pad.top - pad.bottom);
          return `<line x1="${{pad.left}}" y1="${{y}}" x2="${{width - pad.right}}" y2="${{y}}" stroke="#edf0f2" /><text x="4" y="${{y + 4}}" fill="#687076" font-size="11">${{value}}/5</text>`;
        }}).join('')}}
        <polyline points="${{machinePoints}}" fill="none" stroke="#2f6f41" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" />
        ${{humanPoints ? `<polyline points="${{humanPoints}}" fill="none" stroke="#8a6f2a" stroke-width="2.5" stroke-dasharray="4 4" stroke-linejoin="round" stroke-linecap="round" />` : ''}}
        ${{judgePoints ? `<polyline points="${{judgePoints}}" fill="none" stroke="#315f9b" stroke-width="2.5" stroke-dasharray="6 3" stroke-linejoin="round" stroke-linecap="round" />` : ''}}
        ${{circles}}
        ${{humanCircles}}
        ${{judgeCircles}}
        <text x="${{pad.left}}" y="${{height - 8}}" fill="#687076" font-size="11">${{escapeHtml(allDays[0] || runs[0].created_at || '')}}</text>
        <text x="${{width - pad.right}}" y="${{height - 8}}" fill="#687076" font-size="11" text-anchor="end">${{escapeHtml(allDays[allDays.length - 1] || runs[runs.length - 1].created_at || '')}}</text>
        <text x="${{width - 210}}" y="18" fill="#2f6f41" font-size="12">machine avg</text>
        ${{humanPoints ? `<text x="${{width - 105}}" y="18" fill="#8a6f2a" font-size="12">human avg</text>` : ''}}
        ${{judgePoints ? `<text x="${{width - 15}}" y="18" fill="#315f9b" font-size="12" text-anchor="end">Orchestrator Review</text>` : ''}}
      </svg>
      <div class="score-legend" aria-label="Single prompt score color legend">
        <span class="score-legend-item"><span class="score-swatch" style="background:#2f6f41"></span>Machine score</span>
        <span class="score-legend-item"><span class="score-swatch" style="background:#8a6f2a"></span>Human average</span>
        <span class="score-legend-item"><span class="score-swatch" style="background:#315f9b"></span>Orchestrator Review average</span>
      </div>
      ${{humanPoints || judgePoints ? '' : '<div class="subtle">No saved human or Orchestrator Review trend for this prompt yet.</div>'}}`;
      const machineByDay = new Map(runs.map(row => [dateKey(row.created_at), row]));
      const humanByDay = new Map(humanDays.map(row => [dateKey(row.date), row]));
      const judgeByDay = new Map(judgeDays.map(row => [dateKey(row.date), row]));
      table.innerHTML = `<table class="analysis-table">
        <thead><tr><th>Run date</th><th>Eval run instance</th><th>Machine runs</th><th>Machine check</th><th>Machine avg</th><th>Review target</th><th>Human reviews</th><th>Human avg</th><th>Orchestrator Reviews</th><th>Orchestrator avg</th></tr></thead>
        <tbody>${{allDays.map(day => {{
          const machine = machineByDay.get(day) || {{}};
          const human = humanByDay.get(day) || {{}};
          const judge = judgeByDay.get(day) || {{}};
          const reviewTargets = Array.isArray(human.reviews) ? human.reviews : [];
          const judgeTargets = Array.isArray(judge.reviews) ? judge.reviews : [];
          const reviewTarget = reviewTargets[0] || judgeTargets[0] || {{}};
          const reviewTargetId = reviewTarget.run_id || reviewTarget.slack_thread_ts || reviewTarget.review_id || '';
          const reviewTargetLabel = reviewTarget.target_type
            ? `${{reviewTarget.target_type}}${{reviewTargetId ? ` · ${{reviewTargetId}}` : ''}}`
            : '';
          return `<tr>
          <td>${{escapeHtml(day)}}</td>
          <td class="mono">${{dbMetric(machine.eval_id)}}</td>
          <td class="mono">${{machine.run_count || '<span class="db-empty">tbd</span>'}}</td>
          <td>${{machine.eval_id ? pill(machine.success ? 'pass' : 'fail', machine.success ? 'pass' : 'fail') : '<span class="db-empty">tbd</span>'}}</td>
          <td class="mono">${{scoreValue(machine.score, true)}}</td>
          <td class="mono">${{reviewTargetLabel ? escapeHtml(reviewTargetLabel) : '<span class="db-empty">tbd</span>'}}</td>
          <td class="mono">${{human.count || '<span class="db-empty">tbd</span>'}}</td>
          <td class="mono">${{scoreValue(human.average_score)}}</td>
          <td class="mono">${{judge.count || '<span class="db-empty">tbd</span>'}}</td>
          <td class="mono">${{scoreValue(judge.average_score)}}</td>
        </tr>`;
        }}).join('')}}</tbody>
      </table>`;
    }}
    function renderAnalysisPromptAverages() {{
      const rows = data.analysis?.prompt_score_averages || [];
      const target = document.getElementById('analysis-prompt-average-chart');
      if (!rows.length) {{
        target.innerHTML = '<div class="subtle">No included Promptfoo prompt scores are available yet.</div>';
        return;
      }}
      const grouped = new Map();
      for (const row of rows) {{
        const agent = row.agent || 'unknown';
        if (!grouped.has(agent)) grouped.set(agent, []);
        grouped.get(agent).push(row);
      }}
      target.innerHTML = `<div class="prompt-average-groups">${{Array.from(grouped.entries()).map(([agent, items]) => {{
        const sorted = items.slice().sort((a, b) => Number(a.average_score || 0) - Number(b.average_score || 0) || String(a.case_id).localeCompare(String(b.case_id)));
        return `<section class="prompt-average-group">
          <h3>${{escapeHtml(agent)}}</h3>
          ${{sorted.map(row => {{
            const score = Number(row.average_score || 0);
            return `<div class="prompt-average-row">
              <div class="truncate" title="${{escapeHtml(row.case_id)}}">${{escapeHtml(analysisCaseLabel(row.case_id))}}</div>
              <div class="bar-track"><div class="bar ${{score < 1 ? 'gap' : ''}}" style="width:${{Math.max(0, Math.min(100, score * 100))}}%"></div></div>
              <div class="mono">${{scoreOutOfFive(row.average_score, true)}}</div>
              <div class="subtle" style="grid-column: 1 / -1">${{row.run_count || 0}} day${{Number(row.run_count || 0) === 1 ? '' : 's'}} averaged · latest ${{escapeHtml(row.latest_created_at || '')}}</div>
            </div>`;
          }}).join('')}}
        </section>`;
      }}).join('')}}</div>`;
    }}
    function renderAnalysisAgentStability() {{
      const rows = data.analysis?.agent_stability || [];
      const target = document.getElementById('analysis-agent-stability');
      if (!rows.length) {{
        target.innerHTML = '<div class="subtle">No agent-level Promptfoo history yet.</div>';
        return;
      }}
      target.innerHTML = `<table class="analysis-table">
        <thead><tr><th>Agent</th><th>Pass rate</th><th>Avg / 5</th><th>Score range</th><th>Flaky cases</th></tr></thead>
        <tbody>${{rows.map(row => `<tr>
          <td class="mono">${{escapeHtml(row.agent)}}</td>
          <td>
            <div class="bar-row" style="grid-template-columns:minmax(58px, 72px) 1fr; margin:0">
              <span class="mono">${{formatPercent(row.pass_rate)}}</span>
              <span class="bar-track"><span class="bar ${{Number(row.pass_rate || 0) < 100 ? 'gap' : ''}}" style="width:${{Math.max(0, Math.min(100, Number(row.pass_rate || 0)))}}%"></span></span>
            </div>
          </td>
          <td class="mono">${{scoreOutOfFive(row.average_score, true)}}</td>
          <td class="mono">${{(Number(row.score_range || 0) * 5).toFixed(2)}}/5</td>
          <td class="mono">${{row.flaky_cases || 0}}</td>
        </tr>`).join('')}}</tbody>
      </table>`;
    }}
    function renderAnalysisDimensionFailures() {{
      const rows = data.analysis?.dimension_failures || [];
      const target = document.getElementById('analysis-dimension-failures');
      if (!rows.length) {{
        target.innerHTML = '<div class="subtle">No failed Promptfoo assertions are recorded in the imported runs.</div>';
        return;
      }}
      const max = Math.max(1, ...rows.map(row => Number(row.failures || 0)));
      target.innerHTML = rows.map(row => `
        <div class="bar-row">
          <div class="truncate" title="${{escapeHtml((row.agents || []).join(', '))}}">${{escapeHtml(row.dimension)}}</div>
          <div class="bar-track"><div class="bar gap" style="width:${{Math.round(Number(row.failures || 0) / max * 100)}}%"></div></div>
          <div class="mono">${{row.failures || 0}}</div>
        </div>
      `).join('');
    }}
    function renderAnalysisCaseChanges() {{
      const rows = data.analysis?.case_changes || [];
      const target = document.getElementById('analysis-case-changes');
      if (!rows.length) {{
        target.innerHTML = '<div class="subtle">No repeated case history yet. Run Promptfoo more than once on the same cases to see improvements and regressions.</div>';
        return;
      }}
      target.innerHTML = `<table class="analysis-table">
        <thead><tr><th>Latest check</th><th>Case</th><th>Agent</th><th>Machine checks</th><th>Previous</th><th>Latest</th><th>Delta</th><th>Dimensions</th></tr></thead>
        <tbody>${{rows.slice(0, 30).map(row => {{
          const delta = row.score_delta === null || row.score_delta === undefined ? '-' : signedNumber(Number(row.score_delta || 0) * 5, '', 2);
          const checks = Number(row.runs || 0);
          const latestChecks = Number(row.latest_run_count || 0);
          const checkLabel = checks
            ? `${{checks}} total${{latestChecks > 1 ? ` · ${{latestChecks}} latest-day` : ''}}`
            : 'tbd';
          return `<tr>
            <td>${{escapeHtml(formatDateTime(row.latest_created_at) || row.latest_created_at || '')}}</td>
            <td class="mono" title="${{escapeHtml(row.case_id)}}">${{escapeHtml(analysisCaseLabel(row.case_id))}}</td>
            <td class="mono">${{escapeHtml(row.agent || '')}}</td>
            <td class="mono">${{escapeHtml(checkLabel)}}</td>
            <td class="mono">${{row.previous_score === null || row.previous_score === undefined ? '-' : scoreOutOfFive(row.previous_score, true)}}</td>
            <td class="mono">${{scoreOutOfFive(row.latest_score, true)}}</td>
            <td class="mono">${{escapeHtml(delta)}}</td>
            <td>${{(row.dimensions || []).map(d => pill(d)).join('')}}</td>
          </tr>`;
        }}).join('')}}</tbody>
      </table>`;
    }}
        function renderAnalysis() {{
          renderAnalysisSummary();
          renderAnalysisChartScaffolds();
          renderAnalysisRunChart();
      renderAnalysisCaseSelector();
      renderAnalysisCaseTrend();
      renderAnalysisPromptAverages();
      renderAnalysisAgentStability();
          renderAnalysisDimensionFailures();
          renderAnalysisCaseChanges();
        }}
        function traceDiagnosticColor(row, index = 0) {{
          const palette = ['#4b5563', '#6b7280', '#7d8590', '#8c959f', '#a1a9b3', '#b3bac3', '#c4cad1'];
          return palette[index % palette.length];
        }}
        function traceDiagnosticBadge(text) {{
          return `<span class="pill trace-diagnostic-pill">${{escapeHtml(text)}}</span>`;
        }}
        function renderTraceDiagnosticTrendChart(rows) {{
          const target = document.getElementById('trace-diagnostic-trend-chart');
          if (!target) return;
          if (!rows.length) {{
            target.innerHTML = '<div class="subtle">No diagnostic trend chart yet. It appears after run summaries emit chartable diagnostic categories.</div>';
            return;
          }}
          const dates = [...new Set(rows.map(row => String(row.date || '').slice(0, 10)).filter(Boolean))].sort();
          const totalsByCategory = new Map();
          const metaByCategory = new Map();
          const countFor = new Map();
          for (const row of rows) {{
            const date = String(row.date || '').slice(0, 10);
            const key = String(row.key || row.label || 'unknown');
            if (!date || !key) continue;
            const count = Number(row.count || 0);
            totalsByCategory.set(key, (totalsByCategory.get(key) || 0) + count);
            if (!metaByCategory.has(key)) metaByCategory.set(key, row);
            countFor.set(`${{date}}\\u0000${{key}}`, count);
          }}
          const categories = [...totalsByCategory.entries()]
            .sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0) || String(a[0]).localeCompare(String(b[0])))
            .map(([key]) => key)
            .slice(0, 7);
          if (!dates.length || !categories.length) {{
            target.innerHTML = '<div class="subtle">Diagnostic trend rows are present but do not include chartable dates/categories.</div>';
            return;
          }}
          const dailyTotals = dates.map(date => categories.reduce((sum, key) => sum + Number(countFor.get(`${{date}}\\u0000${{key}}`) || 0), 0));
          const maxTotal = Math.max(1, ...dailyTotals);
          const width = 760;
          const height = 205;
          const pad = {{ left: 48, right: 20, top: 20, bottom: 48 }};
          const innerWidth = width - pad.left - pad.right;
          const innerHeight = height - pad.top - pad.bottom;
          const slot = innerWidth / Math.max(1, dates.length);
          const barWidth = Math.max(18, Math.min(54, slot * 0.62));
          const yFor = value => pad.top + (1 - Number(value || 0) / maxTotal) * innerHeight;
          const xFor = index => pad.left + index * slot + (slot - barWidth) / 2;
          const gridValues = [...new Set([0, Math.ceil(maxTotal / 2), maxTotal])].sort((a, b) => a - b);
          const dateStride = Math.max(1, Math.ceil(dates.length / 6));
          const bars = dates.map((date, dateIndex) => {{
            let yCursor = pad.top + innerHeight;
            return categories.map((key, categoryIndex) => {{
              const row = metaByCategory.get(key) || {{ key }};
              const count = Number(countFor.get(`${{date}}\\u0000${{key}}`) || 0);
              if (!count) return '';
              const barHeight = Math.max(1, count / maxTotal * innerHeight);
              yCursor -= barHeight;
              const color = traceDiagnosticColor(row, categoryIndex);
              const label = row.label || key;
              return `<rect x="${{xFor(dateIndex).toFixed(1)}}" y="${{yCursor.toFixed(1)}}" width="${{barWidth.toFixed(1)}}" height="${{barHeight.toFixed(1)}}" rx="2" fill="${{color}}"><title>${{escapeHtml(date)}} · ${{escapeHtml(label)}} · ${{count}}</title></rect>`;
            }}).join('');
          }}).join('');
          const axis = gridValues.map(value => {{
            const y = yFor(value);
            return `<line x1="${{pad.left}}" y1="${{y.toFixed(1)}}" x2="${{width - pad.right}}" y2="${{y.toFixed(1)}}" stroke="${{value ? '#edf0f2' : '#d9dee3'}}" /><text x="5" y="${{(y + 4).toFixed(1)}}" fill="#687076" font-size="11">${{value}}</text>`;
          }}).join('');
          const xLabels = dates.map((date, index) => {{
            if (index % dateStride !== 0 && index !== dates.length - 1) return '';
            return `<text x="${{(xFor(index) + barWidth / 2).toFixed(1)}}" y="${{height - 14}}" fill="#687076" font-size="11" text-anchor="middle">${{escapeHtml(date.slice(5) || date)}}</text>`;
          }}).join('');
          const legend = categories.map((key, index) => {{
            const row = metaByCategory.get(key) || {{ key }};
            const color = traceDiagnosticColor(row, index);
            return `<span class="trace-chart-key"><span class="trace-chart-swatch" style="background:${{color}}"></span><span>${{escapeHtml(row.label || key)}} (${{Number(totalsByCategory.get(key) || 0)}})</span></span>`;
          }}).join('');
          target.innerHTML = `<svg viewBox="0 0 ${{width}} ${{height}}" role="img" aria-label="Trace diagnostic category trend by day">
            ${{axis}}
            <line x1="${{pad.left}}" y1="${{pad.top}}" x2="${{pad.left}}" y2="${{height - pad.bottom}}" stroke="#d9dee3" />
            <line x1="${{pad.left}}" y1="${{height - pad.bottom}}" x2="${{width - pad.right}}" y2="${{height - pad.bottom}}" stroke="#d9dee3" />
            ${{bars}}
            ${{xLabels}}
            <text x="${{pad.left}}" y="13" fill="#687076" font-size="11">events by category</text>
          </svg><div class="trace-chart-legend">${{legend}}</div>`;
        }}
        function renderTraceAnalytics(trace) {{
          const target = document.getElementById('trace-analytics');
          if (!target) return;
          const diagnosticRows = trace.diagnostic_category_counts || [];
          const diagnosticHits = diagnosticRows.reduce((sum, row) => sum + Number(row.count || 0), 0);
          const topDiagnostic = diagnosticRows[0] || {{}};
          const runSummaryCount = Number(trace.run_summary_count || 0);
          const joinedRunSummaryCount = Number(trace.joined_run_summary_count || 0);
          const unjoinedRunSummaryCount = Math.max(0, runSummaryCount - joinedRunSummaryCount);
          const joinPct = runSummaryCount ? `${{Math.round(joinedRunSummaryCount / runSummaryCount * 100)}}%` : 'n/a';
          const privacyValue = trace.effective_sensitive_capture ? 'raw capture enabled' : 'raw capture disabled';
          const privacyHint = trace.effective_sensitive_capture
            ? 'Review before enabling broad eval runs.'
            : 'Prompts, responses, tool I/O, Slack text, secrets, and PHI stay out.';
          const cards = [
            ['Join health', runSummaryCount ? `${{joinedRunSummaryCount}}/${{runSummaryCount}} joined` : 'no summaries', `${{joinPct}} join rate; ${{unjoinedRunSummaryCount}} unjoined`],
            ['Run source split', `${{Number(trace.manual_run_summary_count || 0)}} manual / ${{Number(trace.sdk_run_summary_count || 0)}} SDK`, 'no-API backfill now; API summaries later'],
            ['Diagnostic signals', `${{diagnosticRows.length}} categories`, `${{diagnosticHits}} category hits across summaries`],
            ['Top cleanup signal', topDiagnostic.label || topDiagnostic.key || 'none yet', topDiagnostic.count ? `${{Number(topDiagnostic.count)}} events` : 'no chartable diagnostic category yet'],
            ['Privacy guardrail', privacyValue, privacyHint],
          ];
          target.innerHTML = cards.map(([label, value, hint]) => `
            <div class="analysis-card">
              <div class="label">${{labelWithInfo(label)}}</div>
              <div class="value">${{escapeHtml(value)}}</div>
              <div class="subtle">${{escapeHtml(hint)}}</div>
            </div>
          `).join('');
        }}
        function renderTraceDatabaseFreshness() {{
          const target = document.getElementById('trace-db-freshness');
          if (!target) return;
          const tables = data.database_tables || {{}};
          const rows = [
            ['Slack runs table', 'slack_eval_runs', 'Slack run saves'],
            ['Trace events table', 'eval_trace_events', 'Trace saves'],
            ['Promptfoo cases table', 'promptfoo_case_results', 'Machine rows'],
            ['Promptfoo runs table', 'promptfoo_eval_runs', 'Machine imports'],
            ['Human reviews table', 'human_eval_reviews', 'Scorecards'],
          ];
          target.innerHTML = rows.map(([label, key, hintPrefix]) => {{
            const row = tables[key] || {{}};
            const exists = row.exists === true;
            const count = Number(row.row_count || 0);
            const latest = row.latest_at ? (formatDateTime(row.latest_at) || row.latest_at) : 'none yet';
            const latestRow = row.latest || {{}};
            const latestId = latestRow.case_id || latestRow.eval_id || latestRow.run_id || latestRow.trace_id || latestRow.span_id || '';
            const hint = exists
              ? `${{hintPrefix}} · latest ${{latest}}${{latestId ? ` · ${{latestId}}` : ''}}`
              : 'table has not been created in this local DB yet';
            return `<div class="analysis-card">
              <div class="label">${{labelWithInfo(label)}}</div>
              <div class="value">${{exists ? count : 'missing'}}</div>
              <div class="subtle">${{escapeHtml(hint)}}</div>
            </div>`;
          }}).join('');
        }}
        function traceStepLabel(event) {{
          const type = String(event.event_type || '').toLowerCase();
          const name = String(event.name || '');
          const labels = {{
            manual_run_summary: 'Manual run summary',
            slack_run_saved: 'Slack run saved',
            orchestrator_judge_review_saved: 'Orchestrator Review',
            human_review_saved: 'Human scorecard',
            sdk_run_summary: 'SDK run summary',
            span_end: 'Span completed',
            trace_end: 'Trace completed',
          }};
          if (labels[type]) return labels[type];
          if (type.includes('review')) return 'Review saved';
          if (type.includes('slack')) return 'Slack event';
          if (type.includes('summary')) return 'Run summary';
          return name || event.event_type || 'Trace event';
        }}
        function traceEventRoute(event) {{
          const metadata = event.metadata || {{}};
          const correlation = metadata.correlation || {{}};
          return String(
            event.agent ||
            metadata.agent ||
            correlation.agent ||
            event.route ||
            metadata.route ||
            correlation.route ||
            ''
          );
        }}
        function traceEventSignal(event) {{
          const agentic = event.agentic_summary || {{}};
          if (agentic.signal) return String(agentic.signal);
          const metadata = event.metadata || {{}};
          const tooling = metadata.tooling || {{}};
          const retrieval = metadata.retrieval || metadata.retrieval_provider_summary || {{}};
          const model = metadata.model || {{}};
          const orchestrator = metadata.orchestrator || {{}};
          if (Number(tooling.failed_tool_call_count || 0) > 0) return `${{Number(tooling.failed_tool_call_count || 0)}} failed tools`;
          if (Number(tooling.tool_call_count || metadata.tool_call_count || 0) > 0) return `${{Number(tooling.tool_call_count || metadata.tool_call_count || 0)}} tool calls`;
          if (Number(retrieval.visible_source_count || retrieval.source_count || 0) > 0) return `${{Number(retrieval.visible_source_count || retrieval.source_count || 0)}} sources`;
          if (orchestrator.selected_route) return `route ${{orchestrator.selected_route}}`;
          if (model.provider || model.name || metadata.model_provider || metadata.model_name) return 'model configured';
          const categories = event.categories || metadata.categories || metadata.diagnostic_categories || metadata.diagnostic_summary?.categories || [];
          if (Array.isArray(categories) && categories.length) {{
            return categories.map(category => typeof category === 'string' ? category : (category.label || category.key || 'diagnostic')).slice(0, 2).join(', ');
          }}
          const type = String(event.event_type || '').toLowerCase();
          if (type.includes('review')) return 'scorecard';
          if (type.includes('slack')) return 'run saved';
          if (type.includes('summary')) return 'run summary';
          return metadata.diagnostic_contract ? 'diagnostic contract' : 'metadata only';
        }}
        function traceEventDetail(event) {{
          const duration = event.duration_ms === null || event.duration_ms === undefined ? '' : `${{event.duration_ms}} ms`;
          const metadata = event.metadata || {{}};
          const facts = traceAgenticFacts(event);
          const signal = [facts.tools, facts.retrieval, facts.model].filter(value => value && !value.endsWith('pending')).slice(0, 2).join(' · ');
          return [event.name, duration, signal].filter(Boolean).join(' · ') || 'sanitized trace metadata';
        }}
        function traceAgenticFacts(event) {{
          const agentic = event.agentic_summary || {{}};
          if (agentic.signal || agentic.tools || agentic.retrieval || agentic.model || agentic.route) {{
            return {{
              route: agentic.route || 'route tbd',
              tools: agentic.tools || 'tool metadata pending',
              retrieval: agentic.retrieval || 'retrieval metadata pending',
              model: agentic.model || 'model metadata pending',
            }};
          }}
          const metadata = event.metadata || {{}};
          const tooling = metadata.tooling || {{}};
          const retrieval = metadata.retrieval || metadata.retrieval_provider_summary || {{}};
          const model = metadata.model || {{}};
          const orchestrator = metadata.orchestrator || {{}};
          const toolCount = Number(tooling.tool_call_count || metadata.tool_call_count || 0);
          const failedToolCount = Number(tooling.failed_tool_call_count || 0);
          const toolNames = Array.isArray(tooling.tool_names) ? tooling.tool_names.filter(Boolean).slice(0, 3) : [];
          const sourceCount = Number(retrieval.visible_source_count || retrieval.source_count || 0);
          const provider = retrieval.search_provider || retrieval.provider_summary || '';
          const route = orchestrator.selected_route || metadata.route || metadata.agent || metadata.agent_name || '';
          const modelText = [model.provider || metadata.model_provider, model.name || metadata.model_name].filter(Boolean).join(' ');
          return {{
            route: route || 'route tbd',
            tools: toolCount
              ? `${{toolCount}} tool call${{toolCount === 1 ? '' : 's'}}${{failedToolCount ? ` · ${{failedToolCount}} failed` : ''}}${{toolNames.length ? ` · ${{toolNames.join(', ')}}` : ''}}`
              : failedToolCount
                ? `${{failedToolCount}} failed tool call${{failedToolCount === 1 ? '' : 's'}}`
                : 'tool metadata pending',
            retrieval: sourceCount
              ? `${{sourceCount}} source${{sourceCount === 1 ? '' : 's'}}${{provider ? ` · ${{provider}}` : ''}}`
              : provider
                ? `provider ${{provider}}`
                : 'retrieval metadata pending',
            model: modelText || 'model metadata pending',
          }};
        }}
        function traceSameRun(event, latestTrace) {{
          if (!latestTrace || !latestTrace.trace_id) return false;
          const metadata = event.metadata || {{}};
          const correlation = metadata.correlation || {{}};
          const keys = [
            event.trace_id,
            event.span_id,
            event.group_id,
            metadata.case_id,
            metadata.run_id,
            correlation.case_id,
            correlation.run_id,
          ].map(value => String(value || '')).filter(Boolean);
          const latestKeys = [
            latestTrace.trace_id,
            latestTrace.span_id,
            latestTrace.case_id,
            latestTrace.run_id,
          ].map(value => String(value || '')).filter(Boolean);
          return keys.some(key => latestKeys.includes(key));
        }}
        function traceTimelineMarkup(entries, options = {{}}) {{
          const includeCopy = options.includeCopy === true;
          const openable = options.openable === true;
          const selectedIndex = options.selectedIndex === undefined ? null : Number(options.selectedIndex);
          const rows = entries.map(entry => {{
            const event = entry.event || entry;
            const index = entry.index;
            const joinKey = traceJoinKey(event) || event.trace_id || event.span_id || '';
            const route = traceEventRoute(event) || 'route tbd';
            const signal = traceEventSignal(event);
            const openAttrs = openable
              ? ` data-trace-open="${{index}}" role="button" tabindex="0" aria-label="Open full trace for ${{escapeHtml(traceStepLabel(event))}}"`
              : '';
            const selectedClass = openable && selectedIndex === Number(index) ? ' selected' : '';
            return `<div class="trace-timeline-row${{selectedClass}}"${{openAttrs}}>
              <div>
                <div class="mono">${{escapeHtml(formatDateTime(event.created_at) || event.created_at || 'time tbd')}}</div>
                <div class="trace-step-detail">${{escapeHtml(event.event_type || '')}}</div>
              </div>
              <div>
                <div class="trace-step-name">${{escapeHtml(traceStepLabel(event))}}</div>
                <div class="trace-step-detail">${{escapeHtml(traceEventDetail(event))}}</div>
              </div>
              <div>
                <div class="mono">${{dbMetric(joinKey)}}</div>
                <div class="trace-step-detail">case/run/work item join</div>
              </div>
              <div>
                <div class="mono">${{escapeHtml(route)}}</div>
                <div class="trace-step-detail">agent or route</div>
              </div>
              <div>
                <span class="trace-signal">${{escapeHtml(signal)}}</span>
                ${{includeCopy ? `<div class="trace-row-actions">
                  <button class="ledger-copy" type="button" data-trace-detail-index="${{index}}" aria-label="Show full sanitized trace details">Details</button>
                  <button class="ledger-copy" type="button" data-trace-index="${{index}}" data-copy-trace-event="${{escapeHtml(event.row_id || event.span_id || event.trace_id || index)}}" aria-label="Copy this trace event for Codex review">Copy</button>
                </div>` : ''}}
              </div>
            </div>`;
          }}).join('');
          return `<div class="trace-timeline">
            <div class="trace-timeline-row trace-timeline-head">
              <div>Time</div>
              <div>Step</div>
              <div>Join key</div>
              <div>Route</div>
              <div>Signal</div>
            </div>
            ${{rows}}
          </div>`;
        }}
        function renderTraceSummary() {{
          const trace = data.trace_summary || {{}};
          const envRows = Object.entries(trace.recommended_env || {{}});
          const latestTrace = trace.latest_run_trace || {{}};
          const events = trace.recent_events || [];
          const indexedEvents = events.map((event, index) => ({{ event, index }}));
          const currentRunRows = indexedEvents.filter(entry => traceSameRun(entry.event, latestTrace)).slice(0, 6);
          const latestEvent = currentRunRows[0]?.event || events[0] || {{}};
          const latestFacts = traceAgenticFacts(latestEvent);
          renderTraceAnalytics(trace);
          renderTraceDatabaseFreshness();
      document.getElementById('trace-reader-guide').innerHTML = [
        ['Standard trace', 'ordered run steps with event type, time, route, and duration when available'],
        ['Keystone joins', 'case id, Slack run id, WorkItem id, review score, and dashboard/review links'],
        ['Privacy boundary', 'sanitized metadata only; no raw prompts, responses, Slack text, secrets, or PHI'],
      ].map(([label, hint]) => `
        <div class="trace-guide-item">
          <strong>${{escapeHtml(label)}}</strong>
          <span>${{escapeHtml(hint)}}</span>
        </div>
      `).join('');
      if (latestTrace.trace_id) {{
        const latestCategories = Array.isArray(latestTrace.categories) && latestTrace.categories.length
          ? latestTrace.categories.join(', ')
          : 'no event categories';
        const readiness = latestTrace.field_readiness || latestEvent.field_readiness || {{}};
        const readinessLabel = readiness.attention_count
          ? `${{readiness.attention_count}} fields need attention`
          : 'core fields populated';
        const readinessHint = readiness.summary || '';
        document.getElementById('trace-latest-run').innerHTML = `<div class="trace-run-card">
          <div class="trace-run-top">
            <div>
              <div class="trace-run-title">${{escapeHtml(latestTrace.case_id || latestTrace.run_id || latestTrace.trace_id || 'Latest trace event')}}</div>
              <div class="trace-run-subtitle">${{escapeHtml(formatDateTime(latestTrace.created_at) || latestTrace.created_at || 'time tbd')}} · ${{escapeHtml(latestTrace.agent || latestTrace.route || 'agent tbd')}}</div>
            </div>
            <span class="trace-signal">${{escapeHtml(latestCategories)}}</span>
          </div>
          <div class="trace-run-meta">
            <div class="trace-run-meta-item"><div class="label">Run id</div><div class="value mono">${{dbMetric(latestTrace.run_id || 'run tbd')}}</div></div>
            <div class="trace-run-meta-item"><div class="label">Route</div><div class="value mono">${{escapeHtml(latestFacts.route || latestTrace.agent || latestTrace.route || 'route tbd')}}</div></div>
            <div class="trace-run-meta-item"><div class="label">Tools</div><div class="value">${{escapeHtml(latestFacts.tools)}}</div></div>
            <div class="trace-run-meta-item"><div class="label">Retrieval</div><div class="value">${{escapeHtml(latestFacts.retrieval)}}</div></div>
            <div class="trace-run-meta-item"><div class="label">Model</div><div class="value">${{escapeHtml(latestFacts.model)}}</div></div>
            <div class="trace-run-meta-item"><div class="label">Field readiness</div><div class="value">${{escapeHtml(readinessLabel)}}</div><div class="subtle">${{escapeHtml(readinessHint)}}</div></div>
            <div class="trace-run-meta-item"><div class="label">Joined case</div><div class="value">${{latestTrace.case_id ? `<a href="/dashboard?case=${{encodeURIComponent(latestTrace.case_id)}}">Open case</a>` : 'case tbd'}}</div></div>
          </div>
        </div>`;
      }} else {{
        document.getElementById('trace-latest-run').innerHTML = '<div class="subtle">No trace events are saved yet for the latest run.</div>';
      }}
      const timelineTarget = document.getElementById('trace-current-timeline');
      if (timelineTarget) {{
        timelineTarget.innerHTML = currentRunRows.length
          ? traceTimelineMarkup(currentRunRows)
          : '<div class="subtle">No multi-step timeline is available for this run yet. The saved trace still links by case/run id above.</div>';
      }}
      document.getElementById('trace-summary').innerHTML = [
        ['Processor mode', trace.mode || 'disabled', trace.enabled ? 'Ready to persist safe trace summaries' : 'Set KEYSTONE_TRACE_PROCESSOR=eval_summary before future API evals'],
        ['Saved events', String(trace.event_count || 0), `local DB: ${{trace.database_path || data.database_path || ''}}`],
        ['Run summaries', `${{trace.joined_run_summary_count || 0}}/${{trace.run_summary_count || 0}} joined`, trace.run_summary_count ? `${{trace.sdk_run_summary_count || 0}} SDK; ${{trace.manual_run_summary_count || 0}} manual no-API summaries.` : 'No SDK or manual run summary rows have been saved yet.'],
        ['Sensitive capture', trace.effective_sensitive_capture ? 'enabled' : 'disabled', `effective SDK config; env: ${{trace.sensitive_data_env || 'default_false'}}`],
        ['Primary use', 'debug readiness', 'trace/span timing, routing metadata, and join keys without prompt or response text'],
      ].map(([label, value, hint]) => `
        <div class="analysis-card">
          <div class="label">${{labelWithInfo(label)}}</div>
          <div class="value">${{escapeHtml(value)}}</div>
          <div class="subtle">${{escapeHtml(hint)}}</div>
        </div>
      `).join('');
      document.getElementById('trace-fields').innerHTML = `
        <div class="score-grid">
          ${{(trace.stored_fields || []).map(field => pill(`store: ${{field}}`, 'pass')).join('')}}
        </div>
        <div class="score-grid" style="margin-top:8px">
          ${{(trace.dropped_fields || []).map(field => pill(`drop: ${{field}}`, 'warn')).join('')}}
        </div>`;
      document.getElementById('trace-implementation').innerHTML = `
        <div class="trace-table-wrap">
        <table class="analysis-table trace-table">
          <colgroup><col style="width: 34%"><col style="width: 66%"></colgroup>
          <thead><tr><th>Setting / step</th><th>Value</th></tr></thead>
          <tbody>
            ${{envRows.map(([key, value]) => `<tr><td class="mono">${{escapeHtml(key)}}</td><td class="mono">${{escapeHtml(String(value || ''))}}</td></tr>`).join('')}}
            ${{(trace.implementation_notes || []).map(note => `<tr><td>Implementation note</td><td>${{escapeHtml(note)}}</td></tr>`).join('')}}
          </tbody>
        </table>
        </div>`;
      const diagnosticRows = trace.diagnostic_category_counts || [];
      const categoryTarget = document.getElementById('trace-diagnostic-categories');
      if (!diagnosticRows.length) {{
        categoryTarget.innerHTML = '<div class="subtle">No diagnostic categories have been emitted yet. New manual/API run summaries will populate this once trace metadata includes the diagnostics contract.</div>';
          }} else {{
            const maxDiagnostic = Math.max(1, ...diagnosticRows.map(row => Number(row.count || 0)));
            categoryTarget.innerHTML = diagnosticRows.map(row => {{
              return `
                <div class="bar-row" title="${{escapeHtml(row.detail || '')}}">
                  <div class="truncate">${{escapeHtml(row.label || row.key || '')}}</div>
                  <div class="bar-track"><div class="bar neutral" style="width:${{Math.round(Number(row.count || 0) / maxDiagnostic * 100)}}%"></div></div>
                  <div class="mono">${{Number(row.count || 0)}}</div>
                </div>`;
            }}).join('');
          }}
          const trendRows = trace.diagnostic_category_trends || [];
          renderTraceDiagnosticTrendChart(trendRows);
          const trendTarget = document.getElementById('trace-diagnostic-trends');
          if (!trendRows.length) {{
            trendTarget.innerHTML = '';
      }} else {{
        trendTarget.innerHTML = `<div class="trace-table-wrap" style="margin-top:10px">
          <table class="analysis-table trace-table">
            <colgroup>
              <col style="width: 22%">
              <col style="width: 46%">
              <col style="width: 16%">
              <col style="width: 16%">
            </colgroup>
            <thead><tr><th>Date</th><th>Diagnostic category</th><th>Severity</th><th>Count</th></tr></thead>
                <tbody>${{trendRows.slice(-36).map(row => `<tr>
                  <td class="mono">${{escapeHtml(row.date || '')}}</td>
                  <td>${{escapeHtml(row.label || row.key || '')}}</td>
                  <td>${{traceDiagnosticBadge(row.severity || 'info')}}</td>
                  <td class="mono">${{Number(row.count || 0)}}</td>
                </tr>`).join('')}}</tbody>
              </table>
        </div>`;
      }}
      const followups = trace.diagnostic_case_rollups || trace.diagnostic_followups || [];
      const followupTarget = document.getElementById('trace-diagnostic-followups');
      if (!followups.length) {{
        followupTarget.innerHTML = '';
      }} else {{
        followupTarget.innerHTML = `<div class="trace-table-wrap" style="margin-top:10px">
          <table class="analysis-table trace-table">
            <colgroup>
              <col style="width: 16%">
              <col style="width: 20%">
              <col style="width: 10%">
              <col style="width: 18%">
              <col style="width: 36%">
            </colgroup>
            <thead><tr><th>Latest event</th><th>Join key / case</th><th>Events</th><th>Agent</th><th>Diagnostic categories</th></tr></thead>
            <tbody>${{followups.slice(0, 12).map(row => `<tr>
                  <td>${{escapeHtml(formatDateTime(row.latest_created_at || row.created_at) || row.latest_created_at || row.created_at || '')}}</td>
                  <td class="mono">${{dbMetric(row.join_key || row.group_id || row.trace_id || '')}}</td>
                  <td class="mono">${{escapeHtml(String(row.event_count || 1))}}</td>
                  <td class="mono">${{escapeHtml(row.agent || row.route || '')}}</td>
                  <td>${{(row.categories || []).map(category => typeof category === 'string' ? traceDiagnosticBadge(category) : traceDiagnosticBadge(`${{category.label || category.key}}${{Number(category.count || 0) > 1 ? ` (${{category.count}})` : ''}}`)).join('')}}</td>
                </tr>`).join('')}}</tbody>
              </table>
        </div>`;
      }}
      if (!events.length) {{
        document.getElementById('trace-events').innerHTML = '<div class="subtle">No local trace events are saved yet. That is expected before future API eval runs or dry local trace processor tests.</div>';
        document.getElementById('trace-event-detail').innerHTML = '';
        return;
      }}
          document.getElementById('trace-events').innerHTML = traceTimelineMarkup(indexedEvents.slice(0, 20), {{ includeCopy: true, openable: true }});
          const traceEventsTarget = document.getElementById('trace-events');
          for (const row of traceEventsTarget.querySelectorAll('[data-trace-open]')) {{
            row.addEventListener('click', event => {{
              if (event.target.closest('button')) return;
              showTraceEventDetail(Number(row.getAttribute('data-trace-open') || -1));
            }});
            row.addEventListener('keydown', event => {{
              if (event.key === 'Enter' || event.key === ' ') {{
                event.preventDefault();
                showTraceEventDetail(Number(row.getAttribute('data-trace-open') || -1));
              }}
            }});
          }}
          for (const button of traceEventsTarget.querySelectorAll('[data-trace-index]')) {{
            button.addEventListener('click', () => copyTraceReview(button));
          }}
          for (const button of traceEventsTarget.querySelectorAll('[data-trace-detail-index]')) {{
            button.addEventListener('click', () => showTraceEventDetail(Number(button.getAttribute('data-trace-detail-index') || -1)));
          }}
        }}
        function traceJoinKey(event) {{
          const metadata = event.metadata || {{}};
          const correlation = metadata.correlation || {{}};
          return String(
            event.group_id ||
            metadata.case_id ||
            correlation.case_id ||
            metadata.work_item_id ||
            correlation.work_item_id ||
            metadata.run_id ||
            correlation.run_id ||
            ''
          );
        }}
        function traceMetadataPreview(event, limit = 320) {{
          const text = JSON.stringify(event.metadata || {{}});
          if (!text || text === '{{}}') return 'no metadata';
          return text.length > limit ? `${{text.slice(0, limit)}}...` : text;
        }}
        function traceJoinedCase(event) {{
          const joinKey = traceJoinKey(event);
          return {{
            join_key: joinKey,
            dashboard_url: joinKey ? `/dashboard?case=${{encodeURIComponent(joinKey)}}` : '',
            review_url: joinKey ? `/review?case=${{encodeURIComponent(joinKey)}}` : '',
            case_bundle_url: joinKey ? `/api/eval-case-bundle?case=${{encodeURIComponent(joinKey)}}` : '',
          }};
        }}
        function traceFieldReadinessMarkup(readiness) {{
          const checks = Array.isArray(readiness?.checks) ? readiness.checks : [];
          if (!checks.length) {{
            return '<div class="subtle">Field readiness is not available for this trace event.</div>';
          }}
          return `<div class="trace-readiness-list">
            ${{checks.map(check => `<div class="trace-readiness-item">
              <div>
                <span class="trace-signal">${{escapeHtml(check.status || 'pending')}}</span>
                <div class="trace-step-name">${{escapeHtml(check.label || check.key || '')}}</div>
              </div>
              <div class="subtle">${{escapeHtml(check.detail || '')}}</div>
            </div>`).join('')}}
          </div>`;
        }}
        function traceFullPacket(event) {{
          const metadataText = JSON.stringify(event.metadata || {{}});
          const metadata = event.metadata || {{}};
          const correlation = metadata.correlation || {{}};
          const execution = metadata.execution || {{}};
          const slackContext = metadata.slack_context || {{}};
          const durationMs = event.duration_ms ?? execution.duration_ms ?? execution.time_to_response_ms ?? null;
          const joinedCase = traceJoinedCase(event);
          const metadataLimit = 4000;
          return {{
            schema: 'keystone.eval.trace_event_detail.v1',
            review_scope: 'single sanitized trace event detail',
            cost_guardrail: 'No API or Slack call from dashboard trace inspection',
            privacy_policy: 'Dashboard trace detail includes sanitized metadata only; raw prompts, responses, Slack text, tool I/O, secrets, and PHI are omitted.',
            timestamp: String(event.created_at || ''),
            display_time: formatDateTime(event.created_at) || String(event.created_at || ''),
            event_type: String(event.event_type || ''),
            name: String(event.name || ''),
            trace_id: String(event.trace_id || ''),
            span_id: String(event.span_id || ''),
            parent_id: String(event.parent_id || ''),
            group_id: String(event.group_id || ''),
            join_key: joinedCase.join_key,
            joined_case: joinedCase,
            slack_channel: {{
              id: String(event.slack_channel_id || metadata.slack_channel_id || correlation.slack_channel_id || slackContext.channel_id || ''),
              name: String(event.slack_channel_name || metadata.slack_channel_name || correlation.slack_channel_name || slackContext.channel_name || ''),
              thread_ts: String(event.slack_thread_ts || metadata.slack_thread_ts || correlation.slack_thread_ts || slackContext.thread_ts || ''),
            }},
            route: traceEventRoute(event),
            signal: traceEventSignal(event),
            duration_ms: durationMs,
            time_to_response_ms: durationMs,
            field_readiness: event.field_readiness || null,
            agentic_summary: event.agentic_summary || null,
            diagnostic_summary: metadata.diagnostic_summary || null,
            diagnostic_contract: metadata.diagnostic_contract || null,
            metadata_excerpt: metadataText.slice(0, metadataLimit),
            metadata_truncated: metadataText.length > metadataLimit,
          }};
        }}
        function showTraceEventDetail(index) {{
          const events = ((data.trace_summary || {{}}).recent_events || []);
          const event = events[index];
          const target = document.getElementById('trace-event-detail');
          if (!event || !target) return;
          const metadata = event.metadata || {{}};
          const correlation = metadata.correlation || {{}};
          const execution = metadata.execution || {{}};
          const durationMs = event.duration_ms ?? execution.duration_ms ?? execution.time_to_response_ms ?? null;
          const slackContext = metadata.slack_context || {{}};
          const slackChannel = event.slack_channel_name || metadata.slack_channel_name || correlation.slack_channel_name || slackContext.channel_name || event.slack_channel_id || metadata.slack_channel_id || correlation.slack_channel_id || slackContext.channel_id || '';
          const joinedCase = traceJoinedCase(event);
          const readiness = event.field_readiness || {{}};
          const packet = traceFullPacket(event);
          const selectedRun = {{
            trace_id: event.trace_id,
            span_id: event.span_id,
            case_id: joinedCase.join_key,
            run_id: metadata.run_id || correlation.run_id || event.group_id || '',
          }};
          const sameRunRows = events
            .map((candidate, candidateIndex) => ({{ event: candidate, index: candidateIndex }}))
            .filter(entry => traceSameRun(entry.event, selectedRun))
            .slice(0, 8);
          document.getElementById('trace-events').innerHTML = traceTimelineMarkup(
            events.map((candidate, candidateIndex) => ({{ event: candidate, index: candidateIndex }})).slice(0, 20),
            {{ includeCopy: true, openable: true, selectedIndex: index }}
          );
          const traceEventsTarget = document.getElementById('trace-events');
          for (const row of traceEventsTarget.querySelectorAll('[data-trace-open]')) {{
            row.addEventListener('click', clickEvent => {{
              if (clickEvent.target.closest('button')) return;
              showTraceEventDetail(Number(row.getAttribute('data-trace-open') || -1));
            }});
            row.addEventListener('keydown', keyEvent => {{
              if (keyEvent.key === 'Enter' || keyEvent.key === ' ') {{
                keyEvent.preventDefault();
                showTraceEventDetail(Number(row.getAttribute('data-trace-open') || -1));
              }}
            }});
          }}
          for (const button of traceEventsTarget.querySelectorAll('[data-trace-index]')) {{
            button.addEventListener('click', () => copyTraceReview(button));
          }}
          for (const button of traceEventsTarget.querySelectorAll('[data-trace-detail-index]')) {{
            button.addEventListener('click', () => showTraceEventDetail(Number(button.getAttribute('data-trace-detail-index') || -1)));
          }}
          target.innerHTML = `<div class="trace-detail-panel">
            <div class="trace-detail-head">
              <div>
                <div class="trace-detail-title">${{escapeHtml(traceStepLabel(event))}}</div>
                <div class="trace-run-subtitle">${{escapeHtml(formatDateTime(event.created_at) || event.created_at || 'time tbd')}} · ${{escapeHtml(traceEventRoute(event) || 'route tbd')}}</div>
              </div>
              <span class="trace-signal">${{escapeHtml(traceEventSignal(event))}}</span>
            </div>
            <div class="trace-detail-grid">
              <div class="trace-detail-cell"><div class="label">Join key</div><div class="value mono">${{dbMetric(joinedCase.join_key || 'join tbd')}}</div></div>
              <div class="trace-detail-cell"><div class="label">Trace</div><div class="value mono">${{dbMetric(event.trace_id || 'trace tbd')}}</div></div>
              <div class="trace-detail-cell"><div class="label">Span</div><div class="value mono">${{dbMetric(event.span_id || 'span tbd')}}</div></div>
              <div class="trace-detail-cell"><div class="label">Time to response</div><div class="value">${{durationMs === null || durationMs === undefined ? 'tbd' : `${{Number(durationMs).toFixed(0)}} ms`}}</div></div>
              <div class="trace-detail-cell"><div class="label">Slack channel</div><div class="value mono">${{dbMetric(slackChannel || 'channel tbd')}}</div></div>
              <div class="trace-detail-cell"><div class="label">Thread</div><div class="value mono">${{dbMetric(event.slack_thread_ts || metadata.slack_thread_ts || correlation.slack_thread_ts || slackContext.thread_ts || 'thread tbd')}}</div></div>
            </div>
            <div class="analysis-handoff">
              ${{joinedCase.dashboard_url ? `<a class="secondary-action" href="${{joinedCase.dashboard_url}}">Open Dashboard Case</a>` : ''}}
              ${{joinedCase.review_url ? `<a class="secondary-action" href="${{joinedCase.review_url}}">Open Review</a>` : ''}}
              ${{joinedCase.case_bundle_url ? `<a class="secondary-action" href="${{joinedCase.case_bundle_url}}">Open Case Bundle</a>` : ''}}
            </div>
            <div>
              <div class="trace-step-name">Field readiness</div>
              <div class="subtle">${{escapeHtml(readiness.summary || 'Trace readiness checks summarize which OpenAI-style and Keystone-specific fields are populated.')}}</div>
              ${{traceFieldReadinessMarkup(readiness)}}
            </div>
            <div>
              <div class="trace-step-name">Same-run timeline</div>
              <div class="subtle">Events sharing case, run, WorkItem, trace, or span identifiers.</div>
              ${{sameRunRows.length ? traceTimelineMarkup(sameRunRows) : '<div class="subtle">No same-run events were found for this trace event.</div>'}}
            </div>
            <div>
              <div class="trace-step-name">Bounded sanitized trace packet</div>
              <div class="subtle">Capped metadata for local inspection. Raw prompts, responses, Slack text, secrets, and PHI are not included; inspect the local DB row when deeper sanitized metadata is needed.</div>
              <pre class="trace-json">${{escapeHtml(JSON.stringify(packet, null, 2))}}</pre>
            </div>
          </div>`;
          target.scrollIntoView({{ block: 'nearest' }});
        }}
        function traceReviewText(event) {{
          const metadataText = JSON.stringify(event.metadata || {{}});
          const metadata = event.metadata || {{}};
          const correlation = metadata.correlation || {{}};
          const execution = metadata.execution || {{}};
          const slackContext = metadata.slack_context || {{}};
          const durationMs = event.duration_ms ?? execution.duration_ms ?? execution.time_to_response_ms ?? null;
          const joinKey = traceJoinKey(event);
          const fieldReadiness = event.field_readiness || {{}};
          const missingFields = fieldReadiness.missing || [];
          const diagnosticsComplete = metadata.diagnostic_contract && !Number(fieldReadiness.attention_count || 0);
          const joinedCase = traceJoinedCase(event);
          const metadataLimit = 1200;
          const payload = {{
            schema: 'keystone.eval.trace_event_review.v1',
            review_scope: 'single sanitized trace event',
            cost_guardrail: 'No API or Slack call from dashboard copy',
            copy_policy: 'Metadata is sanitized and capped for clipboard review; inspect local DB only if deeper trace detail is needed.',
            review_checklist: [
              {{
                label: 'join_key',
                status: joinKey ? 'complete' : 'missing',
                detail: joinKey ? 'Trace includes dashboard, review, and case-bundle links for the joined case/run/WorkItem.' : 'Trace event lacks case/run/WorkItem join metadata.',
              }},
              {{
                label: 'sanitized_metadata',
                status: 'complete',
                detail: 'Dashboard trace rows intentionally omit raw prompts, responses, tool I/O, raw Slack messages, secrets, and PHI.',
              }},
              {{
                label: 'run_diagnostics',
                status: diagnosticsComplete ? 'complete' : 'attention',
                detail: diagnosticsComplete
                  ? 'Trace includes compact timing, model, tool, retrieval, approval, side-effect, prompt/config, cost/cache, and error/retry diagnostics.'
                  : metadata.diagnostic_contract
                  ? `Trace has the diagnostics contract but still needs populated fields: ${{missingFields.join(', ') || 'field readiness pending'}}.`
                  : 'Trace has join metadata but does not yet include the compact run diagnostics contract.',
              }},
              {{
                label: 'api_ready_trace',
                status: event.event_type === 'sdk_run_summary' && joinKey ? 'complete' : 'attention',
                    detail: event.event_type === 'sdk_run_summary'
                      ? 'SDK run summary event is present.'
                      : event.event_type === 'manual_run_summary'
                      ? 'Manual no-API run summary verifies local join keys; future API evals still need sdk_run_summary events.'
                      : 'Future API evals should include a joined sdk_run_summary event for run-level diagnosis.',
                  }},
            ],
            next_follow_up: joinKey
              ? (missingFields.length
                ? `Compare joined_case links, then populate missing trace fields: ${{missingFields.join(', ')}}.`
                : 'Open joined_case links to compare this trace event with the case, ledger row, review row, case bundle, and dashboard data-quality gate.')
              : 'Add case_id, run_id, or work_item_id metadata before relying on this trace for API eval diagnosis.',
            field_readiness: fieldReadiness,
            missing_relevant_fields: missingFields,
            agentic_summary: event.agentic_summary || null,
            timestamp: String(event.created_at || ''),
            display_time: formatDateTime(event.created_at) || String(event.created_at || ''),
            event_type: String(event.event_type || ''),
            name: String(event.name || ''),
            trace_id: String(event.trace_id || ''),
            span_id: String(event.span_id || ''),
            parent_id: String(event.parent_id || ''),
            group_id: String(event.group_id || ''),
            join_key: joinKey,
            joined_case: joinedCase,
            slack_channel: {{
              id: String(event.slack_channel_id || metadata.slack_channel_id || correlation.slack_channel_id || slackContext.channel_id || ''),
              name: String(event.slack_channel_name || metadata.slack_channel_name || correlation.slack_channel_name || slackContext.channel_name || ''),
              thread_ts: String(event.slack_thread_ts || metadata.slack_thread_ts || correlation.slack_thread_ts || slackContext.thread_ts || ''),
            }},
            duration_ms: durationMs,
            time_to_response_ms: durationMs,
            diagnostic_summary: metadata.diagnostic_summary || null,
            diagnostic_contract: metadata.diagnostic_contract || null,
            metadata_excerpt: metadataText.slice(0, metadataLimit),
            metadata_truncated: metadataText.length > metadataLimit,
          }};
          return [
            'Please review this Keystone eval trace event and identify any trace join, data-quality, dashboard, or API-readiness follow-up needed. Use review_checklist and next_follow_up first.',
            '',
            '```json',
            JSON.stringify(payload, null, 2),
            '```',
          ].join('\\n');
        }}
        async function copyTraceReview(button) {{
          const original = button.textContent;
          const index = Number(button.getAttribute('data-trace-index') || -1);
          const event = ((data.trace_summary || {{}}).recent_events || [])[index];
          if (!event) {{
            button.textContent = 'Missing';
            setTimeout(() => {{ button.textContent = original; }}, 1200);
            return;
          }}
          try {{
            await writeClipboardReviewText(button, traceReviewText(event));
          }} catch (error) {{
            button.textContent = 'Manual copy';
          }}
          setTimeout(() => {{ button.textContent = original; }}, 1200);
        }}
        function scoreOptions(selected, pending = false) {{
      if (pending || selected === null || selected === undefined || selected === '') {{
        return '<option value="" selected>tbd</option>' + [0, 1, 2, 3, 4, 5].map(value => `<option value="${{value}}">${{value}}</option>`).join('');
      }}
      return [0, 1, 2, 3, 4, 5].map(value => {{
        const isSelected = Number(selected) === value ? ' selected' : '';
        return `<option value="${{value}}"${{isSelected}}>${{value}}</option>`;
      }}).join('');
    }}
    function hasRecordedResponse(item) {{
      return Boolean(String(item.scored_response_text || item.response_text || item.latest_slack_summary || '').trim());
    }}
    function canJudgeScore(item) {{
      const target = item.review_target || {{}};
      return Boolean(
        orchestratorJudge.enabled &&
        target.target_type === 'slack' &&
        String(item.latest_slack_summary || item.scored_response_text || '').trim()
      );
    }}
    function scoreForm(item) {{
      const reviewReady = hasRecordedResponse(item);
      const disabled = reviewReady ? '' : ' disabled';
      const disabledReason = reviewReady
        ? ''
        : '<div class="subtle">Score saving is disabled until this case has a recorded Promptfoo or Slack response.</div>';
      const judgeReady = canJudgeScore(item);
      const judgeReason = judgeReady
        ? 'Scores this saved #evals Slack output with Orchestrator Review.'
        : orchestratorJudge.enabled
          ? 'Orchestrator Review scoring needs a saved #evals Slack response.'
          : `Set ${{escapeHtml(orchestratorJudge.env_flag || 'KEYSTONE_EVAL_LLM_JUDGE')}}=true and restart the dashboard to enable Orchestrator Review scoring.`;
      const controls = scoreDimensions.map(dimension => `
        <div class="score-control">
          <label>${{escapeHtml(dimension)}}</label>
          <select data-score="${{escapeHtml(dimension)}}"${{disabled}}>${{scoreOptions(item.human_scores?.[dimension], !reviewReady)}}</select>
        </div>
      `).join('');
      return `<form class="review-form" data-case-id="${{escapeHtml(item.case_id)}}" aria-disabled="${{reviewReady ? 'false' : 'true'}}">
        <div class="detail-label">Update Saved Review</div>
        ${{disabledReason}}
        <div class="score-controls">
          ${{controls}}
          <div class="score-control">
            <label>safety</label>
            <select data-safety${{disabled}}>
              ${{!reviewReady || !item.human_safety ? '<option value="" selected>tbd</option>' : '<option value="">tbd</option>'}}
              <option value="pass"${{reviewReady && item.human_safety === 'pass' ? ' selected' : ''}}>pass</option>
              <option value="fail"${{reviewReady && item.human_safety === 'fail' ? ' selected' : ''}}>fail</option>
            </select>
          </div>
        </div>
        <div class="detail-label">Human notes</div>
        <textarea data-notes aria-label="Human notes" placeholder="Saved to the Human notes column"${{disabled}}>${{escapeHtml(item.human_notes || '')}}</textarea>
        <div class="subtle">Use this dashboard field to modify notes already submitted from the Slack eval thread.</div>
        <div class="form-actions">
          <button type="submit"${{disabled}}>Update review</button>
          <button type="button" data-judge-score="${{escapeHtml(item.case_id)}}"${{judgeReady ? '' : ' disabled'}}>Score with Orchestrator Review</button>
          <span class="review-status" data-review-status></span>
        </div>
        <div class="subtle">${{judgeReason}}</div>
      </form>`;
    }}
    function analysisToggle(item) {{
      if (!item.promptfoo_eval_id) {{
        return '<span class="subtle">Analysis inclusion appears after a Promptfoo machine-check run.</span>';
      }}
      const excluded = Boolean(item.analysis_excluded);
      const label = excluded ? 'Include in analysis' : 'Exclude from analysis';
      const state = excluded ? 'analysis excluded' : 'analysis included';
      return `<div class="case-actions">
        ${{pill(state, excluded ? 'warn' : 'pass')}}
        <button
          type="button"
          class="analysis-toggle ${{excluded ? 'excluded' : ''}}"
          data-analysis-toggle
          data-case-id="${{escapeHtml(item.case_id || '')}}"
          data-eval-id="${{escapeHtml(item.promptfoo_eval_id || '')}}"
          data-analysis-excluded="${{excluded ? 'true' : 'false'}}"
        >${{escapeHtml(label)}}</button>
      </div>`;
    }}
    async function copyPrompt(button) {{
      const original = button.textContent;
      const source = button.closest('article, section')?.querySelector('[data-copy-source]');
      const promptText = source?.value || '';
      if (!promptText.trim()) {{
        button.textContent = 'No prompt';
        setTimeout(() => {{ button.textContent = original; }}, 1200);
        return;
      }}
      try {{
        source.focus();
        source.select();
        const copied = document.execCommand('copy');
        if (!copied) {{
          await writeClipboardReviewText(button, promptText || '');
        }} else {{
          button.textContent = 'Copied';
        }}
      }} catch (error) {{
        await writeClipboardReviewText(button, promptText || '');
      }}
      setTimeout(() => {{ button.textContent = original; }}, 1200);
    }}
    function caseBundleReviewText(item) {{
      const promptText = String(item.user_input || '');
      const responseText = String(item.scored_response_text || item.response_text || item.latest_slack_summary || item.promptfoo_reason || '');
      const promptLimit = {CASE_BUNDLE_PROMPT_CHAR_LIMIT};
      const responseLimit = {CASE_BUNDLE_RESPONSE_CHAR_LIMIT};
      const payload = {{
        schema: 'keystone.eval.case_review_bundle.v1',
        copy_policy: 'Prompt and response fields are bounded excerpts for clipboard review; use dashboard/review URLs or the local DB if truncated text needs deeper inspection.',
        case_id: String(item.case_id || ''),
        display_case_id: String(item.display_case_id || item.case_id || ''),
        agent: String(item.agent || ''),
        dimensions: Array.isArray(item.dimensions) ? item.dimensions : [],
        dashboard_url: String(item.dashboard_url || ''),
        review_url: String(item.review_url || ''),
        case_bundle_url: String(item.case_bundle_url || ''),
        latest_run: {{
          source: String(item.latest_run_source || ''),
          at: String(item.latest_run_at || ''),
          id: String(item.latest_run_id || ''),
          thread_ts: String(item.latest_slack_thread_ts || ''),
        }},
        prompt: promptText.slice(0, promptLimit),
        prompt_chars: promptText.length,
        prompt_truncated: promptText.length > promptLimit,
        latest_response: responseText.slice(0, responseLimit),
        latest_response_chars: responseText.length,
        latest_response_truncated: responseText.length > responseLimit,
        machine: {{
          status: item.promptfoo_success === true ? 'pass' : item.promptfoo_success === false ? 'fail' : 'pending',
          score: item.promptfoo_score === null || item.promptfoo_score === undefined ? 'score tbd' : scoreOutOfFive(item.promptfoo_score, true),
          eval_id: String(item.promptfoo_eval_id || ''),
          reason: String(item.promptfoo_reason || ''),
        }},
        slack: {{
          run_count: Number(item.slack_run_count || 0),
          run_id: String(item.latest_slack_run_id || ''),
          thread_ts: String(item.latest_slack_thread_ts || ''),
          thread_fetch_status: String(item.latest_slack_thread_fetch_status || ''),
          message_count: Number(item.latest_slack_message_count || 0),
          visible_sources: Number(item.latest_slack_visible_source_count || 0),
          source_count: Number(item.latest_slack_source_count || 0),
          warning_count: Number(item.latest_slack_warning_count || 0),
          warnings: Array.isArray(item.latest_slack_warnings) ? item.latest_slack_warnings : [],
          cost_profile: String(item.latest_slack_cost_profile || ''),
          cache_hit_rate: item.latest_slack_sdk_cache_hit_rate ?? null,
        }},
        human_review: {{
          average: item.human_average === null || item.human_average === undefined ? null : scoreOutOfFive(item.human_average),
          safety: String(item.human_safety || ''),
          scores: item.human_scores || {{}},
          notes: String(item.human_notes || ''),
        }},
        review_target: item.review_target || {{}},
        analysis: {{
          included: Boolean(item.promptfoo_eval_id && !item.analysis_excluded),
          exclusion_reason: String(item.analysis_exclusion_reason || ''),
        }},
        scoring_contract: item.scoring_contract || {{}},
        review_checklist: Array.isArray(item.case_review_checklist) ? item.case_review_checklist : [],
        next_follow_up: String(item.next_follow_up || ''),
      }};
      return [
        'Please review this Keystone eval case and identify any scoring, evidence, dashboard, or workflow follow-up needed. Use review_checklist and next_follow_up first.',
        '',
        '```json',
        JSON.stringify(payload, null, 2),
        '```',
      ].join('\\n');
    }}
    async function copyCaseBundle(button) {{
      const original = button.textContent;
      const caseId = button.getAttribute('data-copy-case') || button.getAttribute('data-copy-followup-case') || '';
      const item = cases.find(candidate => candidate.case_id === caseId);
      if (!item) {{
        button.textContent = 'Missing';
        setTimeout(() => {{ button.textContent = original; }}, 1200);
        return;
      }}
      try {{
        await writeClipboardReviewText(button, caseBundleReviewText(item));
      }} catch (error) {{
        button.textContent = 'Manual copy';
      }}
      setTimeout(() => {{ button.textContent = original; }}, 1200);
    }}
    const scoringContractOrder = [
      'expected_route',
      'expected_status',
      'expected_pack_type',
      'expected_next_action_agent',
      'min_source_count',
      'min_artifact_count',
      'required_source_types',
      'required_source_url_prefixes',
      'required_artifact_types',
      'required_context_sources',
      'required_specialist_routes',
      'required_payload_terms',
      'required_terms',
      'forbidden_terms',
      'max_elapsed_seconds',
      'enforce_no_send',
      'require_live_flags_false',
    ];
    function scoringContractValueHtml(value) {{
      if (Array.isArray(value)) {{
        return value.length
          ? value.map(item => pill(String(item), '')).join('')
          : '<span class="subtle">none</span>';
      }}
      if (value === true || value === false) {{
        return pill(value ? 'true' : 'false', '');
      }}
      if (value && typeof value === 'object') {{
        return `<span class="mono">${{escapeHtml(JSON.stringify(value))}}</span>`;
      }}
      const text = String(value ?? '').trim();
      return text ? `<span>${{escapeHtml(text)}}</span>` : '<span class="subtle">none</span>';
    }}
    function renderScoringContract(item) {{
      const contract = item.scoring_contract && typeof item.scoring_contract === 'object'
        ? item.scoring_contract
        : {{}};
      const checks = contract.checks && typeof contract.checks === 'object' ? contract.checks : {{}};
      const keys = [
        ...scoringContractOrder.filter(key => Object.prototype.hasOwnProperty.call(checks, key)),
        ...Object.keys(checks).filter(key => !scoringContractOrder.includes(key)).sort(),
      ];
      if (!keys.length) {{
        return `<section>
          <div class="detail-label">Promptfoo Scoring Contract</div>
          <div class="scoring-contract"><div class="subtle">No Promptfoo scoring contract recorded for this case.</div></div>
        </section>`;
      }}
      return `<section>
        <div class="detail-label">Promptfoo Scoring Contract</div>
        <div class="scoring-contract">
          ${{keys.map(key => `<div class="scoring-contract-row">
            <div class="scoring-contract-key">${{escapeHtml(key)}}</div>
            <div class="scoring-contract-value">${{scoringContractValueHtml(checks[key])}}</div>
          </div>`).join('')}}
        </div>
      </section>`;
    }}
    function renderPromptRows() {{
      const query = promptSearch.value.trim().toLowerCase();
      const rows = cases.filter(item => {{
        const haystack = [
          item.prompt_number, item.case_id, item.display_case_id, item.agent, (item.dimensions || []).join(' '), item.user_input
        ].join(' ').toLowerCase();
        return (!query || haystack.includes(query))
          && (!promptAgentFilter.value || item.agent === promptAgentFilter.value);
      }});
      document.getElementById('prompt-rows').innerHTML = rows.map(item => {{
        return `
        <article class="prompt-card">
          <div class="case-head" style="border-bottom:0; padding-bottom:0; margin-bottom:8px">
            <div>
              <div class="case-id" title="${{escapeHtml(item.case_id)}}">${{escapeHtml(item.display_case_id || item.case_id)}}</div>
              <div class="case-meta">
                ${{pill(item.agent || 'agent?')}}
                ${{(item.dimensions || []).map(d => pill(d)).join('')}}
              </div>
            </div>
            <button class="prompt-copy" type="button" data-copy-prompt="${{escapeHtml(item.case_id)}}">Copy prompt</button>
          </div>
          <textarea class="copy-source" data-copy-source readonly>${{escapeHtml(item.user_input || '')}}</textarea>
          <div class="text-block">${{escapeHtml(item.user_input || 'No prompt recorded.')}}</div>
        </article>
      `}}).join('');
      for (const button of document.querySelectorAll('#prompt-rows [data-copy-prompt]')) {{
        button.addEventListener('click', () => copyPrompt(button));
      }}
    }}
        function renderRows() {{
      const rows = filteredRunCases();
      document.getElementById('case-rows').innerHTML = rows.map(item => {{
        return `<article class="case-card">
          <div class="case-head">
            <div>
              <div class="case-id" title="${{escapeHtml(item.case_id)}}">${{escapeHtml(item.display_case_id || item.case_id)}}</div>
              <div class="case-meta">
                ${{pill(item.agent || 'agent?')}}
                ${{(item.dimensions || []).map(d => pill(d)).join('')}}
              </div>
            </div>
            <div>${{analysisToggle(item)}}</div>
          </div>
          ${{mergedScoringStatus(item)}}
          <div class="case-body">
            <section>
              <div class="prompt-head">
                <div class="detail-label">Prompt</div>
                <div class="case-actions">
                  <button class="prompt-copy" type="button" data-copy-prompt="${{escapeHtml(item.case_id)}}">Copy prompt</button>
                  <button class="prompt-copy" type="button" data-copy-case="${{escapeHtml(item.case_id)}}" title="Prompt, response, scores, evidence, and next review follow-up.">Copy eval review packet</button>
                </div>
              </div>
              <textarea class="copy-source" data-copy-source readonly>${{escapeHtml(item.user_input || '')}}</textarea>
              <div class="text-block">${{escapeHtml(item.user_input || 'No prompt recorded.')}}</div>
            </section>
            ${{renderScoringContract(item)}}
            <section>
              <div class="detail-label">Latest Response / Result</div>
              <div class="text-block">${{escapeHtml(item.scored_response_text || item.response_text || item.latest_slack_summary || item.promptfoo_reason || 'No response recorded.')}}</div>
            </section>
            <section>
              <div class="detail-label">Human Quality Review</div>
              <div class="score-grid">${{scorePills(item.human_scores)}} ${{item.human_notes ? pill(`notes: ${{item.human_notes}}`, '') : ''}}</div>
            </section>
            <section>
              ${{scoreForm(item)}}
            </section>
          </div>
        </article>`;
      }}).join('');
      for (const form of document.querySelectorAll('.review-form')) {{
        form.addEventListener('submit', event => saveReview(event, form));
        }}
          for (const button of document.querySelectorAll('[data-judge-score]')) {{
            button.addEventListener('click', () => scoreWithOrchestratorJudge(button));
          }}
          for (const button of document.querySelectorAll('[data-copy-prompt]')) {{
            button.addEventListener('click', () => copyPrompt(button));
          }}
          for (const button of document.querySelectorAll('[data-copy-case]')) {{
            button.addEventListener('click', () => copyCaseBundle(button));
          }}
          for (const button of document.querySelectorAll('[data-analysis-toggle]')) {{
            button.addEventListener('click', () => toggleAnalysisExclusion(button));
          }}
        }}
        function renderDatabaseRows() {{
          const query = databaseSearch.value.trim().toLowerCase();
          const rows = latestFirstCases((databaseInventory.rows || []).filter(item => {{
            const haystack = [
              item.prompt_number, item.display_prompt_number, item.case_id, item.display_case_id, item.agent, (item.dimensions || []).join(' '),
              item.latest_run_at, item.latest_run_source, item.latest_run_id,
              item.latest_slack_run_id, item.latest_slack_thread_ts, item.promptfoo_eval_id,
              item.latest_slack_context_policy, item.latest_slack_thread_fetch_status,
              item.latest_slack_cost_profile, item.scoring_status, item.scoring_status_label
            ].join(' ').toLowerCase();
            return (!query || haystack.includes(query))
              && (!databaseAgentFilter.value || item.agent === databaseAgentFilter.value)
              && databaseStateMatches(item);
          }}));
          document.getElementById('database-rows').innerHTML = rows.map(item => {{
            const latestRunAt = item.latest_run_at ? formatDateTime(item.latest_run_at) : '';
            const latestRunSource = item.latest_run_source || 'pending';
            const latestRunId = [
              item.latest_run_id || item.latest_slack_run_id || item.promptfoo_eval_id || '',
              item.latest_slack_thread_ts ? `thread ${{item.latest_slack_thread_ts}}` : '',
            ].filter(Boolean).join(' · ');
            const runScoreParts = [
              item.human_average !== null && item.human_average !== undefined ? `human review ${{scoreValue(item.human_average)}}` : '',
              item.orchestrator_judge_average !== null && item.orchestrator_judge_average !== undefined ? `Orchestrator Review ${{scoreValue(item.orchestrator_judge_average)}}` : '',
              item.scoring_completed_at ? `scored ${{formatDateTime(item.scoring_completed_at)}}` : '',
            ].filter(Boolean);
            const latestRunInfo = [latestRunId, ...runScoreParts].filter(Boolean).join(' · ');
            const machineSummary = item.promptfoo_success === true
              ? `${{pill('pass', 'pass')}}<div class="mono subtle">${{scoreValue(item.promptfoo_score, true)}}</div>`
              : item.promptfoo_success === false
                ? `${{pill('fail', 'fail')}}<div class="mono subtle">${{scoreValue(item.promptfoo_score, true)}}</div>`
                : '<span class="db-empty">pending</span>';
            const analysisState = item.promptfoo_eval_id
              ? `${{item.analysis_excluded ? 'excluded' : 'included'}}${{item.analysis_exclusion_reason ? `: ${{item.analysis_exclusion_reason}}` : ''}}`
              : '';
            const hasHumanScore = item.human_average !== null && item.human_average !== undefined;
            const hasOrchestratorScore = item.orchestrator_judge_average !== null && item.orchestrator_judge_average !== undefined;
            const humanStatus = hasHumanScore ? 'reviewed' : 'unreviewed';
            const humanSummary = hasHumanScore
              ? `${{pill(humanStatus, item.human_safety === 'fail' ? 'fail' : 'pass')}}<div class="mono subtle">${{scoreValue(item.human_average)}}</div>${{item.human_safety ? `<div class="subtle">${{escapeHtml(item.human_safety)}}</div>` : ''}}`
              : '<span class="db-empty">unreviewed</span>';
            const scoringSummary = hasOrchestratorScore
              ? `${{pill('Orchestrator Review complete', 'pass')}}<div class="mono subtle">${{item.orchestrator_judge_created_at ? formatDateTime(item.orchestrator_judge_created_at) : ''}}</div><div class="subtle">${{scoreValue(item.orchestrator_judge_average)}}</div>`
              : '<span class="db-empty">orchestrator review missing</span>';
            const scoreDimensionColumns = scoreDimensions.map(dimension => [
              scoreDimensionCell(item.human_scores || {{}}, dimension),
              scoreDimensionCell(item.orchestrator_judge_scores || {{}}, dimension),
            ].join('')).join('');
            const sourceCount = Number(item.latest_slack_source_count || 0);
            const visibleSourceCount = Number(item.latest_slack_visible_source_count || 0);
            const evidenceParts = [];
            if (Number(item.slack_run_count || 0) > 0) evidenceParts.push(`${{item.slack_run_count}} Slack run${{Number(item.slack_run_count || 0) === 1 ? '' : 's'}}`);
            if (item.latest_slack_thread_fetch_status) evidenceParts.push(`thread ${{item.latest_slack_thread_fetch_status}}`);
            if (sourceCount || visibleSourceCount) evidenceParts.push(`${{visibleSourceCount}}/${{sourceCount}} visible sources`);
            if (item.latest_slack_cost_profile) evidenceParts.push(item.latest_slack_cost_profile);
            if (Number(item.latest_slack_warning_count || 0) > 0) evidenceParts.push(`${{item.latest_slack_warning_count}} warning${{Number(item.latest_slack_warning_count || 0) === 1 ? '' : 's'}}`);
            if (!evidenceParts.length && item.promptfoo_eval_id) evidenceParts.push('machine check imported');
            const evidenceSummary = evidenceParts.join(' · ');
            const detailLinks = [
              item.review_url ? `<a href="${{escapeHtml(item.review_url)}}">Review form</a>` : '',
              item.case_bundle_url ? `<a href="${{escapeHtml(item.case_bundle_url)}}">Case bundle</a>` : '',
            ].filter(Boolean).join(' · ');
            return `<tr>
              <td class="mono">${{escapeHtml(item.display_prompt_number || item.prompt_number || '000')}}</td>
              <td class="mono" title="${{escapeHtml(item.case_id || '')}}">${{escapeHtml(item.display_case_id || item.case_id || '')}}</td>
              <td>${{escapeHtml(item.agent || '')}}</td>
              <td class="mono">${{dbMetric(latestRunAt || item.latest_run_at)}}</td>
              <td>${{pill(latestRunSource, latestRunSource === 'pending' ? 'warn' : 'pass')}}</td>
              <td class="mono">${{dbMetric(latestRunInfo)}}</td>
              <td>${{machineSummary}}</td>
              <td>${{humanSummary}}</td>
              ${{scoreDimensionColumns}}
              <td>${{scoringSummary}}</td>
              <td title="${{escapeHtml(evidenceSummary)}}"><div class="db-cell-text">${{dbMetric(evidenceSummary)}}</div></td>
              <td title="${{escapeHtml(analysisState || (item.promptfoo_eval_id ? 'included' : 'tbd'))}}"><div class="db-cell-text">${{dbMetric(analysisState, item.promptfoo_eval_id ? 'included' : 'tbd')}}</div></td>
              <td>${{detailLinks || '<span class="db-empty">unavailable</span>'}}</td>
            </tr>`;
          }}).join('');
        }}
        async function verifyLocalRefreshEndpoints(result) {{
          const endpoints = Array.isArray(result.refresh_endpoints) ? result.refresh_endpoints : [];
          const localEndpoints = endpoints.filter(endpoint => String(endpoint || '').startsWith('/api/'));
          const outcomes = [];
          for (const endpoint of localEndpoints) {{
            const response = await fetch(endpoint, {{cache: 'no-store'}});
            if (!response.ok) {{
              throw new Error(`saved row but refresh check failed for ${{endpoint}}: HTTP ${{response.status}}`);
            }}
            outcomes.push(endpoint);
          }}
          return outcomes;
        }}
        function databaseFreshnessHint(result) {{
          const tables = result.database_tables || {{}};
          const humanRows = tables.human_eval_reviews?.row_count;
          const traceRows = tables.eval_trace_events?.row_count;
          const parts = [];
          if (humanRows !== undefined) parts.push(`${{Number(humanRows)}} review rows`);
          if (traceRows !== undefined) parts.push(`${{Number(traceRows)}} trace events`);
          return parts.length ? ` DB: ${{parts.join(', ')}}.` : '';
        }}
        async function toggleAnalysisExclusion(button) {{
      const caseId = button.getAttribute('data-case-id') || '';
      const evalId = button.getAttribute('data-eval-id') || '';
      const currentlyExcluded = button.getAttribute('data-analysis-excluded') === 'true';
      const excluded = !currentlyExcluded;
      const original = button.textContent;
      button.disabled = true;
      button.textContent = excluded ? 'Excluding...' : 'Including...';
      try {{
        const response = await fetch('/api/analysis-exclusion', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{
            case_id: caseId,
            eval_id: evalId,
            excluded,
            reason: excluded ? 'manual dashboard exclusion' : '',
          }}),
        }});
        const result = await response.json().catch(() => ({{}}));
        if (!response.ok) throw new Error(result.error || `HTTP ${{response.status}}`);
            const refreshed = await verifyLocalRefreshEndpoints(result);
            const savedState = result.post_save_state?.case || {{}};
            button.textContent = savedState.analysis_excluded
              ? `Excluded; verified ${{refreshed.length}} local views. Reloading...`
              : `Included; verified ${{refreshed.length}} local views. Reloading...`;
            setTimeout(() => window.location.reload(), 250);
      }} catch (error) {{
        button.disabled = false;
        button.textContent = `Failed: ${{error.message}}`;
        setTimeout(() => {{ button.textContent = original; }}, 1800);
      }}
    }}
    async function saveReview(event, form) {{
      event.preventDefault();
      const status = form.querySelector('[data-review-status]');
      const caseId = form.getAttribute('data-case-id');
      const item = cases.find(candidate => candidate.case_id === caseId) || {{}};
      if (!hasRecordedResponse(item)) {{
        status.textContent = 'Save disabled until this case has a recorded response.';
        return;
      }}
      const scores = {{}};
      for (const select of form.querySelectorAll('[data-score]')) {{
        const dimension = select.getAttribute('data-score');
        if (select.value === '') {{
          status.textContent = `Choose a score for ${{dimension}} before saving.`;
          return;
        }}
        scores[dimension] = Number(select.value);
      }}
      const safety = form.querySelector('[data-safety]').value;
      if (!safety) {{
        status.textContent = 'Choose safety pass or fail before saving.';
        return;
      }}
      const reviewTarget = item.review_target || {{}};
      const payload = {{
        case_id: caseId,
        run_id: reviewTarget.run_id || item.latest_slack_run_id || item.promptfoo_eval_id || '',
        agent: reviewTarget.agent || item.agent || '',
        slack_thread_ts: reviewTarget.slack_thread_ts || '',
        safety,
        notes: form.querySelector('[data-notes]').value,
        scores,
      }};
      status.textContent = 'Updating review...';
      try {{
        const response = await fetch('/api/human-review', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify(payload),
        }});
        const result = await response.json().catch(() => ({{}}));
        if (!response.ok) throw new Error(result.error || `HTTP ${{response.status}}`);
        const savedTarget = result.review_target || {{}};
        const targetLabel = savedTarget.target_type ? `${{savedTarget.target_type}} target` : 'current target';
        const followUp = result.post_save_state?.follow_up || {{}};
        const nextGap = followUp.still_open && followUp.follow_up_summary
          ? ` Next: ${{followUp.follow_up_summary}}.`
          : '';
            const refreshed = await verifyLocalRefreshEndpoints(result);
            const dbHint = databaseFreshnessHint(result);
            status.textContent = `Updated ${{targetLabel}} ${{result.average_score}}/5.${{nextGap}} Verified ${{refreshed.length}} local views.${{dbHint}} Reloading dashboard...`;
            setTimeout(() => window.location.reload(), 500);
      }} catch (error) {{
        status.textContent = `Update failed: ${{error.message}}. Start the dashboard server to save scores.`;
      }}
    }}
    async function scoreWithOrchestratorJudge(button) {{
      const caseId = button.getAttribute('data-judge-score') || '';
      const item = cases.find(candidate => candidate.case_id === caseId) || {{}};
      const status = button.closest('form')?.querySelector('[data-review-status]');
      const original = button.textContent;
      if (!canJudgeScore(item)) {{
        if (status) status.textContent = orchestratorJudge.enabled
          ? 'Orchestrator Review needs a saved #evals Slack response.'
          : `Orchestrator Review disabled; set ${{orchestratorJudge.env_flag || 'KEYSTONE_EVAL_LLM_JUDGE'}}=true and restart.`;
        return;
      }}
      const reviewTarget = item.review_target || {{}};
      button.disabled = true;
      button.textContent = 'Scoring...';
      if (status) status.textContent = 'Orchestrator Review scoring this #evals output...';
      try {{
        const response = await fetch('/api/orchestrator-judge-score', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{
            case_id: caseId,
            run_id: reviewTarget.run_id || item.latest_slack_run_id || '',
            slack_thread_ts: reviewTarget.slack_thread_ts || item.latest_slack_thread_ts || '',
          }}),
        }});
        const result = await response.json().catch(() => ({{}}));
        if (!response.ok) throw new Error(result.error || `HTTP ${{response.status}}`);
        const refreshed = await verifyLocalRefreshEndpoints(result);
        const dbHint = databaseFreshnessHint(result);
        if (status) status.textContent = `Orchestrator Review saved ${{scoreValue(result.average_score)}}/5. Verified ${{refreshed.length}} local views.${{dbHint}} Reloading dashboard...`;
        setTimeout(() => window.location.reload(), 500);
      }} catch (error) {{
        button.disabled = false;
        button.textContent = original;
        if (status) status.textContent = `Orchestrator Review failed: ${{error.message}}`;
      }}
    }}
    renderAgentScoreTable('machine');
    renderWorkflowVisualization();
    renderOverviewLatestRun();
    renderDataQualityGates();
    renderCoverageBars();
    renderBars('dimension-bars', data.summary.dimensions, 8);
    renderEvalRuns();
    renderLatestSavedRuns();
    renderFollowUpQueue();
    renderRunLedger();
    renderAnalysis();
    renderTraceSummary();
        for (const card of document.querySelectorAll('[data-drilldown]')) {{
          card.addEventListener('click', () => {{
            setActiveView('overview', card.getAttribute('data-drilldown'));
            renderAgentScoreTable(card.getAttribute('data-drilldown'), true);
          }});
        }}
        for (const button of document.querySelectorAll('[data-score-view]')) {{
          button.addEventListener('click', () => {{
            setActiveView('overview', button.getAttribute('data-score-view'));
            renderAgentScoreTable(button.getAttribute('data-score-view'), true);
          }});
        }}
        for (const button of document.querySelectorAll('[data-view]')) {{
          button.addEventListener('click', () => {{
            const scoreNav = button.getAttribute('data-score-nav') || '';
            setActiveView(button.getAttribute('data-view'), scoreNav);
            if (scoreNav) {{
              renderAgentScoreTable(scoreNav, true);
            }}
          }});
        }}
    search.addEventListener('input', () => {{
      renderLatestSavedRuns();
      renderRows();
    }});
    agentFilter.addEventListener('change', () => {{
      renderLatestSavedRuns();
      renderRows();
    }});
    stateFilter.addEventListener('change', () => {{
      renderLatestSavedRuns();
      renderRows();
    }});
        promptSearch.addEventListener('input', renderPromptRows);
        promptAgentFilter.addEventListener('change', renderPromptRows);
        databaseSearch.addEventListener('input', renderDatabaseRows);
        databaseAgentFilter.addEventListener('change', renderDatabaseRows);
        databaseStateFilter.addEventListener('change', renderDatabaseRows);
        analysisAgentTrendFilter.addEventListener('change', renderAnalysisRunChart);
        analysisCaseFilter.addEventListener('change', renderAnalysisCaseTrend);
    if (params.get('case')) {{
      setActiveView('runs');
    }}
        renderPromptRows();
        renderLatestSavedRuns();
        renderRows();
        renderDatabaseRows();
  </script>
</body>
</html>
"""


def _review_html(payload: dict[str, Any]) -> str:
    data_json = _json_script_payload(payload)
    case = payload["case"]
    title = "Keystone Eval Review"
    review_ready = _case_has_recorded_response(case)
    disabled = "" if review_ready else " disabled"
    prompt_recorded = bool(str(case.get("user_input") or "").strip())
    response_recorded = bool(
        str(
            case.get("scored_response_text")
            or case.get("response_text")
            or case.get("latest_slack_summary")
            or ""
        ).strip()
    )
    judge_ready = (
        bool((payload.get("orchestrator_judge") or {}).get("enabled"))
        and str((case.get("review_target") or {}).get("target_type") or "") == "slack"
        and bool(str(case.get("latest_slack_summary") or case.get("scored_response_text") or "").strip())
    )
    judge_disabled = "" if judge_ready else " disabled"
    if judge_ready:
        judge_note = "Scores this saved #evals Slack output with Orchestrator Review."
    elif not response_recorded:
        judge_note = "Orchestrator Review is disabled until this case has a saved #evals Slack response."
    elif str((case.get("review_target") or {}).get("target_type") or "") != "slack":
        judge_note = "Orchestrator Review is available only for saved #evals Slack runs."
    elif not bool((payload.get("orchestrator_judge") or {}).get("enabled")):
        judge_note = f"Orchestrator Review is disabled; set {JUDGE_ENV_FLAG}=true and restart the dashboard."
    else:
        judge_note = "Orchestrator Review is disabled for this case."
    missing_items: list[str] = []
    if not prompt_recorded:
        missing_items.append("No prompt is recorded for this case ID.")
    if not response_recorded:
        missing_items.append("No Promptfoo result or saved #evals Slack response is recorded.")
    missing_html = "".join(
        f"<li>{html.escape(item)}</li>"
        for item in missing_items
    )
    disabled_note = ""
    if not review_ready:
        disabled_note = (
            '<div class="disabled-banner" role="status">'
            "<strong>Review controls are disabled.</strong> "
            "This page can score only a case with a recorded Promptfoo result or a saved #evals Slack response."
            f"{'<ul>' + missing_html + '</ul>' if missing_html else ''}"
            "</div>"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f8;
      --panel: #ffffff;
      --ink: #1b1f23;
      --muted: #687076;
      --line: #d9dee3;
      --good: #287a47;
      --bad: #b42318;
      --warn: #b54708;
      --soft: #f1f4f6;
      --soft-warn: #f2f4f5;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{ max-width: 900px; margin: 0 auto; padding: 24px 18px 36px; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}
    h1 {{ margin: 0 0 6px; font-size: 24px; letter-spacing: 0; }}
    h2 {{ font-size: 15px; margin: 18px 0 8px; }}
    .subtle {{ color: var(--muted); }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }}
    .pill {{ display: inline-flex; align-items: center; width: fit-content; max-width: 100%; min-height: 22px; padding: 2px 8px; border-radius: 999px; font-size: 12px; line-height: 1.2; border: 1px solid var(--line); margin: 0 4px 4px 0; white-space: nowrap; }}
    .warn {{ color: var(--warn); border-color: #d6d9dc; background: var(--soft-warn); }}
    .text-block {{
      background: var(--soft);
      border: 1px solid #dde5ea;
      border-radius: 6px;
      padding: 9px 10px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}
    .score-controls {{
      display: grid;
      grid-template-columns: repeat(3, minmax(160px, 1fr));
      gap: 8px;
      margin-bottom: 10px;
    }}
    label {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      margin-bottom: 3px;
    }}
    select, textarea {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      width: 100%;
      font: inherit;
      background: #fff;
      color: var(--ink);
    }}
    textarea {{ min-height: 100px; resize: vertical; }}
    button {{
      border: 1px solid #9a927f;
      border-radius: 6px;
      background: #f7f9fa;
      color: var(--ink);
      min-height: 34px;
      padding: 7px 11px;
      font: inherit;
      cursor: pointer;
    }}
    select:disabled,
    textarea:disabled,
    button:disabled {{
      opacity: 1;
      cursor: not-allowed;
      background: #eef2f5;
      color: #8a949e;
      border-color: #cfd7df;
    }}
    .disabled-banner {{
      border: 1px solid #d6d9dc;
      background: #f2f4f5;
      color: #4d5660;
      border-radius: 6px;
      padding: 10px 12px;
      margin: 10px 0 12px;
    }}
    .disabled-banner ul {{
      margin: 6px 0 0 18px;
      padding: 0;
    }}
    .review-summary {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      background: #fbfcfd;
      margin: 10px 0 14px;
    }}
    .review-summary-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 8px;
    }}
    .score-chip-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(130px, 1fr));
      gap: 6px;
      margin-top: 8px;
    }}
    .form-actions {{ display: flex; align-items: center; gap: 10px; margin-top: 10px; flex-wrap: wrap; }}
    @media (max-width: 760px) {{
      .score-controls {{ grid-template-columns: 1fr; }}
      .score-chip-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <div class="panel">
      <h1>{title}</h1>
      <div class="subtle">Standalone intake for one eval case. Scores are 0-5; safety is pass/fail.</div>
      <h2>Case</h2>
      <div class="mono">{html.escape(str(case.get("case_id") or ""))}</div>
      <div style="margin-top:8px">
        <span class="pill">{html.escape(str(case.get("agent") or "agent?"))}</span>
        {"".join(f'<span class="pill">{html.escape(str(dimension))}</span>' for dimension in case.get("dimensions") or [])}
      </div>
      <h2>Prompt</h2>
      <div class="text-block">{html.escape(str(case.get("user_input") or "No prompt recorded."))}</div>
      <h2>Latest Response / Result</h2>
      <div class="text-block">{html.escape(str(case.get("scored_response_text") or case.get("response_text") or "No response recorded."))}</div>
      <h2>Orchestrator Review</h2>
      <div id="orchestrator-review-summary" class="review-summary"></div>
      <h2>Human Review</h2>
      <div class="subtle">Manual human review stays separate from Orchestrator Review. Use this form to edit, override, and save a human scorecard.</div>
      <form id="review-form" aria-disabled="{str(not review_ready).lower()}">
        {disabled_note}
        <div id="score-controls" class="score-controls"></div>
        <label for="notes">Human notes</label>
        <textarea id="notes" aria-label="Human notes" placeholder="Saved to the Human notes column"{disabled}>{html.escape(str(case.get("human_notes") or ""))}</textarea>
        <div class="subtle">Saved to the database Human notes column with this scorecard.</div>
        <div class="form-actions">
          <button type="submit"{disabled}>Update review</button>
          <button type="button" id="judge-score"{judge_disabled}>Score with Orchestrator Review</button>
          <span id="status" class="subtle"></span>
        </div>
        <div class="subtle">{html.escape(judge_note)}</div>
      </form>
    </div>
  </main>
  <script id="eval-data" type="application/json">{data_json}</script>
  <script>
    const data = JSON.parse(document.getElementById('eval-data').textContent);
    const item = data.case || {{}};
    const scoreDimensions = data.score_dimensions || [];
    const orchestratorJudge = data.orchestrator_judge || {{}};
    const reviewReady = Boolean(String(item.scored_response_text || item.response_text || item.latest_slack_summary || '').trim());
    const judgeReady = Boolean(
      orchestratorJudge.enabled &&
      (item.review_target || {{}}).target_type === 'slack' &&
      String(item.latest_slack_summary || item.scored_response_text || '').trim()
    );
    const controls = document.getElementById('score-controls');
    function escapeHtml(value) {{
      return String(value ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
    }}
    function scoreValue(value) {{
      if (value === null || value === undefined || value === '') return 'tbd';
      const number = Number(value);
      if (Number.isNaN(number)) return 'tbd';
      return `${{number.toFixed(1).replace(/\\.0$/, '')}}/5`;
    }}
        function scoreOptions(selected) {{
          if (!reviewReady || selected === null || selected === undefined || selected === '') {{
            return '<option value="" selected>tbd</option>' + [0, 1, 2, 3, 4, 5].map(value => `<option value="${{value}}">${{value}}</option>`).join('');
          }}
      return [0, 1, 2, 3, 4, 5].map(value => {{
        const isSelected = Number(selected) === value ? ' selected' : '';
        return `<option value="${{value}}"${{isSelected}}>${{value}}</option>`;
      }}).join('');
    }}
    controls.innerHTML = scoreDimensions.map(dimension => `
      <div>
        <label>${{escapeHtml(dimension)}}</label>
        <select data-score="${{escapeHtml(dimension)}}"${{reviewReady ? '' : ' disabled'}}>${{scoreOptions(item.human_scores?.[dimension])}}</select>
      </div>
    `).join('') + `
      <div>
        <label>safety</label>
        <select data-safety${{reviewReady ? '' : ' disabled'}}>
          ${{!reviewReady || !item.human_safety ? '<option value="" selected>tbd</option>' : '<option value="">tbd</option>'}}
          <option value="pass"${{reviewReady && item.human_safety === 'pass' ? ' selected' : ''}}>pass</option>
          <option value="fail"${{reviewReady && item.human_safety === 'fail' ? ' selected' : ''}}>fail</option>
            </select>
          </div>
        `;
        function renderOrchestratorReviewSummary() {{
          const target = document.getElementById('orchestrator-review-summary');
          const hasScore = item.orchestrator_judge_average !== null && item.orchestrator_judge_average !== undefined;
          if (!target) return;
          if (!hasScore) {{
            target.innerHTML = '<div class="subtle">No Orchestrator Review scorecard is saved for this case/run yet.</div>';
            return;
          }}
          const scores = item.orchestrator_judge_scores || {{}};
          const rationales = item.orchestrator_judge_dimension_rationales || {{}};
          const chips = scoreDimensions.map(dimension => `
            <div class="text-block">
              <strong>${{escapeHtml(dimension)}} ${{scores[dimension] ?? 'tbd'}}</strong>
              <div class="subtle">${{escapeHtml(rationales[dimension] || 'No metric rationale saved.')}}</div>
            </div>
          `).join('');
          target.innerHTML = `
            <div class="review-summary-head">
              <div>
                <strong>Orchestrator Review ${{scoreValue(item.orchestrator_judge_average)}}</strong>
                <span class="subtle">${{item.orchestrator_judge_safety ? ` · safety ${{escapeHtml(item.orchestrator_judge_safety)}}` : ''}}${{item.orchestrator_judge_created_at ? ` · ${{escapeHtml(item.orchestrator_judge_created_at)}}` : ''}}</span>
              </div>
              <button type="button" id="copy-orchestrator-review">Use as human draft</button>
            </div>
            <div class="score-chip-grid">${{chips}}</div>
            <div class="text-block" style="margin-top:8px">${{escapeHtml(item.orchestrator_judge_run_comment || item.orchestrator_judge_notes || 'No Orchestrator Review comment saved.')}}</div>
          `;
          const copyButton = document.getElementById('copy-orchestrator-review');
          if (copyButton) {{
            copyButton.addEventListener('click', () => {{
              for (const select of document.querySelectorAll('[data-score]')) {{
                const dimension = select.getAttribute('data-score');
                if (scores[dimension] !== null && scores[dimension] !== undefined) {{
                  select.value = String(scores[dimension]);
                }}
              }}
              const safety = document.querySelector('[data-safety]');
              if (safety && item.orchestrator_judge_safety) safety.value = item.orchestrator_judge_safety;
              const notes = document.getElementById('notes');
              const orchestratorComment = item.orchestrator_judge_run_comment || item.orchestrator_judge_notes || '';
              if (notes && orchestratorComment) notes.value = orchestratorComment;
              const status = document.getElementById('status');
              if (status) status.textContent = 'Copied Orchestrator Review into the human form. Save manually to create a human scorecard.';
            }});
          }}
        }}
        renderOrchestratorReviewSummary();
        async function verifyLocalRefreshEndpoints(result) {{
          const endpoints = Array.isArray(result.refresh_endpoints) ? result.refresh_endpoints : [];
          const localEndpoints = endpoints.filter(endpoint => String(endpoint || '').startsWith('/api/'));
          const outcomes = [];
          for (const endpoint of localEndpoints) {{
            const response = await fetch(endpoint, {{cache: 'no-store'}});
            if (!response.ok) {{
              throw new Error(`saved row but refresh check failed for ${{endpoint}}: HTTP ${{response.status}}`);
            }}
            outcomes.push(endpoint);
          }}
          return outcomes;
        }}
        function databaseFreshnessHint(result) {{
          const tables = result.database_tables || {{}};
          const humanRows = tables.human_eval_reviews?.row_count;
          const traceRows = tables.eval_trace_events?.row_count;
          const parts = [];
          if (humanRows !== undefined) parts.push(`${{Number(humanRows)}} review rows`);
          if (traceRows !== undefined) parts.push(`${{Number(traceRows)}} trace events`);
          return parts.length ? ` DB: ${{parts.join(', ')}}.` : '';
        }}
        document.getElementById('review-form').addEventListener('submit', async event => {{
      event.preventDefault();
      const status = document.getElementById('status');
      if (!reviewReady) {{
        status.textContent = 'Update disabled until this case has a recorded response.';
        return;
      }}
      const scores = {{}};
      for (const select of document.querySelectorAll('[data-score]')) {{
        const dimension = select.getAttribute('data-score');
        if (select.value === '') {{
          status.textContent = `Choose a score for ${{dimension}} before saving.`;
          return;
        }}
        scores[dimension] = Number(select.value);
      }}
      const safety = document.querySelector('[data-safety]').value;
      if (!safety) {{
        status.textContent = 'Choose safety pass or fail before saving.';
        return;
      }}
      const reviewTarget = item.review_target || {{}};
      const payload = {{
        case_id: item.case_id || '',
        run_id: reviewTarget.run_id || item.latest_slack_run_id || item.promptfoo_eval_id || '',
        agent: reviewTarget.agent || item.agent || '',
        slack_thread_ts: reviewTarget.slack_thread_ts || '',
        safety,
        notes: document.getElementById('notes').value,
        scores,
      }};
      status.textContent = 'Updating review...';
      try {{
        const response = await fetch('/api/human-review', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify(payload),
        }});
        const result = await response.json().catch(() => ({{}}));
            if (!response.ok) throw new Error(result.error || `HTTP ${{response.status}}`);
            const savedTarget = result.review_target || {{}};
            const targetLabel = savedTarget.target_type ? `${{savedTarget.target_type}} target` : 'current target';
            const refreshed = await verifyLocalRefreshEndpoints(result);
            const dbHint = databaseFreshnessHint(result);
            status.textContent = `Updated ${{targetLabel}} average ${{result.average_score}}/5. Verified ${{refreshed.length}} local dashboard views.${{dbHint}} Reloading review page...`;
            setTimeout(() => window.location.reload(), 500);
          }} catch (error) {{
        status.textContent = `Update failed: ${{error.message}}`;
      }}
    }});
    document.getElementById('judge-score').addEventListener('click', async () => {{
      const status = document.getElementById('status');
      const button = document.getElementById('judge-score');
      if (!judgeReady) {{
        status.textContent = orchestratorJudge.enabled
          ? 'Orchestrator Review needs a saved #evals Slack response.'
          : `Orchestrator Review disabled; set ${{orchestratorJudge.env_flag || 'KEYSTONE_EVAL_LLM_JUDGE'}}=true and restart.`;
        return;
      }}
      const reviewTarget = item.review_target || {{}};
      button.disabled = true;
      button.textContent = 'Scoring...';
      status.textContent = 'Orchestrator Review scoring this #evals output...';
      try {{
        const response = await fetch('/api/orchestrator-judge-score', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{
            case_id: item.case_id || '',
            run_id: reviewTarget.run_id || item.latest_slack_run_id || '',
            slack_thread_ts: reviewTarget.slack_thread_ts || item.latest_slack_thread_ts || '',
          }}),
        }});
        const result = await response.json().catch(() => ({{}}));
        if (!response.ok) throw new Error(result.error || `HTTP ${{response.status}}`);
        const refreshed = await verifyLocalRefreshEndpoints(result);
        const dbHint = databaseFreshnessHint(result);
        status.textContent = `Orchestrator Review saved ${{result.average_score}}/5. Verified ${{refreshed.length}} local dashboard views.${{dbHint}} Reloading review page...`;
        setTimeout(() => window.location.reload(), 500);
      }} catch (error) {{
        button.disabled = false;
        button.textContent = 'Score with Orchestrator Review';
        status.textContent = `Orchestrator Review failed: ${{error.message}}`;
      }}
    }});
  </script>
</body>
</html>
"""


def _json_script_payload(payload: dict[str, Any]) -> str:
    return (
        json.dumps(payload, ensure_ascii=True, sort_keys=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _kpi(label: str, value: Any, hint: str = "", drilldown: str = "") -> str:
    data_attr = f' data-drilldown="{html.escape(drilldown)}"' if drilldown else ""
    return (
        f'<button class="kpi" type="button"{data_attr}>'
        f'<div class="label">{_label_with_info(label)}</div>'
        f'<div class="value">{html.escape(str(value))}</div>'
        f'<div class="hint">{html.escape(hint)}</div>'
        "</button>"
    )


def _label_with_info(label: str, help_text: str = "") -> str:
    text = help_text or _label_help_text(label)
    info = (
        f'<span class="info-dot" tabindex="0" data-tooltip="{html.escape(text)}" '
        f'aria-label="{html.escape(text)}">i</span>'
        if text
        else ""
    )
    return f'<span class="label-with-info"><span>{html.escape(label)}</span>{info}</span>'


def _label_help_text(label: str) -> str:
    return {
        "Agent Coverage": "How many agent roles already have the target seed-prompt count. This is coverage, not score quality.",
        "Prompt Coverage": "Prompt inventory by agent. Use it to find agents below the seed target before starting live #evals runs.",
        "Machine Pass Rate": "Share of imported Promptfoo checks that passed. Pending cases are not counted until a machine-check row exists.",
        "Machine Avg / 5": "Average Promptfoo assertion score for scored cases only, normalized to the dashboard 0-5 scale.",
        "Slack Runs": "Saved #evals run rows. The hint shows distinct linked cases and retry-heavy cases separately.",
        "Scoring Health": "Whether the local scoring endpoint and launch manager are reachable from this machine.",
        "Data Quality": "Preflight gates completed before paid evals: prompt text, run IDs, timestamps, responses, evidence, reviews, traces, and ledger rows.",
        "Slack Evidence": "Saved Slack runs with thread evidence. Warnings mean the run is present but missing some audit metadata.",
        "Human Avg / 5": "Average score from submitted human scorecards. Orchestrator Review scorecards are tracked separately.",
        "Orchestrator Review Avg / 5": "Average score from saved Orchestrator Review scorecards, not the score for the latest imported machine run.",
        "Total Cases": "Total local eval case rows: committed seed prompts plus any ad hoc cases created during Slack testing.",
        "Slack Eval Conversation Flow": "End-to-end #evals workflow status from prompt library to saved Slack run, response, scorecard, and analysis row.",
        "Prompt library": "Committed seed prompts available to start #evals threads. This is the denominator for most workflow counts.",
        "Slack runs": "Cases with a saved #evals thread run. These should appear after the agent responds in Slack.",
        "Responses": "Cases with saved response text from Slack or Promptfoo. These are eligible for review scoring.",
        "Scorecards": "Manual human and Orchestrator Review scorecards saved from the review flow, including scores, safety, and notes.",
        "Analysis": "Cases currently counted in analysis views after excluding duplicates or known problem runs.",
        "Source checks": "Cases tagged for retrieval or evidence quality, useful for auditing source-backed answers.",
        "API Spend Readiness Gates": "Local readiness checks that should pass before paying for live Slack/OpenAI eval runs.",
        "Agent Scores": "Per-agent score table. Machine mode uses Promptfoo imports; review modes use submitted human and Orchestrator Review scorecards.",
        "Promptfoo Assertion Avg / 5 by Agent": "Average Promptfoo assertion score per agent. It is a machine-check signal, not a human quality judgment.",
        "Slack Human Review Avg / 5 by Agent": "Average submitted human-review score per agent from Slack-linked scorecards.",
        "Evaluation Dimensions": "Prompt tags showing which behaviors the eval set exercises, such as retrieval, safety, synthesis, and output format.",
        "Prompt Library": "Searchable list of committed eval prompts that can be copied into #evals as root messages.",
        "Runs & Scoring": "Case-level workspace for latest run state, machine checks, review scorecards, evidence, and analysis inclusion.",
        "Eval Follow-up Queue": "Prioritized current cases that need a run, evidence fix, machine import, review scorecard, or analysis decision.",
        "Eval Run Ledger": "Newest-first event feed across Slack runs, Promptfoo imports, review scorecards, and trace events.",
        "Promptfoo Run Analysis": "Trend and comparison views built from imported Promptfoo rows, manual human reviews, and Orchestrator Review scorecards.",
        "Average Score Across Time": "Daily average machine, manual human, and Orchestrator Review scores. Multiple same-day rows are averaged for trend readability.",
        "Single Prompt Trend": "Score history for one selected prompt, useful for seeing whether a specific fix helped.",
        "Prompt Average Scores by Agent": "Prompt-level machine averages grouped by agent so weak prompts are easier to target.",
        "Agent Stability": "How consistent each agent is across scored cases: pass rate, average, range, and uneven results.",
        "Failure Clusters by Dimension": "Failed machine checks grouped by prompt tags to show which behavior areas need fixes.",
        "Case Changes Across Runs": "Latest score movement for each case compared with its previous imported run.",
        "Trace Explorer": "OpenAI-style run timeline plus Keystone eval joins, review scores, diagnostics, and privacy state from sanitized local trace summaries.",
        "Trace Health": "Group-level trace coverage, source split, diagnostics, and privacy posture for saved run summaries.",
        "Trace Diagnostics": "Chartable run-diagnostic categories from sanitized trace metadata, such as missing model metadata, retrieval gaps, retries, extraction issues, approval gates, and tool failures.",
        "Current Run Trace": "Newest run-level trace reduced to case/run joins, route, review scores, step timeline, and cleanup signals.",
        "Trace Event Log": "Recent sanitized workflow events shown as trace steps. Copy exposes capped metadata for local review.",
        "Storage & Instrumentation": "Processor readiness, database freshness, retained fields, dropped sensitive fields, and implementation notes.",
        "Trace Processor Readiness": "Sanitized trace-capture readiness for future API evals; disabled is normal for current no-API runs.",
        "What We Store Locally": "Trace and workflow fields kept for joins/timing, plus sensitive fields intentionally dropped.",
        "Implementation Contract": "Environment settings and integration rules needed before sanitized trace capture is enabled.",
        "Eval Case Database": "Searchable case table combining seed prompts, newest runs first, review status, evidence, and notes.",
    }.get(label, "")


def _score_out_of_five(value: Any, *, machine: bool = False) -> str:
    if value is None or value == "":
        return "-"
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "-"
    if machine:
        score *= 5
    return f"{score:.1f}/5"


def _split_dimensions(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render the local Promptfoo eval dashboard.")
    parser.add_argument("--database-path", default=str(DEFAULT_EVAL_DB))
    parser.add_argument("--output", default=str(DEFAULT_DASHBOARD_PATH))
    parser.add_argument("--limit", type=int, default=500)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    output = render_dashboard(
        database_path=args.database_path,
        output_path=args.output,
        limit=args.limit,
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
