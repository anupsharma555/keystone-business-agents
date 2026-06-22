"""Opt-in redacted trace summaries for Keystone eval diagnostics."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from keystone_agents.model_provider import sanitize_trace_metadata
from promptfoo.eval_database import DEFAULT_EVAL_DB, record_eval_trace_event

_REGISTERED_PROCESSOR: KeystoneEvalTraceProcessor | None = None
_REGISTER_LOCK = threading.Lock()


class KeystoneEvalTraceProcessor:
    """Agents SDK trace processor that records only safe local summaries."""

    def __init__(self, *, database_path: str | Path | None = None) -> None:
        self._lock = threading.Lock()
        self._starts: dict[str, float] = {}
        self._events: list[dict[str, Any]] = []
        self._database_path = Path(database_path) if database_path is not None else None

    @property
    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)

    def on_trace_start(self, trace: Any) -> None:
        self._record("trace_start", trace)

    def on_trace_end(self, trace: Any) -> None:
        self._record("trace_end", trace)

    def on_span_start(self, span: Any) -> None:
        self._record("span_start", span)

    def on_span_end(self, span: Any) -> None:
        self._record("span_end", span)

    def shutdown(self) -> None:
        with self._lock:
            self._starts.clear()

    def force_flush(self) -> None:
        return None

    def _record(self, event_type: str, item: Any) -> None:
        now = time.time()
        identifier = _identifier(item)
        payload = _safe_trace_payload(event_type, item)
        with self._lock:
            if event_type.endswith("_start"):
                self._starts[identifier] = now
            elif event_type.endswith("_end"):
                started = self._starts.pop(identifier, None)
                if started is not None:
                    payload["duration_ms"] = round((now - started) * 1000, 3)
            self._events.append(payload)
        self._persist(payload)

    def _persist(self, payload: dict[str, Any]) -> None:
        if self._database_path is None:
            return
        try:
            record_eval_trace_event(
                event_type=str(payload.get("event_type") or ""),
                trace_id=str(payload.get("trace_id") or ""),
                span_id=str(payload.get("span_id") or ""),
                parent_id=str(payload.get("parent_id") or ""),
                name=str(payload.get("name") or ""),
                group_id=str(payload.get("group_id") or ""),
                metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
                duration_ms=payload.get("duration_ms"),
                database_path=self._database_path,
            )
        except Exception:
            return


def register_configured_trace_processor() -> KeystoneEvalTraceProcessor | None:
    """Register the eval-summary processor when explicitly requested."""

    if os.environ.get("KEYSTONE_TRACE_PROCESSOR", "").strip().lower() != "eval_summary":
        return None
    global _REGISTERED_PROCESSOR
    with _REGISTER_LOCK:
        if _REGISTERED_PROCESSOR is not None:
            return _REGISTERED_PROCESSOR
        try:
            from agents.tracing import add_trace_processor
        except Exception:
            return None
        processor = KeystoneEvalTraceProcessor(database_path=_configured_database_path())
        add_trace_processor(processor)  # type: ignore[arg-type]
        _REGISTERED_PROCESSOR = processor
        return processor


def _configured_database_path() -> Path:
    return Path(os.environ.get("KEYSTONE_TRACE_SUMMARY_DB") or DEFAULT_EVAL_DB)


def record_sdk_run_summary_trace_event(
    *,
    agent_name: str,
    route: str = "",
    stage: str = "sdk_agent_run",
    live: bool = False,
    run_mode: str = "",
    model_provider: str = "",
    model_name: str = "",
    request_cache: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    cost: dict[str, Any] | None = None,
    budget_guard: dict[str, Any] | None = None,
    search_diagnostics: dict[str, Any] | None = None,
    orchestrator_diagnostics: dict[str, Any] | None = None,
    raw_result: Any | None = None,
    trace_metadata: dict[str, Any] | None = None,
    status: str = "ok",
    failure_kind: str = "",
    retry_count: int = 0,
    repair_loop_count: int = 0,
    duration_ms: float | None = None,
    database_path: str | Path | None = None,
) -> int | None:
    """Persist one sanitized SDK run-level trace summary when trace summaries are enabled."""

    db_path = _sdk_summary_database_path(database_path)
    if db_path is None:
        return None
    request_cache = request_cache or {}
    usage = usage or {}
    cost = cost or {}
    budget_guard = budget_guard or {}
    search_diagnostics = search_diagnostics or {}
    orchestrator_diagnostics = orchestrator_diagnostics or {}
    trace_metadata = trace_metadata or {}
    correlation = _safe_correlation(trace_metadata)
    trace_id = (
        correlation.get("trace_id")
        or correlation.get("run_id")
        or correlation.get("work_item_id")
        or correlation.get("case_id")
        or _summary_trace_id(agent_name, request_cache, duration_ms)
    )
    metadata = _sdk_run_summary_metadata(
        agent_name=agent_name,
        route=route or str(trace_metadata.get("route") or ""),
        stage=stage,
        live=live,
        run_mode=run_mode,
        model_provider=model_provider,
        model_name=model_name,
        request_cache=request_cache,
        usage=usage,
        cost=cost,
        budget_guard=budget_guard,
        search_diagnostics=search_diagnostics,
        orchestrator_diagnostics=orchestrator_diagnostics,
        raw_result=raw_result,
        correlation=correlation,
        status=status,
        failure_kind=failure_kind,
        retry_count=retry_count,
        repair_loop_count=repair_loop_count,
        duration_ms=duration_ms,
    )
    try:
        return record_eval_trace_event(
            event_type="sdk_run_summary",
            trace_id=str(trace_id),
            name="sdk_run_summary",
            group_id=str(correlation.get("case_id") or correlation.get("work_item_id") or ""),
            metadata=metadata,
            duration_ms=duration_ms,
            database_path=db_path,
        )
    except Exception:
        return None


def _sdk_summary_database_path(database_path: str | Path | None) -> Path | None:
    if database_path is not None:
        return Path(database_path)
    if os.environ.get("KEYSTONE_TRACE_PROCESSOR", "").strip().lower() == "eval_summary":
        return _configured_database_path()
    if os.environ.get("KEYSTONE_TRACE_SUMMARY_DB", "").strip():
        return _configured_database_path()
    return None


def _sdk_run_summary_metadata(
    *,
    agent_name: str,
    route: str,
    stage: str,
    live: bool,
    run_mode: str,
    model_provider: str,
    model_name: str,
    request_cache: dict[str, Any],
    usage: dict[str, Any],
    cost: dict[str, Any],
    budget_guard: dict[str, Any],
    search_diagnostics: dict[str, Any],
    orchestrator_diagnostics: dict[str, Any],
    raw_result: Any,
    correlation: dict[str, str],
    status: str,
    failure_kind: str,
    retry_count: int,
    repair_loop_count: int,
    duration_ms: float | None,
) -> dict[str, Any]:
    observed = _observed_sdk_activity(raw_result)
    turns_used, turns_source = _turns_used(raw_result, usage)
    retrieval_errors = len(search_diagnostics.get("search_provider_errors") or [])
    retrieval_error_types = _clean_issue_types(
        search_diagnostics.get("search_provider_errors")
        or search_diagnostics.get("provider_errors")
        or []
    )
    web_extraction_issue_count = _safe_int(
        search_diagnostics.get("web_extraction_issue_count")
        or search_diagnostics.get("extraction_error_count")
        or search_diagnostics.get("extraction_warning_count")
    )
    web_extraction_issue_types = _clean_issue_types(
        search_diagnostics.get("web_extraction_issues")
        or search_diagnostics.get("extraction_issues")
        or []
    )
    failed_tool_count = max(
        _safe_int(search_diagnostics.get("failed_tool_call_count")),
        observed["failed_tool_call_count"],
    )
    tool_names = sorted(observed["tool_call_counts"].keys())[:20]
    has_retrieval_metadata = bool(
        search_diagnostics.get("provider_summary")
        or search_diagnostics.get("providers_used")
        or search_diagnostics.get("search_providers_attempted")
        or retrieval_errors
    )
    status_text = _clean_scalar(status)
    failure_text = _clean_scalar(failure_kind)
    retry_total = max(0, int(retry_count or 0))
    repair_total = max(0, int(repair_loop_count or 0))
    has_orchestrator_preflight = bool(
        orchestrator_diagnostics.get("has_preflight")
        or orchestrator_diagnostics.get("preflight")
        or orchestrator_diagnostics.get("route_advice")
        or orchestrator_diagnostics.get("planner_rationale")
    )
    has_orchestrator_review = bool(
        orchestrator_diagnostics.get("has_review")
        or orchestrator_diagnostics.get("review")
        or orchestrator_diagnostics.get("feedback")
        or orchestrator_diagnostics.get("review_summary")
    )
    orchestrator_feedback_count = _safe_int(
        orchestrator_diagnostics.get("feedback_count")
        or orchestrator_diagnostics.get("review_feedback_count")
    )
    orchestrator_blocker_count = _safe_int(
        orchestrator_diagnostics.get("blocker_count")
        or orchestrator_diagnostics.get("preflight_blocker_count")
    )
    metadata = {
        "schema": "keystone.sdk_run_summary.v1",
        "agent": _clean_scalar(agent_name),
        "route": _clean_scalar(route),
        "stage": _clean_scalar(stage),
        "status": status_text,
        "failure_kind": failure_text,
        "correlation": correlation,
        "live": bool(live),
        "run_mode": _clean_scalar(run_mode),
        "model_provider": _clean_scalar(model_provider),
        "model_name": _clean_scalar(model_name),
        "session": {
            "scope": _clean_scalar(request_cache.get("session_scope")),
            "source": _clean_scalar(request_cache.get("session_source")),
            "session_id_hash": _clean_scalar(request_cache.get("session_id_hash"), identifier=True),
            "history_mode": _clean_scalar(request_cache.get("session_history_mode")),
            "history_limit": _safe_int(request_cache.get("session_history_limit")),
            "truncation_configured": bool(request_cache.get("session_truncation_configured")),
        },
        "turns_used": turns_used,
        "turns_used_source": turns_source,
        "max_turns": _safe_int(request_cache.get("max_turns")),
        "max_turns_source": _clean_scalar(request_cache.get("max_turns_source")),
        "sdk_request_count": _safe_int(usage.get("requests")),
        "configured_tool_count": _safe_int(request_cache.get("tool_count")),
        "tool_call_count": observed["tool_call_count"],
        "tool_call_counts": observed["tool_call_counts"],
        "tool_call_summary": observed["tool_call_summary"],
        "handoff_count": observed["handoff_count"],
        "retry_count": retry_total,
        "repair_loop_count": repair_total,
            "retrieval_provider_summary": {
                "provider_summary": _clean_scalar(search_diagnostics.get("provider_summary")),
                "providers_used": _clean_string_list(search_diagnostics.get("providers_used")),
                "attempted": _clean_string_list(search_diagnostics.get("search_providers_attempted")),
                "error_count": retrieval_errors,
                "error_types": retrieval_error_types,
            },
        "prompt_cache": {
            "static_prefix_sha256": _clean_hash(request_cache.get("static_prefix_sha256")),
            "dynamic_prompt_sha256": _clean_hash(request_cache.get("dynamic_prompt_sha256")),
            "tool_names_sha256": _clean_hash(request_cache.get("tool_names_sha256")),
            "prompt_cache_key_present": bool(usage.get("prompt_cache_key_present")),
            "prompt_cache_key_hash": _clean_hash(usage.get("prompt_cache_key_hash")),
        },
        "token_summary": {
            "available": bool(usage.get("available")),
            "input_tokens": _safe_int(usage.get("input_tokens")),
            "cached_input_tokens": _safe_int(usage.get("cached_input_tokens")),
            "output_tokens": _safe_int(usage.get("output_tokens")),
            "total_tokens": _safe_int(usage.get("total_tokens")),
            "cache_hit_rate": _safe_float(usage.get("cache_hit_rate")),
        },
        "cost_summary": {
            "source": _clean_scalar(cost.get("source")),
            "estimated_usd": _safe_float(cost.get("estimated_usd")),
            "pricing_provider": _clean_scalar(cost.get("pricing_provider")),
            "pricing_model": _clean_scalar(cost.get("pricing_model")),
            "budget_status": _clean_scalar(budget_guard.get("status")),
            "budget_exceeded": bool(budget_guard.get("exceeded")),
        },
        "redaction": {
            "raw_prompt_included": False,
            "raw_response_included": False,
            "raw_tool_io_included": False,
        },
    }
    metadata.update(
        {
            "diagnostic_contract": {
                "schema": "keystone.eval_run_diagnostics.v1",
                "included": [
                    "timing",
                    "model",
                    "tooling",
                    "retrieval",
                    "orchestrator",
                    "approval",
                    "side_effects",
                    "error_retry",
                    "prompt_version",
                ],
                "raw_payloads_included": False,
                "logs_hold_verbose_details": True,
                "future_api_expected": [
                    "orchestrator_review",
                    "web_extraction_summary",
                ],
            },
            "execution": {
                "run_mode": _clean_scalar(run_mode),
                "stage": _clean_scalar(stage),
                "duration_ms": _safe_float(duration_ms),
                "live": bool(live),
            },
            "model": {
                "provider": _clean_scalar(model_provider),
                "name": _clean_scalar(model_name),
                "has_model_config": bool(model_provider or model_name),
            },
            "tooling": {
                "tool_call_count": observed["tool_call_count"],
                "failed_tool_call_count": failed_tool_count,
                "tool_names": tool_names,
                "tool_call_summary": observed["tool_call_summary"],
                "handoff_count": observed["handoff_count"],
                "has_tool_metadata": bool(observed["tool_call_count"] or failed_tool_count or tool_names),
            },
            "retrieval": {
                "search_provider": _clean_scalar(search_diagnostics.get("provider_summary")),
                "search_provider_sequence": _clean_string_list(
                    search_diagnostics.get("providers_used")
                    or search_diagnostics.get("search_providers_attempted")
                ),
                "source_count": _safe_int(search_diagnostics.get("source_count")),
                "visible_source_count": _safe_int(search_diagnostics.get("visible_source_count")),
                "web_extraction_status": _clean_scalar(search_diagnostics.get("web_extraction_status")),
                "web_extraction_issue_count": web_extraction_issue_count,
                "web_extraction_issue_types": web_extraction_issue_types,
                "search_provider_error_count": retrieval_errors,
                "search_provider_error_types": retrieval_error_types,
            },
            "cost": {
                "cost_profile": _clean_scalar(cost.get("source")),
                "sdk_estimated_cost_usd": _safe_float(cost.get("estimated_usd")),
                "sdk_cache_hit_rate": _safe_float(usage.get("cache_hit_rate")),
            },
            "orchestrator": {
                "has_preflight": has_orchestrator_preflight,
                "has_review": has_orchestrator_review,
                "feedback_count": orchestrator_feedback_count,
                "blocker_count": orchestrator_blocker_count,
                "route_confidence": _safe_float(orchestrator_diagnostics.get("route_confidence")),
                "selected_route": _clean_scalar(orchestrator_diagnostics.get("selected_route")),
                "review_status": _clean_scalar(orchestrator_diagnostics.get("review_status")),
            },
            "approval": {
                "required": bool(budget_guard.get("approval_required")),
                "status": _clean_scalar(budget_guard.get("approval_status") or budget_guard.get("status")),
                "send_enabled": False,
            },
            "side_effects": {
                "external_write_performed": False,
                "storage_mode": "api_redacted" if live else "local_review",
            },
            "error_retry": {
                "warning_count": retrieval_errors + web_extraction_issue_count,
                "warning_types": _sdk_warning_types(
                    retrieval_errors=retrieval_errors,
                    web_extraction_issue_count=web_extraction_issue_count,
                    failure_kind=failure_text,
                    repair_loop_count=repair_total,
                    orchestrator_blocker_count=orchestrator_blocker_count,
                ),
                "retry_count": retry_total,
                "retry_status": "retried" if retry_total else "",
            },
            "prompt_version": {
                "prompt_version_count": 0,
                "prompt_metadata_hash": "",
                "request_text_hash": "",
                "result_summary_hash": "",
                "response_hash": "",
                "static_prefix_sha256": _clean_hash(request_cache.get("static_prefix_sha256")),
                "dynamic_prompt_sha256": _clean_hash(request_cache.get("dynamic_prompt_sha256")),
            },
            "diagnostic_summary": {
                "has_model_metadata": bool(model_provider or model_name),
                "has_tool_metadata": bool(observed["tool_call_count"] or failed_tool_count or tool_names),
                "has_retrieval_metadata": has_retrieval_metadata,
                "has_orchestrator_feedback": (
                    has_orchestrator_preflight
                    or has_orchestrator_review
                    or orchestrator_feedback_count > 0
                    or orchestrator_blocker_count > 0
                ),
                "has_web_extraction_issues": web_extraction_issue_count > 0,
                "has_error_or_retry": (
                    status_text not in {"", "ok", "done", "success"}
                    or bool(failure_text)
                    or retry_total > 0
                    or repair_total > 0
                    or failed_tool_count > 0
                    or retrieval_errors > 0
                    or web_extraction_issue_count > 0
                    or orchestrator_blocker_count > 0
                ),
            },
        }
    )
    return metadata


def _sdk_warning_types(
    *,
    retrieval_errors: int,
    web_extraction_issue_count: int,
    failure_kind: str,
    repair_loop_count: int,
    orchestrator_blocker_count: int,
) -> list[str]:
    warnings: list[str] = []
    if retrieval_errors:
        warnings.append("retrieval_provider_errors")
    if web_extraction_issue_count:
        warnings.append("web_extraction_issues")
    if failure_kind:
        warnings.append(failure_kind)
    if repair_loop_count:
        warnings.append("repair_loop")
    if orchestrator_blocker_count:
        warnings.append("orchestrator_blockers")
    return warnings


def _safe_correlation(trace_metadata: dict[str, Any]) -> dict[str, str]:
    allowed = (
        "agent",
        "case_id",
        "eval_id",
        "run_id",
        "slack_channel_id",
        "slack_channel_name",
        "slack_thread_ts",
        "stage",
        "status",
        "trace_id",
        "work_item_id",
    )
    result: dict[str, str] = {}
    for key in allowed:
        value = trace_metadata.get(key)
        cleaned = _clean_scalar(value, identifier=key.endswith("_id") or key == "trace_id")
        if cleaned:
            result[key] = cleaned
    return result


def _observed_sdk_activity(raw_result: Any) -> dict[str, Any]:
    tool_counts: dict[str, int] = {}
    tool_failed_counts: dict[str, int] = {}
    tool_statuses: dict[str, set[str]] = {}
    handoff_count = 0
    for item in _result_items(raw_result):
        kind = _clean_scalar(
            getattr(item, "type", "")
            or getattr(item, "item_type", "")
            or item.__class__.__name__
        ).lower()
        name = _activity_name(item)
        if "handoff" in kind or "handoff" in name.lower():
            handoff_count += 1
            continue
        if "tool" in kind or "function" in kind or name:
            safe_name = _clean_scalar(name or "unknown_tool")
            if safe_name:
                tool_counts[safe_name] = tool_counts.get(safe_name, 0) + 1
                status = _activity_status(item)
                if status:
                    tool_statuses.setdefault(safe_name, set()).add(status)
                if status in {"error", "failed", "failure", "timeout"} or _activity_error_kind(item):
                    tool_failed_counts[safe_name] = tool_failed_counts.get(safe_name, 0) + 1
    tool_call_summary = []
    for name, count in sorted(tool_counts.items()):
        failed_count = tool_failed_counts.get(name, 0)
        statuses = sorted(tool_statuses.get(name, set()))
        if failed_count:
            status = "failed"
        elif statuses:
            status = statuses[-1]
        else:
            status = "observed"
        tool_call_summary.append(
            {
                "name": name,
                "count": count,
                "failed_count": failed_count,
                "status": status,
            }
        )
    return {
        "tool_call_count": sum(tool_counts.values()),
        "tool_call_counts": dict(sorted(tool_counts.items())),
        "failed_tool_call_count": sum(tool_failed_counts.values()),
        "tool_call_summary": tool_call_summary[:20],
        "handoff_count": handoff_count,
    }


def _result_items(raw_result: Any) -> list[Any]:
    items: list[Any] = []
    for attr in ("new_items", "items", "run_items"):
        value = getattr(raw_result, attr, None)
        if isinstance(value, list | tuple):
            items.extend(value)
    if isinstance(raw_result, dict):
        for attr in ("new_items", "items", "run_items"):
            value = raw_result.get(attr)
            if isinstance(value, list | tuple):
                items.extend(value)
    return items[:200]


def _activity_name(item: Any) -> str:
    for candidate in (
        getattr(item, "name", ""),
        getattr(getattr(item, "raw_item", None), "name", ""),
        getattr(getattr(item, "item", None), "name", ""),
        getattr(getattr(item, "tool", None), "name", ""),
    ):
        if candidate:
            return str(candidate)
    if isinstance(item, dict):
        for key in ("name", "tool_name", "function_name"):
            if item.get(key):
                return str(item[key])
        raw = item.get("raw_item")
        if isinstance(raw, dict):
            return str(raw.get("name") or raw.get("tool_name") or "")
    return ""


def _activity_status(item: Any) -> str:
    for candidate in (
        getattr(item, "status", ""),
        getattr(item, "state", ""),
        getattr(item, "outcome", ""),
        getattr(getattr(item, "raw_item", None), "status", ""),
        getattr(getattr(item, "item", None), "status", ""),
    ):
        cleaned = _clean_scalar(candidate)
        if cleaned:
            return cleaned.lower()
    if isinstance(item, dict):
        for key in ("status", "state", "outcome"):
            cleaned = _clean_scalar(item.get(key))
            if cleaned:
                return cleaned.lower()
        raw = item.get("raw_item")
        if isinstance(raw, dict):
            cleaned = _clean_scalar(raw.get("status") or raw.get("state") or raw.get("outcome"))
            if cleaned:
                return cleaned.lower()
    return ""


def _activity_error_kind(item: Any) -> str:
    for candidate in (
        getattr(item, "error_type", ""),
        getattr(item, "error_kind", ""),
        getattr(item, "exception_type", ""),
        getattr(getattr(item, "raw_item", None), "error_type", ""),
        getattr(getattr(item, "item", None), "error_type", ""),
    ):
        cleaned = _clean_scalar(candidate)
        if cleaned:
            return cleaned
    if isinstance(item, dict):
        for key in ("error_type", "error_kind", "exception_type", "error"):
            if item.get(key):
                return _clean_scalar(key if key == "error" else item.get(key))
        raw = item.get("raw_item")
        if isinstance(raw, dict):
            for key in ("error_type", "error_kind", "exception_type", "error"):
                if raw.get(key):
                    return _clean_scalar(key if key == "error" else raw.get(key))
    return ""


def _turns_used(raw_result: Any, usage: dict[str, Any]) -> tuple[int, str]:
    for source, value in (
        ("sdk_result", getattr(raw_result, "turns_used", None)),
        ("sdk_result", getattr(raw_result, "turn_count", None)),
        ("sdk_result", getattr(raw_result, "turns", None)),
        ("usage_requests", usage.get("requests")),
    ):
        count = _safe_int(value)
        if count:
            return count, source
    return 0, "unavailable"


def _summary_trace_id(
    agent_name: str,
    request_cache: dict[str, Any],
    duration_ms: float | None,
) -> str:
    parts = [
        agent_name,
        str(request_cache.get("static_prefix_sha256") or ""),
        str(request_cache.get("dynamic_prompt_sha256") or ""),
        str(duration_ms or ""),
        str(time.time()),
    ]
    return "sdk_run_" + _text_hash("|".join(parts))


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_hash(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"[a-fA-F0-9]{12,64}", text):
        return text.lower()
    return _text_hash(text)


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    result: list[str] = []
    for item in value[:20]:
        cleaned = _clean_scalar(item)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _clean_issue_types(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    result: list[str] = []
    for item in value[:20]:
        if isinstance(item, dict):
            raw = (
                item.get("error_type")
                or item.get("type")
                or item.get("code")
                or item.get("status")
                or item.get("provider")
            )
        else:
            raw = item
        cleaned = _clean_scalar(raw)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _safe_trace_payload(event_type: str, item: Any) -> dict[str, Any]:
    metadata = _metadata(item)
    return {
        "schema": "keystone.trace_summary.v1",
        "event_type": event_type,
        "trace_id": _clean_scalar(getattr(item, "trace_id", ""), identifier=True),
        "span_id": _clean_scalar(getattr(item, "span_id", ""), identifier=True),
        "parent_id": _clean_scalar(getattr(item, "parent_id", ""), identifier=True),
        "name": _clean_scalar(
            getattr(item, "name", "")
            or getattr(item, "workflow_name", "")
            or _span_data_name(getattr(item, "span_data", None))
        ),
        "group_id": _clean_scalar(getattr(item, "group_id", ""), identifier=True),
        "metadata": metadata,
    }


def _metadata(item: Any) -> dict[str, Any]:
    raw = getattr(item, "metadata", None)
    if not isinstance(raw, dict):
        return {}
    try:
        return sanitize_trace_metadata(raw)
    except Exception:
        return {"metadata_status": "rejected_unsafe"}


def _identifier(item: Any) -> str:
    return (
        _clean_scalar(getattr(item, "span_id", ""), identifier=True)
        or _clean_scalar(getattr(item, "trace_id", ""), identifier=True)
        or str(id(item))
    )


def _span_data_name(span_data: Any) -> str:
    if span_data is None:
        return ""
    return str(
        getattr(span_data, "name", "")
        or getattr(span_data, "type", "")
        or span_data.__class__.__name__
    )


def _clean_scalar(value: Any, *, max_chars: int = 160, identifier: bool = False) -> str:
    text = str(value or "").strip().replace("\n", " ").replace("\r", " ")
    if not text:
        return ""
    if identifier:
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", text):
            return text
        return "redacted_" + _text_hash(text)
    redacted = _redact_scalar_text(text)
    if _looks_unsafe(redacted):
        return "redacted_" + _text_hash(text)
    if len(redacted) > max_chars:
        return redacted[:max_chars] + "...[truncated]"
    return redacted


def _redact_scalar_text(value: str) -> str:
    text = value
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", text)
    text = re.sub(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "[REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b", "Bearer [REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[REDACTED_EMAIL]", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b", "[REDACTED_PHONE]", text)
    return " ".join(text.split())


def _looks_unsafe(value: str) -> bool:
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in (
            "raw model prompt",
            "user_input",
            "authorization:",
            "bearer ",
            "diagnosis",
            "medical record",
            "mrn",
            "ssn",
        )
    )


def _text_hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
