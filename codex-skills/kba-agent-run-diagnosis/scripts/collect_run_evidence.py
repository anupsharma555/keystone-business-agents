#!/usr/bin/env python3
"""Collect a bounded, read-only KBA run evidence packet from local state."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "keystone.kba_run_diagnosis.v1"
GENERIC_RESULTS = {
    "workitem command completed.",
    "business agents workitem ready",
}
SLACK_RUNTIME_SOURCE_PATHS = (
    "kni_integrations/slack_socket_mode.py",
    "kni_integrations/business_agents_bridge.py",
    "kni_integrations/business_agent_run_store.py",
    "kni_integrations/business_agent_contract.py",
    "scripts/run_slack_socket.sh",
)


def _clip(value: object, limit: int = 600) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _json(value: object, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _connect_read_only(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _parse_timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _collect_slack_runtime(
    slack_db: Path,
    *,
    health_path: Path | None = None,
    source_paths: Iterable[Path] = (),
) -> tuple[dict[str, Any], list[str]]:
    """Collect evidence that the long-lived Slack worker loaded current code.

    Healthy transport does not prove that edited Python modules were reloaded.
    A relevant source file newer than the latest worker hello is definitive
    stale-runtime evidence. The inverse is only ``no_staleness_proven`` because
    a websocket reconnect can refresh the hello time without restarting Python.
    """

    slack_repo = slack_db.parent.parent
    resolved_health_path = health_path or slack_repo / ".local" / "slack-socket-health.json"
    resolved_sources = list(source_paths) or [
        slack_repo / relative for relative in SLACK_RUNTIME_SOURCE_PATHS
    ]
    warnings: list[str] = []
    health: dict[str, Any] = {}
    if resolved_health_path.is_file():
        try:
            parsed = json.loads(resolved_health_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                health = parsed
            else:
                warnings.append("Slack runtime health evidence was not a JSON object.")
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(
                f"Slack runtime health evidence could not be read: {type(exc).__name__}."
            )
    else:
        warnings.append("Slack runtime health evidence was not found.")

    timestamps: list[tuple[Path, datetime]] = []
    for path in resolved_sources:
        try:
            timestamps.append(
                (
                    path,
                    datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
                )
            )
        except OSError:
            continue
    latest_source = max(timestamps, key=lambda item: item[1]) if timestamps else None
    last_hello = _parse_timestamp(health.get("last_hello_at"))
    newer_sources = [
        path
        for path, modified_at in timestamps
        if last_hello is not None and modified_at > last_hello
    ]
    if newer_sources:
        freshness = "stale_proven"
    elif health and timestamps and last_hello is not None:
        freshness = "no_staleness_proven"
    else:
        freshness = "unknown"

    return (
        {
            "health_path": str(resolved_health_path),
            "health_status": str(health.get("status") or "unknown"),
            "connected": health.get("connected"),
            "websocket_state": str(health.get("websocket_state") or ""),
            "last_hello_at": str(health.get("last_hello_at") or ""),
            "health_updated_at": str(health.get("updated_at") or ""),
            "freshness": freshness,
            "latest_source_mtime": (
                latest_source[1].isoformat() if latest_source is not None else ""
            ),
            "latest_source_path": (str(latest_source[0]) if latest_source is not None else ""),
            "sources_newer_than_last_hello": [str(path) for path in newer_sources],
            "freshness_note": (
                "Relevant Slack source changed after the running worker's latest "
                "hello; restart is required before live reproof."
                if freshness == "stale_proven"
                else "No stale source was proven, but process-start or loaded-revision "
                "evidence is still required to prove the worker is current."
                if freshness == "no_staleness_proven"
                else "Slack worker freshness could not be determined from local evidence."
            ),
        },
        warnings,
    )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _rows(
    connection: sqlite3.Connection, query: str, values: tuple[Any, ...]
) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, values).fetchall()]


def _nested_values(value: object, keys: set[str]) -> Iterable[tuple[str, Any]]:
    def walk(current: object, path: str) -> Iterable[tuple[str, Any]]:
        if isinstance(current, dict):
            for key, item in current.items():
                next_path = f"{path}.{key}" if path else key
                if key in keys:
                    yield next_path, item
                yield from walk(item, next_path)
        elif isinstance(current, list):
            for index, item in enumerate(current):
                yield from walk(item, f"{path}[{index}]")

    return walk(value, "")


def _first_dict(value: object, keys: set[str]) -> dict[str, Any]:
    for _path, item in _nested_values(value, keys):
        if isinstance(item, dict):
            return item
    return {}


def _unique(values: Iterable[object]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def _continuity_fields(request_text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for label, key in (
        ("Current user request (authoritative)", "current_request"),
        ("Provider affinity", "provider_affinity"),
        ("Previous request", "previous_request"),
        ("Previous result title", "previous_result_title"),
        ("Previous result", "previous_result"),
        ("User follow-up", "user_follow_up"),
    ):
        match = re.search(rf"(?m)^{re.escape(label)}:\s*(.+)$", request_text)
        if match:
            fields[key] = _clip(match.group(1))
    return fields


def _provider_names(payload: object) -> list[str]:
    """Collect bounded provider identities from retrieval/fallback telemetry."""

    names: list[object] = []
    keys = {
        "providers_used",
        "provider_attempts",
        "search_providers",
        "search_providers_attempted",
        "providers_attempted",
    }
    for _path, value in _nested_values(payload, keys):
        if isinstance(value, str):
            names.extend(re.split(r"[,+]", value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    names.append(item)
                elif isinstance(item, dict):
                    names.append(
                        item.get("provider") or item.get("name") or item.get("provider_name")
                    )
        elif isinstance(value, dict):
            if value.get("provider") or value.get("name"):
                names.append(value.get("provider") or value.get("name"))
            else:
                names.extend(value.keys())
    return _unique(names)


def _runtime_fingerprint_summary(payload: object) -> dict[str, Any]:
    """Return only the comparable, privacy-safe loaded-runtime evidence."""

    fingerprint = _first_dict(payload, {"runtime_fingerprint"})
    if not fingerprint:
        return {}
    hashes: dict[str, str] = {}
    for key in (
        "runtime_sha256",
        "source_sha256",
        "dependency_sha256",
        "config_sha256",
    ):
        value = str(fingerprint.get(key) or "").strip().lower()
        if re.fullmatch(r"[a-f0-9]{64}", value):
            hashes[key] = value
    return {
        "schema": str(fingerprint.get("schema") or "").strip(),
        **hashes,
        "source_file_count": fingerprint.get("source_file_count"),
        "source_read_error_count": fingerprint.get("source_read_error_count"),
        "dependency_file_count": fingerprint.get("dependency_file_count"),
        "config_key_count": fingerprint.get("config_key_count"),
        "python_runtime": str(fingerprint.get("python_runtime") or "").strip(),
        "process_started_at_utc": str(
            fingerprint.get("process_started_at_utc") or ""
        ).strip(),
    }


def _slack_record_summary(row: dict[str, Any]) -> dict[str, Any]:
    telemetry = _json(row.get("telemetry_json"), {})
    request_cache = telemetry.get("request_cache") if isinstance(telemetry, dict) else {}
    request_cache = request_cache if isinstance(request_cache, dict) else {}
    scope = request_cache.get("request_tool_scope")
    scope = scope if isinstance(scope, dict) else {}
    execution = telemetry.get("tool_execution") if isinstance(telemetry, dict) else {}
    execution = execution if isinstance(execution, dict) else {}
    usage = telemetry.get("usage") if isinstance(telemetry, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    cost = telemetry.get("cost") if isinstance(telemetry, dict) else {}
    cost = cost if isinstance(cost, dict) else {}
    request_text = str(row.get("request_text") or "")
    continuity = _continuity_fields(request_text)
    return {
        "run_id": row.get("run_id") or "",
        "channel_id": row.get("channel_id") or "",
        "thread_ts": row.get("thread_ts") or "",
        "request_ts": row.get("request_ts") or "",
        "route": row.get("route") or "",
        "status": row.get("status") or "",
        "work_item_id": row.get("work_item_id") or "",
        "request": _clip(continuity.get("current_request") or request_text, 1_000),
        "previous_request": continuity.get("previous_request") or "",
        "continuation_envelope_summary": (_clip(request_text, 1_000) if continuity else ""),
        "continuity": continuity,
        "result_title": _clip(row.get("result_title")),
        "result_text": _clip(row.get("result_text"), 1_000),
        "error": _clip(row.get("last_error") or row.get("error")),
        "created_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
        "tool_scope": {
            "agent_name": scope.get("agent_name") or "",
            "source": scope.get("source") or scope.get("effective_mode") or "",
            "candidate_count": scope.get("candidate_tool_count"),
            "selected_count": scope.get("selected_tool_count"),
            "selected_names": _unique(scope.get("selected_tool_names") or []),
            "omitted_count": scope.get("omitted_tool_count"),
            "notes": [_clip(note) for note in scope.get("notes") or []],
        },
        "tool_execution": {
            "mode": execution.get("mode") or "",
            "model_call_count": execution.get("model_tool_call_count"),
            "model_called_names": _unique(execution.get("model_called_tool_names") or []),
            "workflow_call_count": execution.get("workflow_tool_call_count"),
            "workflow_called_names": _unique(execution.get("workflow_called_tool_names") or []),
            "workflow_helper_count": execution.get("workflow_helper_call_count"),
            "workflow_helper_names": _unique(execution.get("workflow_called_helper_names") or []),
            "provider_request_attempt_count": execution.get("provider_request_attempt_count"),
            "provider_request_success_count": execution.get("provider_request_success_count"),
            "provider_receipt_count": execution.get("provider_receipt_count"),
            "context_receipt_count": execution.get("context_receipt_count"),
            "context_receipt_source": execution.get("context_receipt_source") or "",
        },
        "providers": _provider_names(telemetry),
        "receipts": _receipt_summaries(telemetry),
        "usage": {
            "requests": usage.get("requests"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "estimated_cost_usd": cost.get("estimated_usd", cost.get("amount_usd")),
        },
        "runtime_fingerprint": _runtime_fingerprint_summary(telemetry),
    }


def _collect_slack(
    database: Path,
    *,
    channel_id: str,
    thread_ts: str,
    request_ts: str,
    run_id: str,
    latest: bool,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[str]]:
    connection = _connect_read_only(database)
    if connection is None:
        return None, [], [f"Slack state database not found: {database}"]
    warnings: list[str] = []
    try:
        record_rows: list[dict[str, Any]] = []
        if _table_exists(connection, "business_agent_slack_run_records"):
            if run_id:
                record_rows = _rows(
                    connection,
                    "SELECT * FROM business_agent_slack_run_records WHERE run_id=? LIMIT 1",
                    (run_id,),
                )
            elif channel_id and thread_ts:
                record_rows = _rows(
                    connection,
                    "SELECT * FROM business_agent_slack_run_records "
                    "WHERE channel_id=? AND thread_ts=? LIMIT 1",
                    (channel_id, thread_ts),
                )
            elif request_ts:
                record_rows = _rows(
                    connection,
                    "SELECT * FROM business_agent_slack_run_records WHERE request_ts=? LIMIT 1",
                    (request_ts,),
                )
            elif latest:
                record_rows = _rows(
                    connection,
                    "SELECT * FROM business_agent_slack_run_records "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (),
                )
        record = _slack_record_summary(record_rows[0]) if record_rows else None
        if record is None:
            warnings.append("No matching business_agent_slack_run_records row was found.")
            return None, [], warnings

        attempts: list[dict[str, Any]] = []
        if _table_exists(connection, "business_agent_slack_runs"):
            attempt_columns = _table_columns(connection, "business_agent_slack_runs")
            attempt_order = (
                "request_ts ASC, created_at ASC, run_id ASC"
                if "request_ts" in attempt_columns
                else "created_at ASC, run_id ASC"
            )
            raw_attempts = _rows(
                connection,
                "SELECT * FROM business_agent_slack_runs "
                "WHERE channel_id=? AND thread_ts=? ORDER BY " + attempt_order,
                (record["channel_id"], record["thread_ts"]),
            )
            attempts = []
            for row in raw_attempts:
                summary = _slack_record_summary(row)
                summary["attempt_run_id"] = summary.pop("run_id", "")
                summary["retry_of_run_id"] = row.get("retry_of_run_id") or ""
                summary["resolved_request"] = _clip(row.get("resolved_request_text"))
                attempts.append(summary)
        return record, attempts, warnings
    finally:
        connection.close()


def _slack_identity_matches(payload: object, channel_id: str, thread_ts: str) -> bool:
    for _path, item in _nested_values(payload, {"slack_run_provenance", "slack_context"}):
        if not isinstance(item, dict):
            continue
        if channel_id and str(item.get("channel_id") or "") != channel_id:
            continue
        if thread_ts and str(item.get("thread_ts") or "") != thread_ts:
            continue
        return True
    return False


def _receipt_summaries(payload: object) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    keys = {"tool_receipt", "tool_receipts", "provider_receipt", "provider_receipts", "receipts"}
    for path, value in _nested_values(payload, keys):
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            verification = candidate.get("verification")
            verification = verification if isinstance(verification, dict) else {}
            summary = {
                "path": path,
                "provider": candidate.get("provider") or candidate.get("provider_system") or "",
                "operation": candidate.get("operation") or candidate.get("tool_name") or "",
                "status": candidate.get("status") or "",
                "provider_read": candidate.get("provider_read"),
                "provider_write": candidate.get("provider_write"),
                "verification_status": verification.get("status") or "",
                "verification_passed": verification.get("passed"),
                "event_count": len(candidate.get("events") or [])
                if isinstance(candidate.get("events"), list)
                else None,
                "source_count": len(candidate.get("sources") or [])
                if isinstance(candidate.get("sources"), list)
                else None,
            }
            if any(
                value not in ("", None, [], {}) for key, value in summary.items() if key != "path"
            ):
                receipts.append(summary)
    deduped: list[dict[str, Any]] = []
    fingerprints: set[str] = set()
    for receipt in receipts:
        fingerprint = json.dumps(receipt, sort_keys=True)
        if fingerprint not in fingerprints:
            fingerprints.add(fingerprint)
            deduped.append(receipt)
    return deduped[:30]


def _plan_summary(payload: object) -> dict[str, Any]:
    plan = _first_dict(payload, {"manual_request_plan"})
    if not plan:
        return {}
    ask_shape = plan.get("ask_shape")
    ask_shape = ask_shape if isinstance(ask_shape, dict) else {}
    return {
        "source": plan.get("source") or "",
        "requested_agent": plan.get("requested_agent") or "",
        "target_agent": plan.get("target_agent") or "",
        "intent": plan.get("intent") or "",
        "provider_system": plan.get("provider_system") or "",
        "provider_operations": _unique(plan.get("provider_operations") or []),
        "provider_read_scope": plan.get("provider_read_scope") or "",
        "provider_result_mode": plan.get("provider_result_mode") or "",
        "permission_state": ask_shape.get("permission_state") or "",
        "target_type": plan.get("target_type") or "",
        "primary_target": _clip(plan.get("primary_target")),
        "desired_count": plan.get("desired_count"),
        "requires_durable_state": plan.get("requires_durable_state"),
    }


def _agent_run_summary(row: dict[str, Any]) -> dict[str, Any]:
    payload = _json(row.get("output_json"), {})
    script_payload = payload.get("script_payload") if isinstance(payload, dict) else {}
    script_payload = script_payload if isinstance(script_payload, dict) else {}
    request_cache_candidates = (
        payload.get("request_cache") if isinstance(payload, dict) else None,
        script_payload.get("request_cache"),
    )
    request_cache = next(
        (
            candidate
            for candidate in request_cache_candidates
            if isinstance(candidate, dict) and candidate
        ),
        {},
    )
    tool_scope = request_cache.get("request_tool_scope")
    tool_scope = tool_scope if isinstance(tool_scope, dict) else {}
    tool_execution_candidates = (
        payload.get("tool_execution") if isinstance(payload, dict) else None,
        request_cache.get("tool_execution"),
        script_payload.get("tool_execution"),
        (
            script_payload.get("request_cache", {}).get("tool_execution")
            if isinstance(script_payload.get("request_cache"), dict)
            else None
        ),
    )
    tool_execution = next(
        (
            candidate
            for candidate in tool_execution_candidates
            if isinstance(candidate, dict) and candidate
        ),
        _first_dict(payload, {"tool_execution", "tool_execution_summary"}),
    )
    if not tool_scope and tool_execution.get("selected_tool_names"):
        tool_scope = {
            "source": tool_execution.get("scope_source") or "direct_terminal_execution",
            "selected_tool_count": tool_execution.get("selected_tool_count"),
            "selected_tool_names": tool_execution.get("selected_tool_names") or [],
        }
    preflight = _first_dict(payload, {"orchestrator_preflight"})
    route_result = preflight.get("route_result") if isinstance(preflight, dict) else {}
    route_result = route_result if isinstance(route_result, dict) else {}
    return {
        "id": row.get("id"),
        "agent_name": row.get("agent_name") or "",
        "status": row.get("status") or "",
        "model": row.get("model") or "",
        "dry_run": bool(row.get("dry_run")),
        "error": _clip(row.get("error")),
        "request": _clip(row.get("input_summary"), 1_000),
        "created_at_utc": row.get("created_at_utc") or row.get("created_at") or "",
        "created_at_et": row.get("created_at_et") or "",
        "plan": _plan_summary(payload),
        "preflight": {
            "requested_agent": preflight.get("requested_agent") or "",
            "selected_agent": preflight.get("selected_agent") or "",
            "execution_allowed": preflight.get("execution_allowed"),
            "route": route_result.get("route") or "",
            "blockers": [_clip(item) for item in preflight.get("blockers") or []],
        },
        "tool_scope": {
            "agent_name": tool_scope.get("agent_name") or "",
            "candidate_count": tool_scope.get("candidate_tool_count"),
            "selected_count": tool_scope.get("selected_tool_count"),
            "selected_names": _unique(tool_scope.get("selected_tool_names") or []),
            "notes": [_clip(note) for note in tool_scope.get("notes") or []],
        },
        "tool_execution": {
            "mode": tool_execution.get("mode") or "",
            "model_call_count": tool_execution.get("model_tool_call_count"),
            "model_called_names": _unique(tool_execution.get("model_called_tool_names") or []),
            "workflow_call_count": tool_execution.get("workflow_tool_call_count"),
            "workflow_called_names": _unique(
                tool_execution.get("workflow_called_tool_names") or []
            ),
            "workflow_helper_count": tool_execution.get("workflow_helper_call_count"),
            "workflow_helper_names": _unique(
                tool_execution.get("workflow_called_helper_names") or []
            ),
            "provider_request_attempt_count": tool_execution.get("provider_request_attempt_count"),
            "provider_request_attempt_count_available": tool_execution.get(
                "provider_request_attempt_count_available"
            ),
            "provider_request_success_count": tool_execution.get("provider_request_success_count"),
            "provider_request_success_count_available": tool_execution.get(
                "provider_request_success_count_available"
            ),
            "provider_receipt_count": tool_execution.get("provider_receipt_count"),
            "context_receipt_count": tool_execution.get("context_receipt_count"),
            "context_receipt_source": tool_execution.get("context_receipt_source") or "",
        },
        "providers": _provider_names(payload),
        "receipts": _receipt_summaries(payload),
        "result": {
            "status": payload.get("status") if isinstance(payload, dict) else "",
            "block_kind": payload.get("block_kind") if isinstance(payload, dict) else "",
            "human_summary": _clip(payload.get("human_summary"))
            if isinstance(payload, dict)
            else "",
        },
        "runtime_fingerprint": _runtime_fingerprint_summary(payload),
    }


def _collect_kba(
    database: Path,
    *,
    channel_id: str,
    thread_ts: str,
    agent_run_id: int | None,
    work_item_id: str,
    scan_limit: int,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any] | None,
    list[dict[str, Any]],
    list[str],
]:
    connection = _connect_read_only(database)
    if connection is None:
        return [], [], None, [], [f"KBA state database not found: {database}"]
    warnings: list[str] = []
    try:
        raw_runs: list[dict[str, Any]] = []
        if _table_exists(connection, "agent_runs"):
            if agent_run_id is not None:
                raw_runs = _rows(connection, "SELECT * FROM agent_runs WHERE id=?", (agent_run_id,))
            else:
                candidates = _rows(
                    connection,
                    "SELECT * FROM agent_runs ORDER BY id DESC LIMIT ?",
                    (scan_limit,),
                )
                for row in candidates:
                    payload = _json(row.get("output_json"), {})
                    if work_item_id and work_item_id in json.dumps(payload, sort_keys=True):
                        raw_runs.append(row)
                    elif (
                        channel_id
                        and thread_ts
                        and _slack_identity_matches(payload, channel_id, thread_ts)
                    ):
                        raw_runs.append(row)
        agent_runs = [_agent_run_summary(row) for row in reversed(raw_runs)]

        tool_events: list[dict[str, Any]] = []
        if _table_exists(connection, "tool_events"):
            run_ids = [str(row["id"]) for row in raw_runs if row.get("id") is not None]
            for run_id in run_ids:
                for row in _rows(
                    connection,
                    "SELECT id, tool_name, agent_name, run_id, status, error, "
                    "created_at_utc, created_at_et FROM tool_events "
                    "WHERE run_id=? ORDER BY id",
                    (run_id,),
                ):
                    row["error"] = _clip(row.get("error"))
                    tool_events.append(row)

        work_item: dict[str, Any] | None = None
        work_events: list[dict[str, Any]] = []
        if work_item_id and _table_exists(connection, "work_items"):
            found = _rows(connection, "SELECT * FROM work_items WHERE id=?", (work_item_id,))
            if found:
                row = found[0]
                work_item = {
                    "id": row.get("id") or "",
                    "kind": row.get("kind") or "",
                    "status": row.get("status") or "",
                    "title": _clip(row.get("title")),
                    "current_route": row.get("current_route") or "",
                    "last_agent": row.get("last_agent") or "",
                    "confidence": row.get("confidence"),
                    "created_at_utc": row.get("created_at_utc") or "",
                    "updated_at_utc": row.get("updated_at_utc") or "",
                }
            if _table_exists(connection, "work_item_events"):
                for row in _rows(
                    connection,
                    "SELECT id, event_type, actor, summary, metadata_json, "
                    "created_at_utc, created_at_et FROM work_item_events "
                    "WHERE work_item_id=? ORDER BY id",
                    (work_item_id,),
                ):
                    event_metadata = _json(row.get("metadata_json"), {})
                    event_metadata = event_metadata if isinstance(event_metadata, dict) else {}
                    work_events.append(
                        {
                            "id": row.get("id"),
                            "event_type": row.get("event_type") or "",
                            "actor": row.get("actor") or "",
                            "summary": _clip(row.get("summary")),
                            "metadata_keys": sorted(event_metadata),
                            "tool_execution": _first_dict(
                                event_metadata,
                                {"tool_execution", "tool_execution_summary"},
                            ),
                            "receipts": _receipt_summaries(event_metadata),
                            "providers": _provider_names(event_metadata),
                            "usage": _first_dict(event_metadata, {"usage"}),
                            "created_at_utc": row.get("created_at_utc") or "",
                            "created_at_et": row.get("created_at_et") or "",
                        }
                    )
        if not agent_runs and (agent_run_id is not None or channel_id or thread_ts or work_item_id):
            warnings.append("No KBA agent_runs row correlated with the supplied identity.")
        return agent_runs, tool_events, work_item, work_events, warnings
    finally:
        connection.close()


def _external_payloads(paths: list[Path]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            payloads.append({"path": str(path), "error": _clip(exc)})
            continue
        payloads.append(
            {
                "path": str(path),
                "plan": _plan_summary(payload),
                "receipts": _receipt_summaries(payload),
                "providers": _provider_names(payload),
                "tool_scope": _first_dict(payload, {"request_tool_scope"}),
                "tool_execution": _first_dict(
                    payload, {"tool_execution", "tool_execution_summary"}
                ),
                "runtime_fingerprint": _runtime_fingerprint_summary(payload),
            }
        )
    return payloads


def _collect_trace_events(
    database: Path,
    *,
    identities: Iterable[object],
    scan_limit: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    connection = _connect_read_only(database)
    if connection is None:
        return [], [f"Trace-summary database not found: {database}"]
    try:
        if not _table_exists(connection, "eval_trace_events"):
            return [], ["Trace-summary database has no eval_trace_events table."]
        needles = _unique(identities)
        if not needles:
            return [], []
        candidates = _rows(
            connection,
            "SELECT id, event_type, trace_id, span_id, parent_id, name, group_id, "
            "metadata_json, duration_ms, created_at FROM eval_trace_events "
            "ORDER BY id DESC LIMIT ?",
            (max(1, scan_limit),),
        )
        events: list[dict[str, Any]] = []
        for row in reversed(candidates):
            searchable = " ".join(
                str(row.get(key) or "")
                for key in ("trace_id", "span_id", "parent_id", "group_id", "metadata_json")
            )
            if not any(needle in searchable for needle in needles):
                continue
            metadata = _json(row.get("metadata_json"), {})
            metadata = metadata if isinstance(metadata, dict) else {}
            events.append(
                {
                    "id": row.get("id"),
                    "event_type": row.get("event_type") or "",
                    "trace_id": row.get("trace_id") or "",
                    "name": row.get("name") or "",
                    "group_id": row.get("group_id") or "",
                    "duration_ms": row.get("duration_ms"),
                    "created_at": row.get("created_at") or "",
                    "agent": metadata.get("agent_name") or metadata.get("agent") or "",
                    "route": metadata.get("route") or "",
                    "stage": metadata.get("stage") or "",
                    "status": metadata.get("status") or "",
                    "failure_kind": metadata.get("failure_kind") or "",
                    "tool_execution": _first_dict(
                        metadata,
                        {"tool_execution", "tool_execution_summary"},
                    ),
                    "receipts": _receipt_summaries(metadata),
                    "providers": _provider_names(metadata),
                    "usage": _first_dict(metadata, {"usage"}),
                    "metadata_keys": sorted(metadata),
                }
            )
        return events, []
    finally:
        connection.close()


def _execution_called_names(execution: object) -> list[str]:
    if not isinstance(execution, dict):
        return []
    return _unique(
        [
            *(execution.get("model_called_names") or []),
            *(execution.get("model_called_tool_names") or []),
            *(execution.get("workflow_called_names") or []),
            *(execution.get("workflow_called_tool_names") or []),
            *(execution.get("workflow_helper_names") or []),
            *(execution.get("workflow_called_helper_names") or []),
        ]
    )


def _scope_selected_names(scope: object) -> list[str]:
    if not isinstance(scope, dict):
        return []
    return _unique(
        [
            *(scope.get("selected_names") or []),
            *(scope.get("selected_tool_names") or []),
        ]
    )


def _derive_findings(
    report: dict[str, Any], args: argparse.Namespace
) -> tuple[list[dict[str, str]], list[str]]:
    findings: list[dict[str, str]] = []
    gaps: list[str] = []
    slack = report.get("slack_record") or {}
    slack_runtime = report.get("slack_runtime") or {}
    agent_runs = report.get("agent_runs") or []
    slack_fingerprint = slack.get("runtime_fingerprint") or {}
    agent_fingerprints = [
        run.get("runtime_fingerprint") or {}
        for run in agent_runs
        if run.get("runtime_fingerprint")
    ]
    observed_runtime_hashes = _unique(
        [
            slack_fingerprint.get("runtime_sha256"),
            *(item.get("runtime_sha256") for item in agent_fingerprints),
        ]
    )
    if len(observed_runtime_hashes) > 1:
        findings.append(
            {
                "code": "runtime_fingerprint_mismatch",
                "layer": "runtime_deployment_alignment",
                "detail": (
                    "The Slack attempt and correlated KBA child records report different "
                    "loaded code/config fingerprints."
                ),
            }
        )
    elif slack and not slack_fingerprint and not agent_fingerprints:
        gaps.append(
            "No privacy-safe KBA loaded-runtime fingerprint was correlated with this Slack run."
        )
    if slack_runtime.get("freshness") == "stale_proven":
        findings.append(
            {
                "code": "slack_runtime_stale",
                "layer": "runtime_deployment_alignment",
                "detail": (
                    "Relevant Slack source changed after the running worker's latest "
                    "hello, so the live run may have used older dispatch, persistence, "
                    "or rendering code."
                ),
            }
        )
    if slack_runtime and (
        slack_runtime.get("connected") is False
        or slack_runtime.get("health_status") not in {"", "healthy", "unknown"}
    ):
        findings.append(
            {
                "code": "slack_runtime_unhealthy",
                "layer": "runtime_deployment_alignment",
                "detail": "Slack runtime health did not show a healthy connected worker.",
            }
        )
    if slack and slack_runtime.get("freshness") == "unknown":
        gaps.append(
            "Slack process-start or loaded-revision evidence was unavailable, so runtime "
            "freshness could not be verified."
        )
    evidence_surfaces = [
        slack,
        *(report.get("slack_attempts") or []),
        *agent_runs,
        *(report.get("work_item_events") or []),
        *(report.get("trace_events") or []),
        *(report.get("external_payloads") or []),
    ]
    selected = _unique(
        name
        for surface in evidence_surfaces
        for name in _scope_selected_names(surface.get("tool_scope") or {})
    )
    called = _unique(
        [
            *[
                name
                for surface in evidence_surfaces
                for name in _execution_called_names(surface.get("tool_execution") or {})
            ],
            *[event.get("tool_name") for event in report.get("tool_events") or []],
        ]
    )
    receipts = [
        *(slack.get("receipts") or []),
        *[
            receipt
            for attempt in report.get("slack_attempts") or []
            for receipt in attempt.get("receipts") or []
        ],
        *[receipt for run in agent_runs for receipt in run.get("receipts") or []],
        *[
            receipt
            for payload in report.get("external_payloads") or []
            for receipt in payload.get("receipts") or []
        ],
        *[
            receipt
            for event in report.get("work_item_events") or []
            for receipt in event.get("receipts") or []
        ],
        *[
            receipt
            for event in report.get("trace_events") or []
            for receipt in event.get("receipts") or []
        ],
    ]
    reported_receipt_counts = [
        (surface.get("tool_execution") or {}).get("provider_receipt_count")
        for surface in evidence_surfaces
        if isinstance((surface.get("tool_execution") or {}).get("provider_receipt_count"), int)
    ]
    receipt_count = max([len(receipts)] + reported_receipt_counts)
    continuity = slack.get("continuity") or {}
    actual_provider = (
        continuity.get("provider_affinity")
        or next(iter(slack.get("providers") or []), "")
        or next(
            (
                str((run.get("plan") or {}).get("provider_system") or "")
                for run in agent_runs
                if (run.get("plan") or {}).get("provider_system")
            ),
            "",
        )
    )
    expected_providers = _unique(args.expect_provider)
    if selected and not called and receipt_count == 0:
        findings.append(
            {
                "code": "tools_attached_without_execution_evidence",
                "layer": "model_tool_selection_or_workflow_execution",
                "detail": (
                    "Tools were attached, but no model call, workflow tool event, "
                    "or provider receipt was correlated."
                ),
            }
        )
    if actual_provider and actual_provider not in {"unspecified", "none"} and receipt_count == 0:
        findings.append(
            {
                "code": "provider_verification_missing",
                "layer": "provider_or_receipt_capture",
                "detail": (
                    f"The run had {actual_provider} affinity but no correlated provider receipt."
                ),
            }
        )
    result_text = str(slack.get("result_text") or "").strip().lower()
    result_title = str(slack.get("result_title") or "").strip().lower()
    if result_text in GENERIC_RESULTS or result_title in GENERIC_RESULTS:
        findings.append(
            {
                "code": "generic_renderer_fallthrough",
                "layer": "renderer_or_bridge",
                "detail": (
                    "The Slack bridge emitted a generic completion instead of a "
                    "request-specific result or blocker."
                ),
            }
        )
    if (
        continuity
        and slack.get("request_ts")
        and slack.get("thread_ts")
        and slack["request_ts"] != slack["thread_ts"]
        and not report.get("slack_attempts")
    ):
        findings.append(
            {
                "code": "mutable_thread_record",
                "layer": "continuation_or_persistence",
                "detail": (
                    "The latest thread projection represents a follow-up, but no "
                    "append-only attempt history was retained."
                ),
            }
        )
        if not report.get("slack_attempts"):
            gaps.append("The original Slack attempt is not retained as a separate local run row.")
    if slack and not agent_runs:
        gaps.append("No KBA agent run is durably correlated to the Slack thread/run identity.")
    if slack and not report.get("trace_events"):
        gaps.append(
            "No durable trace-summary event is correlated to the Slack run or thread identity."
        )
    if (actual_provider or expected_providers) and receipt_count == 0:
        gaps.append(
            "Without a durable provider receipt, provider non-execution cannot be "
            "distinguished from receipt loss."
        )

    expected_agents = _unique(args.expect_agent)
    actual_agents = _unique(
        [slack.get("route"), (slack.get("tool_scope") or {}).get("agent_name")]
        + [run.get("agent_name") for run in agent_runs]
        + [(run.get("preflight") or {}).get("selected_agent") for run in agent_runs]
    )
    for expected in expected_agents:
        if expected not in actual_agents:
            findings.append(
                {
                    "code": "expected_agent_missing",
                    "layer": "semantic_interpretation_or_ownership",
                    "detail": (
                        f"Expected agent {expected!r} was not found in the correlated run evidence."
                    ),
                }
            )
    actual_providers = _unique(
        [continuity.get("provider_affinity")]
        + [provider for surface in evidence_surfaces for provider in surface.get("providers") or []]
        + [(run.get("plan") or {}).get("provider_system") for run in agent_runs]
        + [receipt.get("provider") for receipt in receipts]
    )
    for expected in expected_providers:
        if expected not in actual_providers:
            findings.append(
                {
                    "code": "expected_provider_missing",
                    "layer": "semantic_interpretation_or_provider_selection",
                    "detail": (
                        f"Expected provider {expected!r} was not found in the "
                        "correlated run evidence."
                    ),
                }
            )
    for expected in _unique(args.expect_attached_tool):
        if expected not in selected:
            findings.append(
                {
                    "code": "expected_tool_not_attached",
                    "layer": "request_scoped_tool_admission",
                    "detail": (
                        f"Expected attached tool {expected!r} was absent from request "
                        "scope evidence."
                    ),
                }
            )
    for expected in _unique(args.expect_called_tool):
        if expected not in called:
            findings.append(
                {
                    "code": "expected_tool_not_called",
                    "layer": "model_tool_selection_or_workflow_execution",
                    "detail": (
                        f"Expected called tool {expected!r} was absent from call/"
                        "completion evidence."
                    ),
                }
            )
    receipt_operations = _unique(receipt.get("operation") for receipt in receipts)
    for expected in _unique(args.expect_receipt_operation):
        if expected not in receipt_operations:
            findings.append(
                {
                    "code": "expected_receipt_missing",
                    "layer": "provider_or_receipt_capture",
                    "detail": f"Expected receipt operation {expected!r} was not found.",
                }
            )
            gaps.append(
                f"No durable receipt recorded the expected provider operation {expected!r}."
            )
    return findings, _unique(gaps)


def _current_terminal_status(report: dict[str, Any]) -> str:
    """Return the newest authoritative status without scanning historical failures."""

    slack = report.get("slack_record") or {}
    if str(slack.get("status") or "").strip():
        return str(slack["status"]).strip().lower()
    attempts = report.get("slack_attempts") or []
    if attempts and str(attempts[-1].get("status") or "").strip():
        return str(attempts[-1]["status"]).strip().lower()
    agent_runs = report.get("agent_runs") or []
    if agent_runs and str(agent_runs[-1].get("status") or "").strip():
        return str(agent_runs[-1]["status"]).strip().lower()
    work_item = report.get("work_item") or {}
    return str(work_item.get("status") or "").strip().lower()


def _verdict(report: dict[str, Any]) -> str:
    current_status = _current_terminal_status(report)
    if current_status in {
        "cancelled",
        "canceled",
        "error",
        "failed",
        "failure",
        "incomplete",
        "timed_out",
        "timeout",
    }:
        return "FAIL"
    codes = {finding["code"] for finding in report.get("findings") or []}
    if codes.intersection(
        {
            "expected_agent_missing",
            "expected_provider_missing",
            "expected_tool_not_attached",
            "expected_tool_not_called",
            "expected_receipt_missing",
            "generic_renderer_fallthrough",
            "slack_runtime_stale",
            "slack_runtime_unhealthy",
            "runtime_fingerprint_mismatch",
        }
    ):
        return "FAIL"
    if report.get("findings") or report.get("evidence_gaps") or report.get("warnings"):
        return "PARTIAL"
    if current_status and current_status not in {
        "complete",
        "completed",
        "pass",
        "passed",
        "success",
        "succeeded",
    }:
        return "PARTIAL"
    return "PASS"


def _tool_execution_evidence_score(execution: object) -> tuple[int, int, int, int]:
    """Prefer the surface with actual calls and receipts over an empty wrapper."""

    if not isinstance(execution, dict):
        return (0, 0, 0, 0)
    called = _execution_called_names(execution)
    receipt_count = execution.get("provider_receipt_count")
    receipt_count = receipt_count if isinstance(receipt_count, int) else 0
    attempt_count = execution.get("provider_request_attempt_count")
    attempt_count = attempt_count if isinstance(attempt_count, int) else 0
    mode = str(execution.get("mode") or "").strip()
    return (
        len(called),
        receipt_count,
        attempt_count,
        int(bool(mode) and mode not in {"tool_free", "model_tools_attached_no_call"}),
    )


def _markdown(report: dict[str, Any]) -> str:
    slack = report.get("slack_record") or {}
    direct = (report.get("agent_runs") or [{}])[-1]
    runtime = report.get("slack_runtime") or {}
    runtime_fingerprint = (
        slack.get("runtime_fingerprint")
        or direct.get("runtime_fingerprint")
        or {}
    )
    channel_thread = (
        f"`{slack.get('channel_id') or 'unknown'}` / `{slack.get('thread_ts') or 'unknown'}`"
    )
    route_status = (
        f"`{slack.get('route') or direct.get('agent_name') or 'unknown'}` / "
        f"`{slack.get('status') or direct.get('status') or 'unknown'}`"
    )
    lines = [
        f"# KBA run diagnosis: {report['verdict']}",
        "",
        "## Run details",
        "",
        f"- Slack run: `{slack.get('run_id') or 'not found'}`",
        f"- Channel/thread: {channel_thread}",
        f"- Request timestamp: `{slack.get('request_ts') or 'unknown'}`",
        f"- Route/status: {route_status}",
        f"- WorkItem: `{slack.get('work_item_id') or 'none'}`",
        f"- Request: {slack.get('request') or direct.get('request') or 'not available'}",
        f"- Slack runtime: `{runtime.get('health_status') or 'unknown'}` / "
        f"freshness `{runtime.get('freshness') or 'unknown'}`",
        "- KBA child fingerprint: `"
        + str(runtime_fingerprint.get("runtime_sha256") or "not recorded")[:16]
        + "`; process `"
        + str(runtime_fingerprint.get("process_started_at_utc") or "unknown")
        + "`",
        "",
        "## Agents and state",
        "",
    ]
    if report.get("agent_runs"):
        for run in report["agent_runs"]:
            plan = run.get("plan") or {}
            run_identity = f"`{run.get('id')}`: `{run.get('agent_name')}` / `{run.get('status')}`"
            lines.append(
                f"- Agent run {run_identity}; intent "
                f"`{plan.get('intent') or 'unknown'}`, provider "
                f"`{plan.get('provider_system') or 'unknown'}`."
            )
    else:
        lines.append("- No correlated KBA agent-run row was found.")
    if slack.get("continuity"):
        if slack.get("previous_request"):
            lines.append(f"- Previous request: {slack['previous_request']}")
        lines.append(
            f"- Continuation envelope: `{', '.join(sorted(slack['continuity']))}` fields retained."
        )
    if runtime:
        lines.append(f"- Runtime evidence: {runtime.get('freshness_note') or 'not available'}")
        if runtime.get("sources_newer_than_last_hello"):
            lines.append(
                "- Slack sources newer than worker hello: `"
                + "`, `".join(runtime["sources_newer_than_last_hello"])
                + "`"
            )

    scope = slack.get("tool_scope") or direct.get("tool_scope") or {}
    execution = max(
        (
            slack.get("tool_execution") or {},
            direct.get("tool_execution") or {},
        ),
        key=_tool_execution_evidence_score,
    )
    candidate_attached = f"`{scope.get('candidate_count')}` / `{scope.get('selected_count')}`"
    attached_tools = ", ".join(scope.get("selected_names") or []) or "none recorded"
    model_tools = ", ".join(execution.get("model_called_names") or []) or "none recorded"
    workflow_tools = ", ".join(execution.get("workflow_called_names") or []) or "none recorded"
    workflow_helpers = ", ".join(execution.get("workflow_helper_names") or []) or "none recorded"
    provider_attempts = (
        execution.get("provider_request_attempt_count")
        if execution.get("provider_request_attempt_count_available") is not False
        else None
    )
    provider_successes = (
        execution.get("provider_request_success_count")
        if execution.get("provider_request_success_count_available") is not False
        else None
    )
    provider_receipts = execution.get("provider_receipt_count")
    receipt_rows = slack.get("receipts") or direct.get("receipts") or []
    receipt_operations = ", ".join(
        ":".join(
            part
            for part in (
                str(receipt.get("provider") or "").strip(),
                str(receipt.get("operation") or "").strip(),
            )
            if part
        )
        for receipt in receipt_rows
        if receipt.get("provider") or receipt.get("operation")
    ) or "none recorded"
    provider_read_proof = any(
        receipt.get("provider_read") is True for receipt in receipt_rows
    )
    provider_write_proof = any(
        receipt.get("provider_write") is True for receipt in receipt_rows
    )
    providers_observed = (
        ", ".join(
            slack.get("providers")
            or direct.get("providers")
            or [str((direct.get("plan") or {}).get("provider_system") or "")]
        ).strip(", ")
        or "none recorded"
    )
    lines.extend(
        [
            "",
            "## Tools and provider evidence",
            "",
            f"- Candidate/attached: {candidate_attached}",
            f"- Attached tools: `{attached_tools}`",
            f"- Model-called tools: `{model_tools}`",
            f"- Workflow-called tools: `{workflow_tools}`",
            f"- Workflow helpers: `{workflow_helpers}`",
            "- Provider attempts/successes: "
            f"`{provider_attempts if provider_attempts is not None else 'unknown'}` / "
            f"`{provider_successes if provider_successes is not None else 'unknown'}`",
            f"- Providers observed: `{providers_observed}`",
            f"- Receipt operations: `{receipt_operations}`",
            "- Receipt access proof: "
            f"read=`{provider_read_proof}`, write=`{provider_write_proof}`",
            "- Provider receipt count: "
            f"`{provider_receipts if provider_receipts is not None else 'unknown'}`",
            f"- Local tool events: `{len(report.get('tool_events') or [])}`",
            f"- Correlated trace events: `{len(report.get('trace_events') or [])}`",
            "",
            "## Findings",
            "",
        ]
    )
    if report.get("findings"):
        for finding in report["findings"]:
            lines.append(f"- **{finding['layer']}** — {finding['detail']} (`{finding['code']}`)")
    else:
        lines.append("- No contradiction was detected in the collected local evidence.")
    lines.extend(["", "## Missing evidence", ""])
    if report.get("evidence_gaps"):
        lines.extend(f"- {gap}" for gap in report["evidence_gaps"])
    else:
        lines.append("- No evidence gap was detected by the collector.")
    if report.get("warnings"):
        lines.extend(["", "## Collection warnings", ""])
        lines.extend(f"- {warning}" for warning in report["warnings"])
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    repo = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--slack-db",
        type=Path,
        default=repo.parent / "keystone-slack" / ".local" / "workflow-state.sqlite",
    )
    parser.add_argument("--slack-health", type=Path)
    parser.add_argument("--slack-runtime-source", type=Path, action="append", default=[])
    parser.add_argument("--kba-db", type=Path, default=repo / "keystone_agents.db")
    parser.add_argument(
        "--trace-db",
        type=Path,
        default=repo / ".keystone" / "promptfoo" / "human-reviews.sqlite",
    )
    parser.add_argument("--channel-id", default="")
    parser.add_argument("--thread-ts", default="")
    parser.add_argument("--request-ts", default="")
    parser.add_argument("--slack-run-id", default="")
    parser.add_argument("--work-item-id", default="")
    parser.add_argument("--agent-run-id", type=int)
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--payload-json", type=Path, action="append", default=[])
    parser.add_argument("--expect-agent", action="append", default=[])
    parser.add_argument("--expect-provider", action="append", default=[])
    parser.add_argument("--expect-attached-tool", action="append", default=[])
    parser.add_argument("--expect-called-tool", action="append", default=[])
    parser.add_argument("--expect-receipt-operation", action="append", default=[])
    parser.add_argument("--scan-limit", type=int, default=1_000)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not any(
        (
            args.channel_id and args.thread_ts,
            args.request_ts,
            args.slack_run_id,
            args.work_item_id,
            args.agent_run_id is not None,
            args.latest,
            args.payload_json,
        )
    ):
        parser.error(
            "provide a run/thread/WorkItem/agent-run selector, --latest, or --payload-json"
        )

    slack, attempts, slack_warnings = _collect_slack(
        args.slack_db,
        channel_id=args.channel_id,
        thread_ts=args.thread_ts,
        request_ts=args.request_ts,
        run_id=args.slack_run_id,
        latest=args.latest,
    )
    slack_runtime, runtime_warnings = _collect_slack_runtime(
        args.slack_db,
        health_path=args.slack_health,
        source_paths=args.slack_runtime_source,
    )
    channel_id = str((slack or {}).get("channel_id") or args.channel_id)
    thread_ts = str((slack or {}).get("thread_ts") or args.thread_ts)
    work_item_id = str(args.work_item_id or (slack or {}).get("work_item_id") or "")
    agent_runs, tool_events, work_item, work_events, kba_warnings = _collect_kba(
        args.kba_db,
        channel_id=channel_id,
        thread_ts=thread_ts,
        agent_run_id=args.agent_run_id,
        work_item_id=work_item_id,
        scan_limit=max(1, args.scan_limit),
    )
    trace_events, trace_warnings = _collect_trace_events(
        args.trace_db,
        identities=(
            args.slack_run_id,
            (slack or {}).get("run_id"),
            thread_ts,
            args.request_ts,
            (slack or {}).get("request_ts"),
            work_item_id,
            args.agent_run_id,
        ),
        scan_limit=max(1, args.scan_limit),
    )
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "sources": {
            "slack_database": str(args.slack_db),
            "slack_health": str(args.slack_health or slack_runtime.get("health_path") or ""),
            "kba_database": str(args.kba_db),
            "trace_database": str(args.trace_db),
            "payload_json": [str(path) for path in args.payload_json],
        },
        "slack_record": slack,
        "slack_attempts": attempts,
        "slack_runtime": slack_runtime,
        "agent_runs": agent_runs,
        "tool_events": tool_events,
        "work_item": work_item,
        "work_item_events": work_events,
        "trace_events": trace_events,
        "external_payloads": _external_payloads(args.payload_json),
        "warnings": [
            *slack_warnings,
            *runtime_warnings,
            *kba_warnings,
            *trace_warnings,
        ],
    }
    report["findings"], report["evidence_gaps"] = _derive_findings(report, args)
    report["verdict"] = _verdict(report)
    if args.format == "markdown":
        print(_markdown(report), end="")
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
