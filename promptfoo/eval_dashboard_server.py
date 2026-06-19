"""Local HTTP server for the Promptfoo eval dashboard and human scoring form."""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

try:  # pragma: no cover - platform dependent.
    import resource
except ImportError:  # pragma: no cover - Windows fallback for local tooling.
    resource = None  # type: ignore[assignment]

from promptfoo.eval_dashboard import (
    CASE_DETAIL_LOOKUP_LIMIT,
    DEFAULT_DASHBOARD_PATH,
    dashboard_case_export_csv,
    dashboard_case_export_rows,
    dashboard_case_review_bundle,
    dashboard_case_review_bundle_text,
    dashboard_payload,
    render_dashboard,
    render_review_form,
)
from promptfoo.eval_database import (
    DEFAULT_EVAL_DB,
    database_table_summaries,
    get_eval_trace_event,
    record_eval_trace_event,
    resolve_human_review_target,
    set_promptfoo_analysis_exclusion,
    validate_human_review_target,
)
from promptfoo.eval_urls import eval_dashboard_case_url, eval_dashboard_url, eval_review_case_url
from promptfoo.human_review import (
    SCORE_DIMENSIONS,
    HumanEvalReview,
    save_human_review,
)

NO_STORE_CACHE_CONTROL = "no-store, max-age=0"
SERVER_RENDER_CACHE_TTL_SECONDS = 1.0
SERVER_RENDER_LOCK_TIMEOUT_SECONDS = 2.0
SERVER_MIN_NOFILE_LIMIT = 2048
_NOFILE_LIMIT_STATUS: dict[str, Any] = {
    "available": False,
    "soft": 0,
    "hard": 0,
    "raised": False,
    "error": "",
}


def _raise_nofile_limit(min_soft_limit: int = SERVER_MIN_NOFILE_LIMIT) -> dict[str, Any]:
    """Raise this local dashboard process' file descriptor ceiling when allowed."""

    if resource is None:
        return {"available": False, "soft": 0, "hard": 0, "raised": False, "error": ""}
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = min(max(int(soft), int(min_soft_limit)), int(hard))
        raised = target > int(soft)
        if raised:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        return {
            "available": True,
            "soft": int(soft),
            "hard": int(hard),
            "raised": raised,
            "error": "",
        }
    except (OSError, ValueError) as exc:
        return {"available": True, "soft": 0, "hard": 0, "raised": False, "error": str(exc)}


def _database_table_summaries_safe(database_path: str | Path) -> dict[str, Any]:
    try:
        return {
            "ok": True,
            "error": "",
            "tables": database_table_summaries(database_path),
        }
    except (OSError, sqlite3.Error) as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "tables": {},
        }


def _refresh_endpoints(case_id: str = "") -> list[str]:
    endpoints = [
        "/api/status?refresh=1",
        "/api/eval-cases",
        "/api/follow-up-queue",
        "/api/data-quality",
        "/api/eval-run-ledger",
        "/api/trace-diagnostics",
    ]
    normalized = str(case_id or "").strip()
    if normalized:
        endpoints.append(f"/api/eval-case-bundle?case={quote(normalized, safe='')}")
    return endpoints


def _database_cache_signature(database_path: Path) -> tuple[tuple[str, int, int], ...]:
    db_path = Path(database_path)
    signature: list[tuple[str, int, int]] = []
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        try:
            stat = path.stat()
        except OSError:
            signature.append((path.name, 0, 0))
            continue
        signature.append((path.name, int(stat.st_mtime_ns), int(stat.st_size)))
    return tuple(signature)


def _truthy_query_flag(query: str, key: str) -> bool:
    values = parse_qs(query or "").get(key, [])
    return any(str(value).strip().lower() in {"1", "true", "yes", "on"} for value in values)


def save_human_review_payload(
    payload: dict[str, Any],
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
    require_recorded_response: bool = False,
) -> dict[str, Any]:
    """Validate and persist one dashboard human-review form payload."""

    case_id = str(payload.get("case_id") or "").strip()
    if not case_id:
        raise ValueError("case_id is required")
    input_case_id = case_id
    case_id = _canonical_dashboard_case_id(case_id, database_path=database_path)
    if require_recorded_response and not _case_has_recorded_response(
        case_id,
        database_path=database_path,
    ):
        raise ValueError("human review requires a recorded Promptfoo or Slack response")
    raw_scores = payload.get("scores") if isinstance(payload.get("scores"), dict) else {}
    scores: dict[str, float] = {}
    for dimension in SCORE_DIMENSIONS:
        if dimension not in raw_scores or str(raw_scores.get(dimension) or "").strip() == "":
            raise ValueError(f"{dimension} score is required")
        scores[dimension] = _coerce_score(raw_scores[dimension], dimension)
    safety = str(payload.get("safety") or "").strip().lower()
    if safety not in {"pass", "fail"}:
        raise ValueError("safety is required and must be pass or fail")
    review = HumanEvalReview(
        case_id=case_id,
        run_id=str(payload.get("run_id") or "").strip(),
        agent=str(payload.get("agent") or "").strip(),
        reviewer=str(payload.get("reviewer") or "anup").strip() or "anup",
        scores=scores,
        safety=safety,
        notes=str(payload.get("notes") or "").strip(),
        slack_channel_id=str(payload.get("slack_channel_id") or "C0BA17Y9C01").strip(),
        slack_channel_name=str(payload.get("slack_channel_name") or "evals").strip(),
        slack_thread_ts=str(payload.get("slack_thread_ts") or "").strip(),
        raw_text=json.dumps(payload, ensure_ascii=True, sort_keys=True),
        storage_mode=str(payload.get("storage_mode") or "local_review").strip() or "local_review",
    )
    review = resolve_human_review_target(
        review,
        database_path=database_path,
        require_recorded_response=require_recorded_response,
    )
    review_target = validate_human_review_target(
        review,
        database_path=database_path,
        require_recorded_response=require_recorded_response,
    )
    row_id = save_human_review(review, database_path=database_path)
    record_eval_trace_event(
        event_type="human_review_saved",
        trace_id=f"human_review:{row_id}",
        span_id=f"human_review_row:{row_id}",
        name="human_eval_review_saved",
        group_id=case_id,
        metadata={
            "schema": "keystone.eval_manual_trace.v1",
            "case_id": case_id,
            "input_case_id": input_case_id,
            "run_id": review.run_id,
            "agent": review.agent,
            "source": "human_review",
            "average_score": review.average_score,
            "safety": review.safety,
            "score_dimension_count": len(review.scores),
            "slack_channel_name": review.slack_channel_name,
            "has_slack_thread_ts": bool(review.slack_thread_ts),
            "row_id": row_id,
            "review_target": review_target,
            "target_type": review_target.get("target_type") or "",
        },
        database_path=database_path,
    )
    result = review.to_dict()
    result["id"] = row_id
    result["input_case_id"] = input_case_id
    result["dashboard_case_url"] = eval_dashboard_case_url(case_id)
    result["review_case_url"] = eval_review_case_url(case_id)
    result["review_target"] = review_target
    result["post_save_state"] = _human_review_post_save_state(
        case_id,
        row_id=row_id,
        database_path=database_path,
    )
    result["database_tables"] = database_table_summaries(database_path)
    result["refresh_targets"] = [
        "overview",
        "human_review",
        "database",
        "runs_scoring",
        "analysis",
    ]
    result["refresh_endpoints"] = _refresh_endpoints(case_id)
    result["trace_event_type"] = "human_review_saved"
    return result


def _human_review_post_save_state(
    case_id: str,
    *,
    row_id: int = 0,
    database_path: str | Path = DEFAULT_EVAL_DB,
) -> dict[str, Any]:
    payload = dashboard_payload(database_path=database_path, limit=CASE_DETAIL_LOOKUP_LIMIT)
    normalized = str(case_id or "").strip()
    case = next(
        (item for item in payload.get("cases", []) if item.get("case_id") == normalized),
        {},
    )
    checks = {
        str(item.get("label") or ""): str(item.get("status") or "")
        for item in case.get("case_review_checklist", [])
        if item.get("label")
    }
    analysis = payload.get("analysis") or {}
    human_case_trend = next(
        (
            item
            for item in analysis.get("human_case_trends", [])
            if item.get("case_id") == normalized
        ),
        {},
    )
    summary = payload.get("summary") or {}
    follow_up_item = next(
        (
            item
            for item in payload.get("follow_up_queue", [])
            if item.get("case_id") == normalized
        ),
        {},
    )
    follow_up_open = isinstance(follow_up_item, dict) and bool(follow_up_item)
    follow_up_next = (
        str(follow_up_item.get("next_follow_up") or "")
        if follow_up_open
        else str(case.get("next_follow_up") or "")
    )
    follow_up_summary = (
        str(follow_up_item.get("follow_up_summary") or "") if follow_up_open else ""
    )
    missing_labels = (
        follow_up_item.get("missing_labels")
        if follow_up_open and isinstance(follow_up_item.get("missing_labels"), list)
        else []
    )
    attention_labels = (
        follow_up_item.get("attention_labels")
        if follow_up_open and isinstance(follow_up_item.get("attention_labels"), list)
        else []
    )
    review_trace = (
        get_eval_trace_event(
            database_path=database_path,
            event_type="human_review_saved",
            span_id=f"human_review_row:{int(row_id or 0)}",
        )
        if row_id
        else {}
    )
    review_visible = case.get("human_average") is not None or bool(
        case.get("human_created_at")
    )
    analysis_human_review_visible = bool(human_case_trend)
    return {
        "case_id": normalized,
        "case": {
            "human_average": case.get("human_average"),
            "human_safety": case.get("human_safety") or "",
            "human_created_at": case.get("human_created_at") or "",
            "review_target": case.get("review_target") or {},
            "next_follow_up": case.get("next_follow_up") or "",
            "check_statuses": checks,
        },
        "summary": {
            "human_reviewed": summary.get("human_reviewed", 0),
            "human_average": summary.get("human_average"),
        },
        "analysis": {
            "human_review_trend_days": len(analysis.get("human_review_trends") or []),
            "human_case_trend_days": len(human_case_trend.get("days") or []),
        },
        "follow_up": {
            "still_open": follow_up_open,
            "queue_item": follow_up_item if follow_up_open else {},
            "next_follow_up": follow_up_next,
            "follow_up_summary": follow_up_summary,
            "missing_labels": missing_labels,
            "attention_labels": attention_labels,
        },
        "dashboard_visibility": {
            "case_visible": bool(case),
            "display_case_id": case.get("display_case_id") or normalized,
            "review_visible": review_visible,
            "analysis_human_review_visible": analysis_human_review_visible,
            "in_follow_up_queue": follow_up_open,
        },
        "trace": {
            "human_review_saved_present": bool(review_trace),
            "event_type": str(review_trace.get("event_type") or ""),
            "trace_id": str(review_trace.get("trace_id") or ""),
            "span_id": str(review_trace.get("span_id") or ""),
            "group_id": str(review_trace.get("group_id") or ""),
        },
    }


def _analysis_exclusion_post_save_state(
    case_id: str,
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
) -> dict[str, Any]:
    payload = dashboard_payload(database_path=database_path, limit=CASE_DETAIL_LOOKUP_LIMIT)
    normalized = str(case_id or "").strip()
    case = next(
        (item for item in payload.get("cases", []) if item.get("case_id") == normalized),
        {},
    )
    analysis = payload.get("analysis") or {}
    summary = payload.get("summary") or {}
    workflow = payload.get("workflow_readiness") or {}
    return {
        "case_id": normalized,
        "case": {
            "analysis_excluded": bool(case.get("analysis_excluded")),
            "analysis_exclusion_reason": case.get("analysis_exclusion_reason") or "",
            "next_follow_up": case.get("next_follow_up") or "",
            "review_target": case.get("review_target") or {},
        },
        "summary": {
            "human_reviewed": summary.get("human_reviewed", 0),
            "machine_scored": summary.get("machine_scored", 0),
            "total_cases": summary.get("total_cases", 0),
        },
        "analysis": {
            "run_trends": len(analysis.get("run_trends") or []),
            "case_trends": len(analysis.get("case_trends") or []),
            "human_review_trend_days": len(analysis.get("human_review_trends") or []),
        },
        "workflow_readiness": {
            "human_scorecards": (workflow.get("counts") or {}).get("human_scorecards", 0),
            "promptfoo_imported": (workflow.get("counts") or {}).get("promptfoo_imported", 0),
        },
    }


def save_analysis_exclusion_payload(
    payload: dict[str, Any],
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
) -> dict[str, Any]:
    """Validate and persist one dashboard analysis-inclusion toggle payload."""

    input_case_id = str(payload.get("case_id") or "").strip()
    case_id = _canonical_dashboard_case_id(input_case_id, database_path=database_path)
    result = set_promptfoo_analysis_exclusion(
        eval_id=str(payload.get("eval_id") or ""),
        case_id=case_id,
        excluded=bool(payload.get("excluded")),
        reason=str(payload.get("reason") or ""),
        database_path=database_path,
    )
    result["input_case_id"] = input_case_id
    result["post_save_state"] = _analysis_exclusion_post_save_state(
        case_id,
        database_path=database_path,
    )
    result["database_tables"] = database_table_summaries(database_path)
    result["refresh_targets"] = ["overview", "database", "runs_scoring", "analysis"]
    result["refresh_endpoints"] = _refresh_endpoints(case_id)
    return result


def _canonical_dashboard_case_id(
    case_id: str,
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
) -> str:
    """Return the canonical case id for a dashboard display id when known."""

    normalized = str(case_id or "").strip()
    if not normalized:
        return ""
    payload = dashboard_payload(database_path=database_path, limit=CASE_DETAIL_LOOKUP_LIMIT)
    for item in payload.get("cases", []):
        canonical = str(item.get("case_id") or "").strip()
        display = str(item.get("display_case_id") or "").strip()
        if normalized in {canonical, display}:
            return canonical or normalized
    return normalized


def _case_has_recorded_response(
    case_id: str,
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
) -> bool:
    payload = dashboard_payload(database_path=database_path, limit=CASE_DETAIL_LOOKUP_LIMIT)
    normalized = str(case_id or "").strip()
    for case in payload.get("cases", []):
        if case.get("case_id") != normalized:
            continue
        return bool(
            str(case.get("scored_response_text") or case.get("response_text") or "").strip()
        )
    return False


def _coerce_score(value: Any, dimension: str) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{dimension} must be a numeric 0-5 score") from exc
    if score < 0 or score > 5:
        raise ValueError(f"{dimension} must be between 0 and 5")
    return score


def legacy_dashboard_redirect_target(path: str, query: str = "") -> str | None:
    """Return the canonical dashboard URL for the legacy static dashboard route."""

    if not path.endswith("/.keystone/promptfoo/dashboard.html"):
        return None
    target = eval_dashboard_url()
    if query:
        target = f"{target}?{query}"
    return target


def workflow_readiness_response(
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
    limit: int = 500,
) -> dict[str, Any]:
    """Return the local-only Slack eval workflow readiness contract."""

    db_path = Path(database_path)
    payload = dashboard_payload(database_path=db_path, limit=max(1, int(limit)))
    return {
        "status": "ok",
        "database_path": str(db_path),
        "workflow_readiness": payload.get("workflow_readiness") or {},
    }


def eval_run_ledger_response(
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
    limit: int = 500,
) -> dict[str, Any]:
    """Return the normalized local eval run ledger."""

    db_path = Path(database_path)
    payload = dashboard_payload(database_path=db_path, limit=max(1, int(limit)))
    return {
        "status": "ok",
        "database_path": str(db_path),
        "rows": payload.get("run_ledger") or [],
    }


def follow_up_queue_response(
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
    limit: int = 500,
) -> dict[str, Any]:
    """Return the current eval follow-up queue used by Runs & Scoring."""

    db_path = Path(database_path)
    payload = dashboard_payload(database_path=db_path, limit=max(1, int(limit)))
    rows = payload.get("follow_up_queue") if isinstance(payload.get("follow_up_queue"), list) else []
    return {
        "status": "ok",
        "database_path": str(db_path),
        "mode": "local_follow_up_queue",
        "live_api_calls": False,
        "total": len(rows),
        "rows": rows,
    }


def eval_case_bundle_response(
    *,
    case_id: str,
    database_path: str | Path = DEFAULT_EVAL_DB,
    limit: int = 500,
) -> dict[str, Any]:
    """Return one case bundle for manual or Codex eval review."""

    db_path = Path(database_path)
    payload = dashboard_payload(
        database_path=db_path,
        limit=max(CASE_DETAIL_LOOKUP_LIMIT, int(limit)),
    )
    bundle = dashboard_case_review_bundle(payload, case_id)
    return {
        "status": "ok",
        "database_path": str(db_path),
        "bundle": bundle,
        "copy_text": dashboard_case_review_bundle_text(bundle),
    }


def data_quality_response(
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
    limit: int = 500,
) -> dict[str, Any]:
    """Return local data-quality gates for future eval readiness checks."""

    db_path = Path(database_path)
    payload = dashboard_payload(database_path=db_path, limit=max(1, int(limit)))
    return {
        "status": "ok",
        "database_path": str(db_path),
        "data_quality": payload.get("data_quality") or {},
    }


def trace_diagnostics_response(
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
    limit: int = 500,
) -> dict[str, Any]:
    """Return sanitized trace diagnostics for dashboard analysis and API checks."""

    db_path = Path(database_path)
    payload = dashboard_payload(database_path=db_path, limit=max(1, int(limit)))
    trace_summary = payload.get("trace_summary") if isinstance(payload.get("trace_summary"), dict) else {}
    return {
        "status": "ok",
        "database_path": str(db_path),
        "mode": "local_trace_diagnostics",
        "live_api_calls": False,
        "trace_diagnostics": {
            "event_count": int(trace_summary.get("event_count") or 0),
            "run_summary_count": int(trace_summary.get("run_summary_count") or 0),
            "sdk_run_summary_count": int(trace_summary.get("sdk_run_summary_count") or 0),
            "manual_run_summary_count": int(trace_summary.get("manual_run_summary_count") or 0),
            "joined_run_summary_count": int(trace_summary.get("joined_run_summary_count") or 0),
            "unjoined_run_summary_count": int(trace_summary.get("unjoined_run_summary_count") or 0),
            "diagnostic_category_counts": trace_summary.get("diagnostic_category_counts") or [],
            "diagnostic_category_trends": trace_summary.get("diagnostic_category_trends") or [],
            "diagnostic_case_rollups": trace_summary.get("diagnostic_case_rollups") or [],
            "diagnostic_followups": trace_summary.get("diagnostic_followups") or [],
            "dropped_fields": trace_summary.get("dropped_fields") or [],
            "effective_sensitive_capture": bool(trace_summary.get("effective_sensitive_capture")),
        },
    }


def build_handler(
    *,
    database_path: Path,
    dashboard_path: Path,
    limit: int,
) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to dashboard paths."""

    render_lock = threading.Lock()
    dashboard_cache: dict[str, Any] = {}
    review_cache: dict[str, dict[str, Any]] = {}

    class EvalDashboardHandler(BaseHTTPRequestHandler):
        server_version = "KeystoneEvalDashboard/1.0"

        def do_GET(self) -> None:  # noqa: N802 - http.server API.
            parsed = urlparse(self.path)
            path = parsed.path
            target = legacy_dashboard_redirect_target(path, parsed.query)
            if target:
                self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
                self.send_header("Location", target)
                self._send_no_store_headers()
                self.end_headers()
                return
            if path in {"/", "/dashboard", "/dashboard.html"}:
                self._serve_dashboard()
                return
            if path in {"/review", "/review.html"}:
                self._serve_review(parsed.query)
                return
            if path == "/api/status":
                self._serve_status(refresh_cache=_truthy_query_flag(parsed.query, "refresh"))
                return
            if path == "/api/workflow-readiness":
                self._serve_workflow_readiness()
                return
            if path == "/api/data-quality":
                self._serve_data_quality()
                return
            if path == "/api/trace-diagnostics":
                self._serve_trace_diagnostics()
                return
            if path == "/api/eval-cases":
                self._serve_eval_cases()
                return
            if path == "/api/eval-run-ledger":
                self._serve_eval_run_ledger()
                return
            if path == "/api/follow-up-queue":
                self._serve_follow_up_queue()
                return
            if path == "/api/eval-case-bundle":
                self._serve_eval_case_bundle(parsed.query)
                return
            if path == "/api/eval-cases.csv":
                self._serve_eval_cases_csv()
                return
            if path == "/favicon.ico":
                self._send_empty(HTTPStatus.NO_CONTENT)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

        def do_HEAD(self) -> None:  # noqa: N802 - http.server API.
            parsed = urlparse(self.path)
            path = parsed.path
            target = legacy_dashboard_redirect_target(path, parsed.query)
            if target:
                self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
                self.send_header("Location", target)
                self._send_no_store_headers()
                self.end_headers()
                return
            if path in {"/", "/dashboard", "/dashboard.html"}:
                self._serve_dashboard(head_only=True)
                return
            if path in {"/review", "/review.html"}:
                self._serve_review(parsed.query, head_only=True)
                return
            if path == "/api/status":
                self._serve_status(
                    head_only=True,
                    refresh_cache=_truthy_query_flag(parsed.query, "refresh"),
                )
                return
            if path == "/api/workflow-readiness":
                self._serve_workflow_readiness(head_only=True)
                return
            if path == "/api/data-quality":
                self._serve_data_quality(head_only=True)
                return
            if path == "/api/trace-diagnostics":
                self._serve_trace_diagnostics(head_only=True)
                return
            if path == "/api/eval-cases":
                self._serve_eval_cases(head_only=True)
                return
            if path == "/api/eval-run-ledger":
                self._serve_eval_run_ledger(head_only=True)
                return
            if path == "/api/follow-up-queue":
                self._serve_follow_up_queue(head_only=True)
                return
            if path == "/api/eval-case-bundle":
                self._serve_eval_case_bundle(parsed.query, head_only=True)
                return
            if path == "/api/eval-cases.csv":
                self._serve_eval_cases_csv(head_only=True)
                return
            if path == "/favicon.ico":
                self._send_empty(HTTPStatus.NO_CONTENT)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

        def do_POST(self) -> None:  # noqa: N802 - http.server API.
            path = urlparse(self.path).path
            if path == "/api/human-review":
                self._save_human_review()
                return
            if path == "/api/analysis-exclusion":
                self._save_analysis_exclusion()
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

        def _save_human_review(self) -> None:
            try:
                payload = self._read_json_body()
                result = save_human_review_payload(
                    payload,
                    database_path=database_path,
                    require_recorded_response=True,
                )
                with render_lock:
                    self._refresh_dashboard_cache()
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._send_json({"status": "error", "error": str(exc)}, status=400)
                return
            self._send_json({"status": "saved", **result})

        def _save_analysis_exclusion(self) -> None:
            try:
                payload = self._read_json_body()
                result = save_analysis_exclusion_payload(
                    payload,
                    database_path=database_path,
                )
                with render_lock:
                    self._refresh_dashboard_cache()
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._send_json({"status": "error", "error": str(exc)}, status=400)
                return
            self._send_json({"status": "saved", **result})

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _serve_status(
            self,
            *,
            head_only: bool = False,
            refresh_cache: bool = False,
        ) -> None:
            refreshed = False
            refresh_error = ""
            if refresh_cache and not head_only:
                with render_lock:
                    try:
                        self._refresh_dashboard_cache()
                    except (OSError, sqlite3.Error) as exc:
                        refresh_error = f"{type(exc).__name__}: {exc}"
                        if self._dashboard_fallback_body(error=exc) is None:
                            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                            return
                    else:
                        refreshed = True
            db_signature = _database_cache_signature(database_path)
            cache_signature = dashboard_cache.get("db_signature")
            cache_expires_at = float(dashboard_cache.get("expires_at") or 0.0)
            now = time.monotonic()
            cache_present = isinstance(dashboard_cache.get("body"), bytes)
            table_summaries = _database_table_summaries_safe(database_path)
            payload = {
                "status": "ok",
                "database_path": str(database_path),
                "database_signature": db_signature,
                "database_tables": table_summaries["tables"],
                "database_tables_ok": bool(table_summaries["ok"]),
                "database_tables_error": str(table_summaries["error"] or ""),
                "nofile_limit": dict(_NOFILE_LIMIT_STATUS),
                "render_cache_ttl_seconds": SERVER_RENDER_CACHE_TTL_SECONDS,
                "render_cache_refresh_endpoint": "/api/status?refresh=1",
                "render_cache_refresh_requested": bool(refresh_cache),
                "render_cache_refreshed": refreshed,
                "render_cache_last_error": refresh_error
                or str(dashboard_cache.get("last_refresh_error") or ""),
                "render_cache_serving_stale": bool(
                    dashboard_cache.get("served_stale_after_error")
                ),
                "dashboard_cache": {
                    "present": cache_present,
                    "fresh_for_database": cache_present and cache_signature == db_signature,
                    "expires_in_seconds": max(0.0, round(cache_expires_at - now, 3)),
                },
            }
            self._send_json(payload, head_only=head_only)

        def _serve_dashboard(self, *, head_only: bool = False) -> None:
            acquired = render_lock.acquire(timeout=SERVER_RENDER_LOCK_TIMEOUT_SECONDS)
            try:
                if acquired:
                    body = self._dashboard_body()
                else:
                    fallback = self._dashboard_fallback_body(
                        error=TimeoutError("dashboard render lock timed out")
                    )
                    if fallback is None:
                        self._send_error(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            "dashboard render lock timed out",
                        )
                        return
                    body = fallback
            except (OSError, sqlite3.Error) as exc:
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            finally:
                if acquired:
                    render_lock.release()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._send_no_store_headers()
            self.end_headers()
            if head_only:
                return
            self._write_body(body)

        def _serve_review(self, query: str, *, head_only: bool = False) -> None:
            case_id = str((parse_qs(query).get("case") or [""])[0] or "").strip()
            if not case_id:
                self.send_error(HTTPStatus.BAD_REQUEST, "case query parameter is required")
                return
            try:
                with render_lock:
                    body = self._review_body(case_id)
            except (OSError, sqlite3.Error) as exc:
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._send_no_store_headers()
            self.end_headers()
            if head_only:
                return
            self._write_body(body)

        def _serve_eval_cases(self, *, head_only: bool = False) -> None:
            try:
                with render_lock:
                    payload = dashboard_payload(database_path=database_path, limit=limit)
                    rows = dashboard_case_export_rows(payload)
            except (OSError, sqlite3.Error) as exc:
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self._send_json(
                {
                    "status": "ok",
                    "database_path": str(database_path),
                    "rows": rows,
                },
                head_only=head_only,
            )

        def _serve_eval_run_ledger(self, *, head_only: bool = False) -> None:
            try:
                with render_lock:
                    payload = eval_run_ledger_response(
                        database_path=database_path,
                        limit=limit,
                    )
            except (OSError, sqlite3.Error) as exc:
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self._send_json(payload, head_only=head_only)

        def _serve_follow_up_queue(self, *, head_only: bool = False) -> None:
            try:
                with render_lock:
                    payload = follow_up_queue_response(
                        database_path=database_path,
                        limit=limit,
                    )
            except (OSError, sqlite3.Error) as exc:
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self._send_json(payload, head_only=head_only)

        def _serve_eval_case_bundle(self, query: str, *, head_only: bool = False) -> None:
            case_id = str((parse_qs(query).get("case") or [""])[0] or "").strip()
            try:
                with render_lock:
                    payload = eval_case_bundle_response(
                        case_id=case_id,
                        database_path=database_path,
                        limit=limit,
                    )
            except ValueError as exc:
                self._send_json({"status": "error", "error": str(exc)}, status=400)
                return
            except (OSError, sqlite3.Error) as exc:
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self._send_json(payload, head_only=head_only)

        def _serve_data_quality(self, *, head_only: bool = False) -> None:
            try:
                with render_lock:
                    payload = data_quality_response(
                        database_path=database_path,
                        limit=limit,
                    )
            except (OSError, sqlite3.Error) as exc:
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self._send_json(payload, head_only=head_only)

        def _serve_trace_diagnostics(self, *, head_only: bool = False) -> None:
            try:
                with render_lock:
                    payload = trace_diagnostics_response(
                        database_path=database_path,
                        limit=limit,
                    )
            except (OSError, sqlite3.Error) as exc:
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self._send_json(payload, head_only=head_only)

        def _serve_workflow_readiness(self, *, head_only: bool = False) -> None:
            try:
                with render_lock:
                    payload = workflow_readiness_response(
                        database_path=database_path,
                        limit=limit,
                    )
            except (OSError, sqlite3.Error) as exc:
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self._send_json(payload, head_only=head_only)

        def _serve_eval_cases_csv(self, *, head_only: bool = False) -> None:
            try:
                with render_lock:
                    payload = dashboard_payload(database_path=database_path, limit=limit)
                    body = dashboard_case_export_csv(payload).encode("utf-8")
            except (OSError, sqlite3.Error) as exc:
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="keystone-eval-cases.csv"')
            self.send_header("Content-Length", str(len(body)))
            self._send_no_store_headers()
            self.end_headers()
            if head_only:
                return
            self._write_body(body)

        def _dashboard_body(self) -> bytes:
            now = time.monotonic()
            db_signature = _database_cache_signature(database_path)
            body = dashboard_cache.get("body")
            expires_at = float(dashboard_cache.get("expires_at") or 0.0)
            if (
                isinstance(body, bytes)
                and now < expires_at
                and dashboard_cache.get("db_signature") == db_signature
            ):
                return body
            try:
                return self._refresh_dashboard_cache()
            except (OSError, sqlite3.Error) as exc:
                fallback = self._dashboard_fallback_body(error=exc)
                if fallback is not None:
                    return fallback
                raise

        def _refresh_dashboard_cache(self) -> bytes:
            path = render_dashboard(
                database_path=database_path,
                output_path=dashboard_path,
                limit=limit,
            )
            body = path.read_bytes()
            dashboard_cache.clear()
            dashboard_cache.update(
                {
                    "body": body,
                    "db_signature": _database_cache_signature(database_path),
                    "expires_at": time.monotonic() + SERVER_RENDER_CACHE_TTL_SECONDS,
                    "last_refresh_error": "",
                    "served_stale_after_error": False,
                }
            )
            review_cache.clear()
            return body

        def _dashboard_fallback_body(self, *, error: BaseException) -> bytes | None:
            error_text = f"{type(error).__name__}: {error}"
            body = dashboard_cache.get("body")
            if not isinstance(body, bytes):
                try:
                    body = dashboard_path.read_bytes()
                except OSError:
                    return None
            dashboard_cache.update(
                {
                    "body": body,
                    "db_signature": dashboard_cache.get("db_signature")
                    or _database_cache_signature(database_path),
                    "expires_at": time.monotonic() + SERVER_RENDER_CACHE_TTL_SECONDS,
                    "last_refresh_error": error_text,
                    "served_stale_after_error": True,
                }
            )
            return body

        def _review_body(self, case_id: str) -> bytes:
            now = time.monotonic()
            db_signature = _database_cache_signature(database_path)
            cached = review_cache.get(case_id) or {}
            body = cached.get("body")
            expires_at = float(cached.get("expires_at") or 0.0)
            if (
                isinstance(body, bytes)
                and now < expires_at
                and cached.get("db_signature") == db_signature
            ):
                return body
            body = render_review_form(
                case_id=case_id,
                database_path=database_path,
                limit=limit,
            ).encode("utf-8")
            review_cache[case_id] = {
                "body": body,
                "db_signature": db_signature,
                "expires_at": time.monotonic() + SERVER_RENDER_CACHE_TTL_SECONDS,
            }
            return body

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                raise ValueError("JSON body is required")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload

        def _send_json(
            self,
            payload: dict[str, Any],
            *,
            status: int = 200,
            head_only: bool = False,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._send_no_store_headers()
            self.end_headers()
            if head_only:
                return
            self._write_body(body)

        def _write_body(self, body: bytes) -> None:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            try:
                self.send_error(status, message)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _send_empty(self, status: HTTPStatus) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self._send_no_store_headers()
            self.end_headers()

        def _send_no_store_headers(self) -> None:
            self.send_header("Cache-Control", NO_STORE_CACHE_CONTROL)
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")

    return EvalDashboardHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the local Promptfoo eval dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--database-path", default=str(DEFAULT_EVAL_DB))
    parser.add_argument("--dashboard-path", default=str(DEFAULT_DASHBOARD_PATH))
    parser.add_argument("--limit", type=int, default=500)
    return parser


def main() -> int:
    global _NOFILE_LIMIT_STATUS
    args = build_parser().parse_args()
    _NOFILE_LIMIT_STATUS = _raise_nofile_limit()
    database_path = Path(args.database_path).expanduser().resolve()
    dashboard_path = Path(args.dashboard_path).expanduser().resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    dashboard_path.parent.mkdir(parents=True, exist_ok=True)
    handler = build_handler(
        database_path=database_path,
        dashboard_path=dashboard_path,
        limit=max(1, int(args.limit)),
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    server.daemon_threads = True
    print(f"Serving Keystone eval dashboard at http://{args.host}:{args.port}/dashboard")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
