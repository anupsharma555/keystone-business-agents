"""Local eval database linking Promptfoo results, Slack runs, and human scores."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from promptfoo.human_review import (
    DEFAULT_REVIEW_DB,
    REVIEW_KIND_HUMAN,
    REVIEW_KIND_ORCHESTRATOR_JUDGE,
    HumanEvalReview,
    ensure_human_review_schema,
)

DEFAULT_EVAL_DB = DEFAULT_REVIEW_DB
DEFAULT_EVAL_SLACK_CHANNEL_ID = "C0BA17Y9C01"
DEFAULT_EVAL_SLACK_CHANNEL_NAME = "evals"
DATABASE_TABLE_SUMMARIES: tuple[tuple[str, str], ...] = (
    ("promptfoo_eval_runs", "imported_at"),
    ("promptfoo_case_results", "imported_at"),
    ("slack_eval_runs", "created_at"),
    ("human_eval_reviews", "created_at"),
    ("eval_trace_events", "created_at"),
)
DATABASE_TABLE_LATEST_COLUMNS: dict[str, tuple[str, ...]] = {
    "promptfoo_eval_runs": (
        "eval_id",
        "created_at",
        "imported_at",
        "total",
        "successes",
        "failures",
        "errors",
    ),
    "promptfoo_case_results": (
        "id",
        "eval_id",
        "case_id",
        "result_id",
        "agent_under_test",
        "success",
        "score",
        "imported_at",
    ),
    "slack_eval_runs": (
        "id",
        "case_id",
        "run_id",
        "work_item_id",
        "agent",
        "route",
        "status",
        "slack_thread_ts",
        "warning_count",
        "created_at",
    ),
    "human_eval_reviews": (
        "id",
        "case_id",
        "run_id",
        "agent",
        "reviewer",
        "review_kind",
        "average_score",
        "total_score",
        "safety",
        "slack_thread_ts",
        "created_at",
    ),
    "eval_trace_events": (
        "id",
        "event_type",
        "trace_id",
        "span_id",
        "group_id",
        "name",
        "duration_ms",
        "created_at",
    ),
}
_AGENT_PREFIXES = (
    "business research analyst",
    "business_research_analyst",
    "opportunity scout",
    "opportunity_scout",
    "chief of staff",
    "chief_of_staff",
    "gmail triage",
    "gmail_triage",
    "outreach composer",
    "outreach_composer",
    "orchestrator agent",
    "orchestrator",
)


def database_table_summaries(database_path: str | Path = DEFAULT_EVAL_DB) -> dict[str, dict[str, Any]]:
    """Return cheap row counts and freshness signals for dashboard health checks."""

    summaries: dict[str, dict[str, Any]] = {
        table_name: {
            "exists": False,
            "row_count": 0,
            "latest_at": "",
            "timestamp_column": timestamp_column,
            "latest": {},
        }
        for table_name, timestamp_column in DATABASE_TABLE_SUMMARIES
    }
    db_path = Path(database_path)
    if not db_path.exists():
        return summaries
    try:
        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            for table_name, timestamp_column in DATABASE_TABLE_SUMMARIES:
                try:
                    row = connection.execute(
                        f"""
                        SELECT COUNT(*) AS row_count, MAX(NULLIF({timestamp_column}, '')) AS latest_at
                        FROM {table_name}
                        """
                    ).fetchone()
                except sqlite3.OperationalError:
                    continue
                summaries[table_name] = {
                    "exists": True,
                    "row_count": int(row[0] or 0) if row else 0,
                    "latest_at": str(row["latest_at"] or "") if row else "",
                    "timestamp_column": timestamp_column,
                    "latest": _latest_table_summary_row(
                        connection,
                        table_name=table_name,
                        timestamp_column=timestamp_column,
                    ),
                }
    except sqlite3.DatabaseError:
        return summaries
    return summaries


def _latest_table_summary_row(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    timestamp_column: str,
) -> dict[str, Any]:
    columns = {
        str(row["name"] or "")
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    selected_columns = [
        column
        for column in DATABASE_TABLE_LATEST_COLUMNS.get(table_name, ())
        if column in columns
    ]
    if not selected_columns or timestamp_column not in columns:
        return {}
    select_clause = ", ".join(selected_columns)
    try:
        row = connection.execute(
            f"""
            SELECT {select_clause}
            FROM {table_name}
            ORDER BY NULLIF({timestamp_column}, '') DESC, rowid DESC
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if row is None:
        return {}
    return {
        column: _summary_scalar(row[column])
        for column in selected_columns
        if row[column] not in (None, "")
    }


def _summary_scalar(value: Any) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass(frozen=True)
class PromptfooImportSummary:
    eval_id: str
    total: int
    successes: int
    failures: int
    errors: int
    database_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "eval_id": self.eval_id,
            "total": self.total,
            "successes": self.successes,
            "failures": self.failures,
            "errors": self.errors,
            "database_path": self.database_path,
        }


def import_promptfoo_results(
    results_path: str | Path,
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
) -> PromptfooImportSummary:
    """Import a Promptfoo JSON result file into the local eval database."""

    path = Path(results_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    results_block = payload.get("results") if isinstance(payload, dict) else {}
    if not isinstance(results_block, dict):
        raise ValueError("Promptfoo output does not contain a results object")

    eval_id = str(
        payload.get("evalId")
        or results_block.get("evalId")
        or results_block.get("timestamp")
        or path.stem
    )
    stats = results_block.get("stats") if isinstance(results_block.get("stats"), dict) else {}
    case_results = (
        results_block.get("results") if isinstance(results_block.get("results"), list) else []
    )
    score_summary = _promptfoo_score_summary(case_results)
    timestamp = str(results_block.get("timestamp") or payload.get("timestamp") or _now())
    provenance = _promptfoo_run_provenance(payload, case_results, result_path=path)

    db_path = Path(database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_eval_schema(connection)
        connection.execute(
            """
            INSERT OR REPLACE INTO promptfoo_eval_runs (
                eval_id, result_path, created_at, imported_at, total, successes,
                failures, errors, stats_json, average_score, agent_scores_json,
                prompt_versions_json, prompt_metadata_json, prompt_metadata_hash,
                model_provider, model_name, run_mode, search_provider,
                search_provider_sequence_json, git_revision, run_label
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                eval_id,
                str(path),
                timestamp,
                _now(),
                len(case_results),
                int(stats.get("successes") or 0),
                int(stats.get("failures") or 0),
                int(stats.get("errors") or 0),
                json.dumps(stats, ensure_ascii=True, sort_keys=True),
                score_summary["average_score"],
                json.dumps(score_summary["agent_scores"], ensure_ascii=True, sort_keys=True),
                json.dumps(provenance["prompt_versions"], ensure_ascii=True, sort_keys=True),
                json.dumps(provenance["prompt_metadata"], ensure_ascii=True, sort_keys=True),
                provenance["prompt_metadata_hash"],
                provenance["model_provider"],
                provenance["model_name"],
                provenance["run_mode"],
                provenance["search_provider"],
                json.dumps(provenance["search_provider_sequence"], ensure_ascii=True, sort_keys=True),
                provenance["git_revision"],
                provenance["run_label"],
            ),
        )
        connection.execute("DELETE FROM promptfoo_case_results WHERE eval_id = ?", (eval_id,))
        for item in case_results:
            if isinstance(item, dict):
                _insert_promptfoo_case_result(connection, eval_id, item, provenance)
        connection.commit()

    return PromptfooImportSummary(
        eval_id=eval_id,
        total=len(case_results),
        successes=int(stats.get("successes") or 0),
        failures=int(stats.get("failures") or 0),
        errors=int(stats.get("errors") or 0),
        database_path=str(db_path),
    )


def record_slack_eval_run(
    *,
    case_id: str,
    run_id: str = "",
    agent: str = "",
    work_item_id: str = "",
    slack_channel_id: str = "C0BA17Y9C01",
    slack_channel_name: str = "evals",
    slack_thread_ts: str = "",
    permalink: str = "",
    request_text: str = "",
    result_summary: str = "",
    route: str = "",
    status: str = "",
    context_policy: str = "",
    thread_fetch_status: str = "",
    thread_message_count: int = 0,
    warning_count: int = 0,
    warnings: list[str] | None = None,
    cost_profile: str = "",
    source_count: int = 0,
    visible_source_count: int = 0,
    sdk_estimated_cost_usd: float | None = None,
    sdk_cache_hit_rate: float | None = None,
    duration_ms: float | None = None,
    response_hash: str = "",
    evidence: dict[str, Any] | None = None,
    prompt_versions: list[Any] | None = None,
    prompt_metadata: dict[str, Any] | None = None,
    model_provider: str = "",
    model_name: str = "",
    run_mode: str = "",
    search_provider: str = "",
    search_provider_sequence: list[str] | None = None,
    git_revision: str = "",
    run_label: str = "",
    storage_mode: str = "",
    database_path: str | Path = DEFAULT_EVAL_DB,
) -> int:
    """Record a real Slack agent run for a Promptfoo-linked eval case."""

    normalized_case_id = str(case_id or "").strip()
    if not normalized_case_id:
        raise ValueError("case_id is required")
    db_path = Path(database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_eval_schema(connection)
        normalized_evidence = _with_business_state_database_evidence(evidence or {})
        normalized_storage_mode = _run_storage_mode(
            storage_mode=storage_mode,
            run_mode=run_mode,
            evidence=normalized_evidence,
        )
        raw_request_text = str(request_text or "").strip()
        raw_result_summary = str(result_summary or "").strip()
        stored_request_text = (
            _redacted_prompt_summary(raw_request_text)
            if normalized_storage_mode == "api_redacted"
            else raw_request_text
        )
        stored_result_summary = (
            _redact_eval_text(raw_result_summary, max_chars=500)
            if normalized_storage_mode == "api_redacted"
            else raw_result_summary
        )
        _upsert_eval_case(
            connection,
            case_id=normalized_case_id,
            agent_under_test=agent,
            user_input=stored_request_text,
            source="slack",
        )
        normalized_prompt_metadata = prompt_metadata or {}
        prompt_metadata_hash = _metadata_hash(normalized_prompt_metadata)
        row_values = {
            "case_id": normalized_case_id,
            "run_id": str(run_id or "").strip(),
            "agent": str(agent or "").strip(),
            "slack_channel_id": str(slack_channel_id or "").strip(),
            "slack_channel_name": str(slack_channel_name or "").strip(),
            "slack_thread_ts": str(slack_thread_ts or "").strip(),
            "permalink": str(permalink or "").strip(),
            "request_text": stored_request_text,
            "result_summary": stored_result_summary,
            "work_item_id": str(work_item_id or run_id or "").strip(),
            "route": str(route or agent or "").strip(),
            "status": str(status or "").strip(),
            "context_policy": str(context_policy or "").strip(),
            "thread_fetch_status": str(thread_fetch_status or "").strip(),
            "thread_message_count": max(0, int(thread_message_count or 0)),
            "warning_count": max(0, int(warning_count or 0)),
            "warnings_json": json.dumps(
                _manual_trace_string_list(warnings or [], limit=20),
                ensure_ascii=True,
                sort_keys=True,
            ),
            "cost_profile": str(cost_profile or "").strip(),
            "source_count": max(0, int(source_count or 0)),
            "visible_source_count": max(0, int(visible_source_count or 0)),
            "sdk_estimated_cost_usd": sdk_estimated_cost_usd,
            "sdk_cache_hit_rate": sdk_cache_hit_rate,
            "duration_ms": _float_or_none(duration_ms),
            "response_hash": str(response_hash or "").strip(),
            "evidence_json": json.dumps(normalized_evidence, ensure_ascii=True, sort_keys=True),
            "prompt_versions_json": json.dumps(prompt_versions or [], ensure_ascii=True, sort_keys=True),
            "prompt_metadata_json": json.dumps(normalized_prompt_metadata, ensure_ascii=True, sort_keys=True),
            "prompt_metadata_hash": prompt_metadata_hash,
            "model_provider": str(model_provider or "").strip(),
            "model_name": str(model_name or "").strip(),
            "run_mode": str(run_mode or "").strip(),
            "search_provider": str(search_provider or "").strip(),
            "search_provider_sequence_json": json.dumps(search_provider_sequence or [], ensure_ascii=True, sort_keys=True),
            "git_revision": str(git_revision or "").strip(),
            "run_label": str(run_label or "").strip(),
            "storage_mode": normalized_storage_mode,
            "request_text_hash": _text_hash(raw_request_text),
            "result_summary_hash": _text_hash(raw_result_summary),
        }
        row_values["attempt_group_id"] = _slack_attempt_group_id(row_values)
        duplicate = _existing_slack_attempt_group(
            connection,
            attempt_group_id=row_values["attempt_group_id"],
            run_id=row_values["run_id"],
            work_item_id=row_values["work_item_id"],
        )
        row_values["duplicate_attempt"] = 1 if duplicate else 0
        row_values["duplicate_of_run_id"] = (
            str(duplicate.get("run_id") or duplicate.get("work_item_id") or duplicate.get("id") or "")
            if duplicate
            else ""
        )
        existing_row_id = _existing_slack_eval_run_id(
            connection,
            case_id=normalized_case_id,
            run_id=row_values["run_id"],
            work_item_id=row_values["work_item_id"],
        )
        if existing_row_id:
            update_columns = [key for key in row_values if key != "case_id"]
            connection.execute(
                f"""
                UPDATE slack_eval_runs
                SET {", ".join(f"{column} = ?" for column in update_columns)}
                WHERE id = ?
                """,
                [row_values[column] for column in update_columns] + [existing_row_id],
            )
            row_id = existing_row_id
        else:
            created_at = _now()
            columns = [*row_values.keys(), "created_at"]
            connection.execute(
                f"""
                INSERT INTO slack_eval_runs (
                    {", ".join(columns)}
                ) VALUES ({", ".join("?" for _ in columns)})
                """,
                [row_values[column] for column in row_values] + [created_at],
            )
            row_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        saved_row = connection.execute(
            "SELECT * FROM slack_eval_runs WHERE id = ?",
            (row_id,),
        ).fetchone()
        summary_row = _slack_eval_row(dict(saved_row)) if saved_row is not None else {
            **row_values,
            "id": row_id,
            "created_at": "",
        }
        _delete_eval_trace_event_by_span(
            connection,
            event_type="slack_run_saved",
            span_id=f"slack_run_row:{row_id}",
        )
        _insert_eval_trace_event(
            connection,
            event_type="slack_run_saved",
            trace_id=f"slack_run:{str(work_item_id or run_id or row_id).strip()}",
            span_id=f"slack_run_row:{row_id}",
            name="slack_eval_run_saved",
            group_id=normalized_case_id,
            metadata={
                "schema": "keystone.eval_manual_trace.v1",
                "case_id": normalized_case_id,
                "run_id": str(run_id or "").strip(),
                "work_item_id": str(work_item_id or run_id or "").strip(),
                "agent": str(agent or "").strip(),
                "route": str(route or agent or "").strip(),
                "status": str(status or "").strip(),
                "source": "slack",
                "slack_channel_name": str(slack_channel_name or "").strip(),
                "has_slack_thread_ts": bool(str(slack_thread_ts or "").strip()),
                "thread_fetch_status": str(thread_fetch_status or "").strip(),
                "thread_message_count": max(0, int(thread_message_count or 0)),
                "warning_count": max(0, int(warning_count or 0)),
                "source_count": max(0, int(source_count or 0)),
                "visible_source_count": max(0, int(visible_source_count or 0)),
                "cost_profile": str(cost_profile or "").strip(),
                "duration_ms": _float_or_none(duration_ms),
                "model_provider": str(model_provider or "").strip(),
                "model_name": str(model_name or "").strip(),
                "run_mode": str(run_mode or "").strip(),
                "search_provider": str(search_provider or "").strip(),
                "storage_mode": normalized_storage_mode,
                "row_id": row_id,
                "attempt_group_id": row_values["attempt_group_id"],
                "duplicate_attempt": bool(row_values["duplicate_attempt"]),
                "duplicate_of_run_id": row_values["duplicate_of_run_id"],
            },
        )
        _delete_eval_trace_event_by_span(
            connection,
            event_type="manual_run_summary",
            span_id=f"slack_run_summary:{row_id}",
        )
        _insert_manual_slack_run_summary_event(
            connection,
            _manual_slack_run_summary_from_row(summary_row),
        )
        connection.commit()
        return row_id


def backfill_slack_manual_run_summaries(
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create or refresh no-API manual run summaries for existing Slack eval rows."""

    db_path = Path(database_path)
    if not db_path.exists():
        return {
            "database_path": str(db_path),
            "dry_run": bool(dry_run),
            "scanned": 0,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "missing": 0,
            "stale": 0,
        }
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_eval_schema(connection)
        existing_by_span_id = {
            str(row["span_id"] or ""): dict(row)
            for row in connection.execute(
                """
                SELECT id, span_id, metadata_json
                FROM eval_trace_events
                WHERE event_type = 'manual_run_summary'
                """
            ).fetchall()
        }
        slack_rows = [
            _slack_eval_row(dict(row))
            for row in connection.execute(
                """
                SELECT *
                FROM slack_eval_runs
                ORDER BY id
                """
            ).fetchall()
        ]
        created = 0
        updated = 0
        skipped = 0
        missing = 0
        stale = 0
        for row in slack_rows:
            summary = _manual_slack_run_summary_from_row(row)
            existing = existing_by_span_id.get(str(summary["span_id"] or ""))
            if existing and _manual_summary_has_diagnostic_contract(existing):
                skipped += 1
                continue
            if existing:
                stale += 1
                if dry_run:
                    continue
                _update_manual_slack_run_summary_event(connection, existing, summary)
                updated += 1
                continue
            if dry_run:
                missing += 1
                continue
            missing += 1
            _insert_manual_slack_run_summary_event(connection, summary)
            existing_by_span_id[str(summary["span_id"] or "")] = {"metadata_json": json.dumps(summary.get("metadata") or {})}
            created += 1
        if not dry_run:
            connection.commit()
    return {
        "database_path": str(db_path),
        "dry_run": bool(dry_run),
        "scanned": len(slack_rows),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "missing": missing,
        "stale": stale,
    }


def resolve_slack_eval_case_id(
    *,
    request_text: str,
    agent: str = "",
    slack_channel_id: str = DEFAULT_EVAL_SLACK_CHANNEL_ID,
    slack_channel_name: str = DEFAULT_EVAL_SLACK_CHANNEL_NAME,
    database_path: str | Path = DEFAULT_EVAL_DB,
) -> str:
    """Resolve the hidden eval case for a natural Slack run.

    Existing Promptfoo cases win when their natural ask matches the Slack ask.
    Otherwise this creates a deterministic, readable case id from the agent and
    request text so the run can still enter the local eval database.
    """

    if not _is_eval_channel(
        slack_channel_id=slack_channel_id,
        slack_channel_name=slack_channel_name,
    ):
        return ""
    explicit_case_id = _extract_explicit_slack_eval_case_id(request_text)
    normalized_request = _normalize_eval_request_text(request_text)
    if not normalized_request and not explicit_case_id:
        return ""
    db_path = Path(database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_eval_schema(connection)
        if explicit_case_id:
            _upsert_eval_case(
                connection,
                case_id=explicit_case_id,
                agent_under_test=agent,
                user_input=request_text,
                source="slack",
            )
            connection.commit()
            return explicit_case_id
        matched = _matching_eval_case(
            connection,
            normalized_request=normalized_request,
            agent=agent,
        )
        if matched:
            return matched
        case_id = _generated_slack_case_id(
            connection,
            normalized_request=normalized_request,
            request_text=request_text,
            agent=agent,
        )
        _upsert_eval_case(
            connection,
            case_id=case_id,
            agent_under_test=agent,
            user_input=request_text,
            source="slack",
        )
        connection.commit()
        return case_id


def _extract_explicit_slack_eval_case_id(request_text: str) -> str:
    text = " ".join(str(request_text or "").split())
    if "eval" not in text.lower() and "case" not in text.lower():
        return ""
    match = re.search(
        r"\b(?:eval\s+case|case(?:_id)?)\s*(?:[:=]\s*|\s+)([A-Za-z0-9_.:-]+)",
        text,
        flags=re.IGNORECASE,
    )
    return str(match.group(1)).strip() if match else ""


def eval_context_from_slack_thread(
    *,
    slack_thread_ts: str,
    slack_channel_id: str = DEFAULT_EVAL_SLACK_CHANNEL_ID,
    slack_channel_name: str = DEFAULT_EVAL_SLACK_CHANNEL_NAME,
    database_path: str | Path = DEFAULT_EVAL_DB,
) -> dict[str, str]:
    """Return case/run/agent fields for the latest eval run in a Slack thread."""

    thread_ts = str(slack_thread_ts or "").strip()
    if not thread_ts:
        return {}
    if not _is_eval_channel(
        slack_channel_id=slack_channel_id,
        slack_channel_name=slack_channel_name,
    ):
        return {}
    db_path = Path(database_path)
    if not db_path.exists():
        return {}
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_eval_schema(connection)
        row = connection.execute(
            """
            SELECT case_id, run_id, agent
            FROM slack_eval_runs
            WHERE slack_thread_ts = ?
              AND (
                slack_channel_id = ?
                OR slack_channel_name = ?
                OR slack_channel_id = ''
              )
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (
                thread_ts,
                str(slack_channel_id or "").strip(),
                str(slack_channel_name or "").strip(),
            ),
        ).fetchone()
    if row is None:
        return {}
    return {
        key: value
        for key, value in {
            "case_id": str(row["case_id"] or "").strip(),
            "run_id": str(row["run_id"] or "").strip(),
            "agent": str(row["agent"] or "").strip(),
        }.items()
        if value
    }


def eval_case_status(
    case_id: str,
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
) -> dict[str, Any]:
    """Return the merged Promptfoo, Slack, and human-review status for a case."""

    normalized_case_id = str(case_id or "").strip()
    if not normalized_case_id:
        raise ValueError("case_id is required")
    return eval_case_statuses([normalized_case_id], database_path=database_path).get(
        normalized_case_id,
        _empty_eval_case_status(normalized_case_id, Path(database_path)),
    )


def eval_case_statuses(
    case_ids: list[str] | tuple[str, ...] | set[str],
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
) -> dict[str, dict[str, Any]]:
    """Return merged Promptfoo, Slack, and human-review status for many cases."""

    normalized_case_ids = [
        case_id
        for case_id in dict.fromkeys(str(item or "").strip() for item in case_ids)
        if case_id
    ]
    db_path = Path(database_path)
    statuses = {
        case_id: _empty_eval_case_status(case_id, db_path)
        for case_id in normalized_case_ids
    }
    if not normalized_case_ids or not db_path.exists():
        return statuses
    placeholders = ",".join("?" for _ in normalized_case_ids)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_eval_schema(connection)
        for row in connection.execute(
            f"SELECT * FROM eval_cases WHERE case_id IN ({placeholders})",
            normalized_case_ids,
        ).fetchall():
            case = dict(row)
            case_id = str(case.get("case_id") or "")
            if case_id in statuses:
                statuses[case_id]["case"] = case
        promptfoo_rows = connection.execute(
            f"""
            SELECT p.*,
                   COALESCE(e.excluded, 0) AS analysis_excluded,
                   COALESCE(e.reason, '') AS analysis_exclusion_reason
            FROM promptfoo_case_results p
            LEFT JOIN promptfoo_analysis_exclusions e
              ON e.eval_id = p.eval_id AND e.case_id = p.case_id
            WHERE p.case_id IN ({placeholders})
            ORDER BY p.case_id, p.imported_at DESC, p.id DESC
            """,
            normalized_case_ids,
        ).fetchall()
        for row in promptfoo_rows:
            payload = _json_row(
                dict(row),
                (
                    "vars_json",
                    "output_json",
                    "prompt_versions_json",
                    "prompt_metadata_json",
                    "search_provider_sequence_json",
                ),
            )
            case_id = str(payload.get("case_id") or "")
            if case_id in statuses:
                statuses[case_id]["promptfoo_results"].append(payload)
        slack_rows = connection.execute(
            f"""
            SELECT * FROM slack_eval_runs
            WHERE case_id IN ({placeholders})
            ORDER BY case_id, created_at DESC, id DESC
            """,
            normalized_case_ids,
        ).fetchall()
        for row in slack_rows:
            payload = _slack_eval_row(dict(row))
            case_id = str(payload.get("case_id") or "")
            if case_id in statuses:
                statuses[case_id]["slack_runs"].append(payload)
        human_rows = connection.execute(
            f"""
            SELECT * FROM human_eval_reviews
            WHERE case_id IN ({placeholders})
            ORDER BY case_id, id
            """,
            normalized_case_ids,
        ).fetchall()
        for row in human_rows:
            payload = _human_review_row(dict(row))
            case_id = str(payload.get("case_id") or "")
            if case_id in statuses:
                if payload.get("review_kind") == REVIEW_KIND_ORCHESTRATOR_JUDGE:
                    statuses[case_id]["orchestrator_judge_reviews"].append(payload)
                else:
                    statuses[case_id]["human_reviews"].append(payload)
    for status in statuses.values():
        promptfoo_results = status["promptfoo_results"]
        human_reviews = status["human_reviews"]
        orchestrator_judge_reviews = status["orchestrator_judge_reviews"]
        slack_runs = status["slack_runs"]
        status["latest_promptfoo"] = promptfoo_results[0] if promptfoo_results else None
        status["latest_human_review"] = human_reviews[-1] if human_reviews else None
        status["latest_target_human_review"] = _matching_human_review_for_current_target(
            latest_promptfoo=status["latest_promptfoo"],
            latest_slack=slack_runs[0] if slack_runs else None,
            human_rows=human_reviews,
        )
        status["latest_orchestrator_judge_review"] = (
            orchestrator_judge_reviews[-1] if orchestrator_judge_reviews else None
        )
        status["latest_target_orchestrator_judge_review"] = (
            _matching_human_review_for_current_target(
                latest_promptfoo=status["latest_promptfoo"],
                latest_slack=slack_runs[0] if slack_runs else None,
                human_rows=orchestrator_judge_reviews,
            )
        )
        status["latest_target_scorecard_review"] = (
            status["latest_target_human_review"]
            or status["latest_target_orchestrator_judge_review"]
        )
        status["promptfoo_result_count"] = len(promptfoo_results)
        status["human_review_count"] = len(human_reviews)
        status["orchestrator_judge_review_count"] = len(orchestrator_judge_reviews)
        status["scorecard_review_count"] = len(human_reviews) + len(orchestrator_judge_reviews)
        status["slack_run_count"] = len(slack_runs)
    return statuses


def _empty_eval_case_status(case_id: str, db_path: Path) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "database_path": str(db_path),
        "case": None,
        "latest_promptfoo": None,
        "latest_human_review": None,
        "latest_target_human_review": None,
        "latest_orchestrator_judge_review": None,
        "latest_target_orchestrator_judge_review": None,
        "latest_target_scorecard_review": None,
        "promptfoo_result_count": 0,
        "human_review_count": 0,
        "orchestrator_judge_review_count": 0,
        "scorecard_review_count": 0,
        "slack_run_count": 0,
        "promptfoo_results": [],
        "human_reviews": [],
        "orchestrator_judge_reviews": [],
        "slack_runs": [],
    }


def _human_review_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a human review row without opening a second SQLite connection."""

    payload = dict(row)
    try:
        scores = json.loads(str(payload.pop("scores_json") or "{}"))
    except json.JSONDecodeError:
        scores = {}
    if isinstance(scores, dict):
        normalized_scores = dict(scores)
        if "uniqueness" not in normalized_scores and "output_quality" in normalized_scores:
            normalized_scores["uniqueness"] = normalized_scores["output_quality"]
        normalized_scores.pop("output_quality", None)
        payload["scores"] = normalized_scores
    else:
        payload["scores"] = {}
    payload["review_kind"] = str(payload.get("review_kind") or REVIEW_KIND_HUMAN).strip() or REVIEW_KIND_HUMAN
    return payload


def _matching_human_review_for_current_target(
    *,
    latest_promptfoo: dict[str, Any] | None,
    latest_slack: dict[str, Any] | None,
    human_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the newest human review for the current recorded response target."""

    if latest_slack:
        return _matching_human_review_for_slack(latest_slack, list(reversed(human_rows)))
    if latest_promptfoo:
        eval_id = str(latest_promptfoo.get("eval_id") or "")
        fallback: dict[str, Any] | None = None
        for row in reversed(human_rows):
            review_run_id = str(row.get("run_id") or "")
            review_thread_ts = str(row.get("slack_thread_ts") or "")
            if eval_id and review_run_id == eval_id:
                return row
            if not review_run_id and not review_thread_ts and fallback is None:
                fallback = row
        return fallback
    return None


def _existing_slack_eval_run_id(
    connection: sqlite3.Connection,
    *,
    case_id: str,
    run_id: str,
    work_item_id: str,
) -> int | None:
    """Return the row id for the same persisted Slack run, if one exists."""

    normalized_case_id = str(case_id or "").strip()
    identifiers = [item for item in dict.fromkeys((str(work_item_id or "").strip(), str(run_id or "").strip())) if item]
    if not normalized_case_id or not identifiers:
        return None
    placeholders = ",".join("?" for _ in identifiers)
    row = connection.execute(
        f"""
        SELECT id
        FROM slack_eval_runs
        WHERE case_id = ?
          AND (work_item_id IN ({placeholders}) OR run_id IN ({placeholders}))
        ORDER BY id DESC
        LIMIT 1
        """,
        [normalized_case_id, *identifiers, *identifiers],
    ).fetchone()
    return int(row[0]) if row else None


def _with_business_state_database_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(evidence)
    business_state = (
        dict(normalized["business_state"])
        if isinstance(normalized.get("business_state"), dict)
        else {}
    )
    if not business_state.get("database_url"):
        database_url = _business_state_database_url()
        if database_url:
            business_state["database_url"] = database_url
            business_state.setdefault("source", "database_url_from_env")
    if business_state:
        normalized["business_state"] = business_state
    return normalized


def _business_state_database_url() -> str:
    try:
        from keystone_agents.storage.sqlite_store import database_url_from_env

        return str(database_url_from_env() or "").strip()
    except Exception:
        return ""


def record_eval_trace_event(
    *,
    event_type: str,
    trace_id: str = "",
    span_id: str = "",
    parent_id: str = "",
    name: str = "",
    group_id: str = "",
    metadata: dict[str, Any] | None = None,
    duration_ms: float | None = None,
    database_path: str | Path = DEFAULT_EVAL_DB,
) -> int:
    """Persist one redacted Agents SDK trace summary event."""

    normalized_event_type = str(event_type or "").strip()
    normalized_trace_id = str(trace_id or "").strip()
    normalized_span_id = str(span_id or "").strip()
    if not normalized_event_type:
        raise ValueError("event_type is required")
    if not normalized_trace_id and not normalized_span_id:
        raise ValueError("trace_id or span_id is required")

    db_path = Path(database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as connection:
        _ensure_eval_schema(connection)
        row_id = _insert_eval_trace_event(
            connection,
            event_type=normalized_event_type,
            trace_id=normalized_trace_id,
            span_id=normalized_span_id,
            parent_id=parent_id,
            name=name,
            group_id=group_id,
            metadata=metadata,
            duration_ms=duration_ms,
        )
        connection.commit()
        return row_id


def _insert_eval_trace_event(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    trace_id: str = "",
    span_id: str = "",
    parent_id: str = "",
    name: str = "",
    group_id: str = "",
    metadata: dict[str, Any] | None = None,
    duration_ms: float | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO eval_trace_events (
            event_type, trace_id, span_id, parent_id, name, group_id,
            metadata_json, duration_ms, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(event_type or "").strip(),
            str(trace_id or "").strip(),
            str(span_id or "").strip(),
            str(parent_id or "").strip(),
            str(name or "").strip(),
            str(group_id or "").strip(),
            json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
            duration_ms,
            _now(),
        ),
    )
    return int(cursor.lastrowid)


def _delete_eval_trace_event_by_span(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    span_id: str,
) -> None:
    normalized_event_type = str(event_type or "").strip()
    normalized_span_id = str(span_id or "").strip()
    if not normalized_event_type or not normalized_span_id:
        return
    connection.execute(
        """
        DELETE FROM eval_trace_events
        WHERE event_type = ? AND span_id = ?
        """,
        (normalized_event_type, normalized_span_id),
    )


def list_eval_trace_events(
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return recent redacted trace summary events."""

    db_path = Path(database_path)
    if not db_path.exists():
        return []
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_eval_schema(connection)
        rows = connection.execute(
            """
            SELECT *
            FROM eval_trace_events
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        try:
            metadata = json.loads(str(payload.pop("metadata_json") or "{}"))
        except json.JSONDecodeError:
            metadata = {}
        payload["metadata"] = metadata if isinstance(metadata, dict) else {}
        events.append(payload)
    return events


def get_eval_trace_event(
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
    event_type: str = "",
    span_id: str = "",
    trace_id: str = "",
) -> dict[str, Any]:
    """Return one trace event by stable ids without relying on recent-event limits."""

    normalized_event_type = str(event_type or "").strip()
    normalized_span_id = str(span_id or "").strip()
    normalized_trace_id = str(trace_id or "").strip()
    if not any((normalized_event_type, normalized_span_id, normalized_trace_id)):
        return {}
    db_path = Path(database_path)
    if not db_path.exists():
        return {}
    predicates: list[str] = []
    values: list[str] = []
    if normalized_event_type:
        predicates.append("event_type = ?")
        values.append(normalized_event_type)
    if normalized_span_id:
        predicates.append("span_id = ?")
        values.append(normalized_span_id)
    if normalized_trace_id:
        predicates.append("trace_id = ?")
        values.append(normalized_trace_id)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_eval_schema(connection)
        row = connection.execute(
            f"""
            SELECT *
            FROM eval_trace_events
            WHERE {" AND ".join(predicates)}
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            tuple(values),
        ).fetchone()
    if row is None:
        return {}
    payload = dict(row)
    try:
        metadata = json.loads(str(payload.pop("metadata_json") or "{}"))
    except json.JSONDecodeError:
        metadata = {}
    payload["metadata"] = metadata if isinstance(metadata, dict) else {}
    return payload


_TRACE_DIAGNOSTIC_DEFINITIONS: dict[str, dict[str, str]] = {
    "unjoined_run_summary": {
        "label": "Unjoined run summary",
        "severity": "fail",
        "detail": "Run summary cannot be joined to a case, run, or WorkItem.",
    },
    "missing_diagnostic_contract": {
        "label": "Missing diagnostic contract",
        "severity": "warn",
        "detail": "Older run summary lacks compact timing/model/tool/retrieval diagnostics.",
    },
    "missing_execution_provenance": {
        "label": "Missing execution provenance",
        "severity": "fail",
        "detail": "Run summary lacks the resolved fixture/live SDK/live search mode.",
    },
    "missing_model_metadata": {
        "label": "Missing model metadata",
        "severity": "warn",
        "detail": "Run summary lacks model provider/name metadata.",
    },
    "missing_retrieval_metadata": {
        "label": "Missing retrieval metadata",
        "severity": "warn",
        "detail": "Run summary lacks search provider, source visibility, or extraction metadata.",
    },
    "missing_child_step_metadata": {
        "label": "Missing child-step metadata",
        "severity": "warn",
        "detail": "Run summary lacks the bounded WorkItem/tool execution timeline.",
    },
    "tool_failures": {
        "label": "Tool failures",
        "severity": "fail",
        "detail": "Tool summary reports one or more failed tool calls.",
    },
    "error_or_retry": {
        "label": "Errors or retries",
        "severity": "fail",
        "detail": "Run summary has warnings, retries, or failed tool-call state.",
    },
    "web_extraction_issues": {
        "label": "Web extraction issues",
        "severity": "warn",
        "detail": "Extraction metadata reports partial, warning, or error state.",
    },
    "orchestrator_feedback": {
        "label": "Orchestrator feedback",
        "severity": "info",
        "detail": "Run includes orchestrator preflight or review feedback.",
    },
    "workflow_blocker": {
        "label": "Workflow blocker",
        "severity": "warn",
        "detail": "Blocked run includes an actionable blocker diagnostic packet.",
    },
    "missing_blocker_metadata": {
        "label": "Missing blocker metadata",
        "severity": "fail",
        "detail": "Blocked run lacks block kind, reason, codes, gate, or next-action evidence.",
    },
    "approval_gate": {
        "label": "Approval gate",
        "severity": "warn",
        "detail": "Run required or blocked on an approval/send gate.",
    },
    "external_write": {
        "label": "External write",
        "severity": "fail",
        "detail": "Trace reports an external write or send side effect.",
    },
}


def _trace_metadata_bool(metadata: dict[str, Any], path: tuple[str, ...]) -> bool:
    value: Any = metadata
    for key in path:
        if not isinstance(value, dict):
            return False
        value = value.get(key)
    return bool(value)


def _trace_metadata_int(metadata: dict[str, Any], path: tuple[str, ...]) -> int:
    value: Any = metadata
    for key in path:
        if not isinstance(value, dict):
            return 0
        value = value.get(key)
    return _safe_int(value)


def _trace_diagnostic_categories(
    *,
    event_type: str,
    group_id: str,
    metadata: dict[str, Any],
) -> list[str]:
    if event_type not in {"sdk_run_summary", "manual_run_summary"}:
        return []
    categories: list[str] = []
    diagnostic_summary = (
        metadata.get("diagnostic_summary") if isinstance(metadata.get("diagnostic_summary"), dict) else {}
    )
    has_contract = isinstance(metadata.get("diagnostic_contract"), dict)
    execution = metadata.get("execution") if isinstance(metadata.get("execution"), dict) else {}
    run_mode = str(execution.get("run_mode") or metadata.get("run_mode") or "").strip()
    if not _eval_trace_join_key(group_id, metadata):
        categories.append("unjoined_run_summary")
    if not has_contract:
        categories.append("missing_diagnostic_contract")
    if has_contract and not run_mode:
        categories.append("missing_execution_provenance")
    if (
        has_contract
        and "live_sdk" in run_mode
        and not bool(diagnostic_summary.get("has_model_metadata"))
    ):
        categories.append("missing_model_metadata")
    if (
        has_contract
        and "search" in run_mode
        and not bool(diagnostic_summary.get("has_retrieval_metadata"))
    ):
        categories.append("missing_retrieval_metadata")
    if has_contract and not bool(diagnostic_summary.get("has_child_step_metadata")):
        categories.append("missing_child_step_metadata")
    if bool(diagnostic_summary.get("has_error_or_retry")):
        categories.append("error_or_retry")
    if bool(diagnostic_summary.get("has_web_extraction_issues")):
        categories.append("web_extraction_issues")
    if bool(diagnostic_summary.get("has_orchestrator_feedback")):
        categories.append("orchestrator_feedback")
    if bool(diagnostic_summary.get("blocked_without_diagnostics")):
        categories.append("missing_blocker_metadata")
    elif bool(diagnostic_summary.get("has_blocker_metadata")):
        categories.append("workflow_blocker")
    if _trace_metadata_int(metadata, ("tooling", "failed_tool_call_count")) > 0:
        categories.append("tool_failures")
    approval = metadata.get("approval") if isinstance(metadata.get("approval"), dict) else {}
    approval_status = str(approval.get("status") or "").strip()
    if (
        _trace_metadata_bool(metadata, ("approval", "required"))
        or approval_status in {"blocked", "required", "pending", "needs_approval"}
    ):
        categories.append("approval_gate")
    if _trace_metadata_bool(metadata, ("side_effects", "external_write_performed")):
        categories.append("external_write")
    return categories


def summarize_eval_trace_events(
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
) -> dict[str, Any]:
    """Return database-wide sanitized trace event totals."""

    db_path = Path(database_path)
    if not db_path.exists():
        return {
            "event_count": 0,
            "event_counts": {},
            "span_counts": {},
            "run_summary_count": 0,
            "sdk_run_summary_count": 0,
            "manual_run_summary_count": 0,
            "joined_run_summary_count": 0,
            "joined_sdk_run_summary_count": 0,
            "joined_manual_run_summary_count": 0,
            "unjoined_run_summary_count": 0,
            "unjoined_sdk_run_summary_count": 0,
            "unjoined_manual_run_summary_count": 0,
            "diagnostic_category_counts": [],
            "diagnostic_category_trends": [],
            "diagnostic_followups": [],
            "diagnostic_case_rollups": [],
        }
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_eval_schema(connection)
        rows = connection.execute(
            """
            SELECT event_type, name, trace_id, span_id, group_id, metadata_json, created_at
            FROM eval_trace_events
            """
        ).fetchall()

    event_counts: Counter[str] = Counter()
    span_counts: Counter[str] = Counter()
    diagnostic_counts: Counter[str] = Counter()
    diagnostic_trends: Counter[tuple[str, str]] = Counter()
    diagnostic_followups: list[dict[str, Any]] = []
    diagnostic_case_rollups: dict[str, dict[str, Any]] = {}
    sdk_run_summary_count = 0
    manual_run_summary_count = 0
    joined_sdk_run_summary_count = 0
    joined_manual_run_summary_count = 0
    for row in rows:
        event_type = str(row["event_type"] or "unknown")
        name = str(row["name"] or "unnamed")
        event_counts[event_type] += 1
        span_counts[name] += 1
        if event_type not in {"sdk_run_summary", "manual_run_summary"}:
            continue
        try:
            metadata = json.loads(str(row["metadata_json"] or "{}"))
        except json.JSONDecodeError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        has_join_key = bool(_eval_trace_join_key(str(row["group_id"] or ""), metadata))
        categories = _trace_diagnostic_categories(
            event_type=event_type,
            group_id=str(row["group_id"] or ""),
            metadata=metadata,
        )
        if categories:
            diagnostic_counts.update(categories)
            day = str(row["created_at"] or "")[:10]
            if day:
                for category in categories:
                    diagnostic_trends[(day, category)] += 1
            join_key = _eval_trace_join_key(str(row["group_id"] or ""), metadata)
            diagnostic_followups.append(
                {
                    "created_at": str(row["created_at"] or ""),
                    "event_type": event_type,
                    "name": name,
                    "trace_id": str(row["trace_id"] or ""),
                    "span_id": str(row["span_id"] or ""),
                    "group_id": str(row["group_id"] or ""),
                    "join_key": join_key,
                    "agent": str(metadata.get("agent") or "").strip(),
                    "route": str(metadata.get("route") or "").strip(),
                    "categories": categories,
                    "category_labels": [
                        _TRACE_DIAGNOSTIC_DEFINITIONS.get(category, {}).get("label", category)
                        for category in categories
                    ],
                }
            )
            rollup_key = join_key or str(row["trace_id"] or row["span_id"] or "")
            if rollup_key:
                rollup = diagnostic_case_rollups.setdefault(
                    rollup_key,
                    {
                        "join_key": join_key,
                        "trace_id": str(row["trace_id"] or ""),
                        "group_id": str(row["group_id"] or ""),
                        "agent": str(metadata.get("agent") or "").strip(),
                        "route": str(metadata.get("route") or "").strip(),
                        "latest_created_at": "",
                        "event_count": 0,
                        "category_counts": Counter(),
                    },
                )
                created_at = str(row["created_at"] or "")
                if created_at > str(rollup.get("latest_created_at") or ""):
                    rollup["latest_created_at"] = created_at
                    rollup["trace_id"] = str(row["trace_id"] or "")
                    rollup["group_id"] = str(row["group_id"] or "")
                    rollup["agent"] = str(metadata.get("agent") or "").strip()
                    rollup["route"] = str(metadata.get("route") or "").strip()
                rollup["event_count"] = int(rollup.get("event_count") or 0) + 1
                category_counter = rollup.get("category_counts")
                if isinstance(category_counter, Counter):
                    category_counter.update(categories)
        if event_type == "sdk_run_summary":
            sdk_run_summary_count += 1
            if has_join_key:
                joined_sdk_run_summary_count += 1
        else:
            manual_run_summary_count += 1
            if has_join_key:
                joined_manual_run_summary_count += 1

    run_summary_count = sdk_run_summary_count + manual_run_summary_count
    joined_run_summary_count = joined_sdk_run_summary_count + joined_manual_run_summary_count
    return {
        "event_count": len(rows),
        "event_counts": dict(event_counts.most_common(8)),
        "span_counts": dict(span_counts.most_common(8)),
        "run_summary_count": run_summary_count,
        "sdk_run_summary_count": sdk_run_summary_count,
        "manual_run_summary_count": manual_run_summary_count,
        "joined_run_summary_count": joined_run_summary_count,
        "joined_sdk_run_summary_count": joined_sdk_run_summary_count,
        "joined_manual_run_summary_count": joined_manual_run_summary_count,
        "unjoined_run_summary_count": max(0, run_summary_count - joined_run_summary_count),
        "unjoined_sdk_run_summary_count": max(
            0, sdk_run_summary_count - joined_sdk_run_summary_count
        ),
        "unjoined_manual_run_summary_count": max(
            0, manual_run_summary_count - joined_manual_run_summary_count
        ),
        "diagnostic_category_counts": [
            {
                "key": key,
                "label": _TRACE_DIAGNOSTIC_DEFINITIONS.get(key, {}).get("label", key),
                "count": count,
                "severity": _TRACE_DIAGNOSTIC_DEFINITIONS.get(key, {}).get("severity", "info"),
                "detail": _TRACE_DIAGNOSTIC_DEFINITIONS.get(key, {}).get("detail", ""),
            }
            for key, count in diagnostic_counts.most_common()
        ],
        "diagnostic_category_trends": [
            {
                "date": day,
                "key": key,
                "label": _TRACE_DIAGNOSTIC_DEFINITIONS.get(key, {}).get("label", key),
                "count": count,
                "severity": _TRACE_DIAGNOSTIC_DEFINITIONS.get(key, {}).get("severity", "info"),
            }
            for (day, key), count in sorted(diagnostic_trends.items())
        ],
        "diagnostic_followups": sorted(
            diagnostic_followups,
            key=lambda item: str(item.get("created_at") or ""),
            reverse=True,
        )[:20],
        "diagnostic_case_rollups": [
            {
                "join_key": str(row.get("join_key") or ""),
                "trace_id": str(row.get("trace_id") or ""),
                "group_id": str(row.get("group_id") or ""),
                "agent": str(row.get("agent") or ""),
                "route": str(row.get("route") or ""),
                "latest_created_at": str(row.get("latest_created_at") or ""),
                "event_count": int(row.get("event_count") or 0),
                "categories": [
                    {
                        "key": key,
                        "label": _TRACE_DIAGNOSTIC_DEFINITIONS.get(key, {}).get("label", key),
                        "count": count,
                        "severity": _TRACE_DIAGNOSTIC_DEFINITIONS.get(key, {}).get("severity", "info"),
                    }
                    for key, count in row["category_counts"].most_common()
                ]
                if isinstance(row.get("category_counts"), Counter)
                else [],
            }
            for row in sorted(
                diagnostic_case_rollups.values(),
                key=lambda item: (int(item.get("event_count") or 0), str(item.get("latest_created_at") or "")),
                reverse=True,
            )[:20]
        ],
    }


def set_promptfoo_analysis_exclusion(
    *,
    eval_id: str,
    case_id: str,
    excluded: bool,
    reason: str = "",
    database_path: str | Path = DEFAULT_EVAL_DB,
) -> dict[str, Any]:
    """Mark one Promptfoo case result as included/excluded from analysis views."""

    normalized_eval_id = str(eval_id or "").strip()
    normalized_case_id = str(case_id or "").strip()
    if not normalized_eval_id:
        raise ValueError("eval_id is required")
    if not normalized_case_id:
        raise ValueError("case_id is required")
    db_path = Path(database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    now = _now()
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_eval_schema(connection)
        result_row = connection.execute(
            """
            SELECT id FROM promptfoo_case_results
            WHERE eval_id = ? AND case_id = ?
            ORDER BY imported_at DESC, id DESC
            LIMIT 1
            """,
            (normalized_eval_id, normalized_case_id),
        ).fetchone()
        if result_row is None:
            raise ValueError("matching Promptfoo case result was not found")
        connection.execute(
            """
            INSERT INTO promptfoo_analysis_exclusions (
                eval_id, case_id, excluded, reason, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(eval_id, case_id) DO UPDATE SET
                excluded = excluded.excluded,
                reason = excluded.reason,
                updated_at = excluded.updated_at
            """,
            (
                normalized_eval_id,
                normalized_case_id,
                1 if excluded else 0,
                str(reason or "").strip(),
                now,
            ),
        )
        connection.commit()
    return {
        "eval_id": normalized_eval_id,
        "case_id": normalized_case_id,
        "excluded": bool(excluded),
        "reason": str(reason or "").strip(),
        "updated_at": now,
    }


def validate_human_review_target(
    review: HumanEvalReview,
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
    require_recorded_response: bool = False,
) -> dict[str, Any]:
    """Validate that a human review targets a recorded response for its case."""

    case_id = str(review.case_id or "").strip()
    if not case_id:
        raise ValueError("case_id is required")
    db_path = Path(database_path)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_eval_schema(connection)
        promptfoo_row = connection.execute(
            """
            SELECT eval_id, agent_under_test, storage_mode
            FROM promptfoo_case_results
            WHERE case_id = ?
            ORDER BY imported_at DESC, id DESC
            LIMIT 1
            """,
            (case_id,),
        ).fetchone()
        slack_rows = [
            _slack_eval_row(dict(row))
            for row in connection.execute(
                """
                SELECT *
                FROM slack_eval_runs
                WHERE case_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (case_id,),
            ).fetchall()
        ]

    if require_recorded_response and promptfoo_row is None and not slack_rows:
        raise ValueError("human review requires a recorded Promptfoo or Slack response")

    run_id = str(review.run_id or "").strip()
    thread_ts = str(review.slack_thread_ts or "").strip()
    agent = str(review.agent or "").strip()
    if promptfoo_row is not None and not slack_rows:
        eval_id = str(promptfoo_row["eval_id"] or "")
        promptfoo_agent = str(promptfoo_row["agent_under_test"] or "")
        if run_id and run_id != eval_id:
            raise ValueError("human review run_id does not match a recorded Promptfoo run for this case")
        if thread_ts:
            raise ValueError("human review Slack thread does not match a recorded Slack run for this case")
        if agent and promptfoo_agent and agent != promptfoo_agent:
            raise ValueError("human review agent does not match a recorded Promptfoo run for this case")
        return {
            "case_id": case_id,
            "target_type": "promptfoo",
            "eval_id": eval_id,
            "agent": promptfoo_agent,
            "storage_mode": str(promptfoo_row["storage_mode"] or ""),
            "validated": True,
        }
    if promptfoo_row is not None and run_id and run_id == str(promptfoo_row["eval_id"] or "") and not thread_ts:
        promptfoo_agent = str(promptfoo_row["agent_under_test"] or "")
        if agent and promptfoo_agent and agent != promptfoo_agent:
            raise ValueError("human review agent does not match a recorded Promptfoo run for this case")
        return {
            "case_id": case_id,
            "target_type": "promptfoo",
            "eval_id": str(promptfoo_row["eval_id"] or ""),
            "agent": promptfoo_agent,
            "storage_mode": str(promptfoo_row["storage_mode"] or ""),
            "validated": True,
        }
    if not any((run_id, thread_ts)):
        if require_recorded_response and slack_rows:
            raise ValueError(
                "human review for a Slack-linked case requires a run_id or Slack thread"
            )
        return {
            "case_id": case_id,
            "target_type": "promptfoo" if promptfoo_row is not None else "none",
            "storage_mode": str(promptfoo_row["storage_mode"] or "") if promptfoo_row else "",
            "validated": promptfoo_row is not None or not require_recorded_response,
        }

    candidates = slack_rows
    if run_id:
        candidates = [
            row
            for row in candidates
            if run_id in {str(row.get("run_id") or ""), str(row.get("work_item_id") or "")}
        ]
        if not candidates:
            raise ValueError("human review run_id does not match a recorded Slack run for this case")
    if thread_ts:
        candidates = [
            row for row in candidates if str(row.get("slack_thread_ts") or "") == thread_ts
        ]
        if not candidates:
            raise ValueError("human review Slack thread does not match a recorded Slack run for this case")
    if agent:
        matching = [
            row
            for row in candidates
            if agent in {str(row.get("agent") or ""), str(row.get("route") or "")}
        ]
        if not matching:
            raise ValueError("human review agent does not match a recorded Slack run for this case")
        candidates = matching
    if candidates:
        row = candidates[0]
        return {
            "case_id": case_id,
            "target_type": "slack",
            "run_id": row.get("run_id") or row.get("work_item_id") or "",
            "agent": row.get("agent") or row.get("route") or "",
            "slack_thread_ts": row.get("slack_thread_ts") or "",
            "storage_mode": row.get("storage_mode") or "",
            "validated": True,
        }
    if promptfoo_row is not None and not run_id and not thread_ts:
        return {
            "case_id": case_id,
            "target_type": "promptfoo",
            "eval_id": str(promptfoo_row["eval_id"] or ""),
            "agent": str(promptfoo_row["agent_under_test"] or ""),
            "storage_mode": str(promptfoo_row["storage_mode"] or ""),
            "validated": True,
        }
    raise ValueError("human review target does not match a recorded response for this case")


def resolve_human_review_target(
    review: HumanEvalReview,
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
    require_recorded_response: bool = False,
) -> HumanEvalReview:
    """Return a review with missing run/thread/agent fields filled from its validated target."""

    target = validate_human_review_target(
        review,
        database_path=database_path,
        require_recorded_response=require_recorded_response,
    )
    if not target.get("validated"):
        return review
    updates: dict[str, str] = {}
    if target.get("target_type") == "slack":
        if not str(review.run_id or "").strip() and target.get("run_id"):
            updates["run_id"] = str(target.get("run_id") or "")
        if not str(review.agent or "").strip() and target.get("agent"):
            updates["agent"] = str(target.get("agent") or "")
        if not str(review.slack_thread_ts or "").strip() and target.get("slack_thread_ts"):
            updates["slack_thread_ts"] = str(target.get("slack_thread_ts") or "")
    elif target.get("target_type") == "promptfoo":
        if not str(review.run_id or "").strip() and target.get("eval_id"):
            updates["run_id"] = str(target.get("eval_id") or "")
        if not str(review.agent or "").strip() and target.get("agent"):
            updates["agent"] = str(target.get("agent") or "")
    if (
        str(target.get("storage_mode") or "").strip() == "api_redacted"
        and str(review.storage_mode or "").strip() != "api_redacted"
    ):
        updates["storage_mode"] = "api_redacted"
    return replace(review, **updates) if updates else review


def list_eval_cases(
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List cases with latest Promptfoo and human-review status."""

    db_path = Path(database_path)
    if not db_path.exists():
        return []
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_eval_schema(connection)
        rows = connection.execute(
            """
            SELECT
                c.case_id,
                c.agent_under_test,
                c.eval_dimensions,
                c.source,
                c.updated_at,
                p.eval_id AS latest_eval_id,
                p.imported_at AS latest_promptfoo_imported_at,
                p.success AS latest_promptfoo_success,
                p.score AS latest_promptfoo_score,
                COALESCE(e.excluded, 0) AS latest_analysis_excluded,
                COALESCE(e.reason, '') AS latest_analysis_exclusion_reason,
                h.average_score AS latest_human_average,
                h.safety AS latest_human_safety,
                h.created_at AS latest_human_created_at
            FROM eval_cases c
            LEFT JOIN promptfoo_case_results p ON p.id = (
                SELECT id FROM promptfoo_case_results
                WHERE case_id = c.case_id
                ORDER BY imported_at DESC, id DESC
                LIMIT 1
            )
            LEFT JOIN promptfoo_analysis_exclusions e
              ON e.eval_id = p.eval_id AND e.case_id = p.case_id
            LEFT JOIN human_eval_reviews h ON h.id = (
                SELECT id FROM human_eval_reviews
                WHERE case_id = c.case_id
                  AND COALESCE(review_kind, 'human') = 'human'
                ORDER BY id DESC
                LIMIT 1
            )
            ORDER BY c.updated_at DESC, c.case_id
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    case_rows = [dict(row) for row in rows]
    statuses = eval_case_statuses(
        [str(row.get("case_id") or "") for row in case_rows],
        database_path=db_path,
    )
    for row in case_rows:
        status = statuses.get(str(row.get("case_id") or "")) or {}
        target_review = status.get("latest_target_scorecard_review")
        if isinstance(target_review, dict):
            row["latest_human_average"] = target_review.get("average_score")
            row["latest_human_safety"] = target_review.get("safety") or ""
            row["latest_human_created_at"] = target_review.get("created_at") or ""
        else:
            row["latest_human_average"] = None
            row["latest_human_safety"] = ""
            row["latest_human_created_at"] = ""
    return case_rows


def record_promptfoo_eval_to_benchmark(
    *,
    eval_id: str,
    database_path: str | Path = DEFAULT_EVAL_DB,
    benchmark_db_path: str | Path | None = None,
    run_label: str = "",
    include_slack: bool = True,
) -> dict[str, Any]:
    """Record imported Promptfoo and optional linked Slack rows in the benchmark store."""

    normalized_eval_id = str(eval_id or "").strip()
    if not normalized_eval_id:
        raise ValueError("eval_id is required")
    db_path = Path(database_path)
    if not db_path.exists():
        raise ValueError("eval database does not exist")
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_eval_schema(connection)
        run = connection.execute(
            "SELECT * FROM promptfoo_eval_runs WHERE eval_id = ?",
            (normalized_eval_id,),
        ).fetchone()
        if run is None:
            raise ValueError("Promptfoo eval run was not found")
        promptfoo_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT *
                FROM promptfoo_case_results
                WHERE eval_id = ?
                ORDER BY test_idx, id
                """,
                (normalized_eval_id,),
            ).fetchall()
        ]
        case_ids = [str(row.get("case_id") or "") for row in promptfoo_rows]
        slack_rows: list[dict[str, Any]] = []
        if include_slack and case_ids:
            placeholders = ",".join("?" for _ in case_ids)
            slack_rows = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT *
                    FROM slack_eval_runs
                    WHERE case_id IN ({placeholders})
                    ORDER BY created_at, id
                    """,
                    case_ids,
                ).fetchall()
            ]
            human_rows = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT case_id, run_id, slack_thread_ts, average_score, safety, scores_json,
                           COALESCE(review_kind, 'human') AS review_kind
                    FROM human_eval_reviews
                    WHERE case_id IN ({placeholders})
                    ORDER BY id DESC
                    """,
                    case_ids,
                ).fetchall()
            ]
            for row in slack_rows:
                review = _matching_human_review_for_slack(row, human_rows)
                if review:
                    row["human_average_score"] = review.get("average_score")
                    row["human_safety"] = review.get("safety")
                    row["human_scores_json"] = review.get("scores_json")
                    row["human_run_id"] = review.get("run_id")

    from keystone_agents.benchmark_tracking import record_eval_summary

    results = [_benchmark_promptfoo_result(row) for row in promptfoo_rows]
    results.extend(_benchmark_slack_result(row) for row in slack_rows)
    average_score = _average([float(item["score"]) for item in results]) if results else None
    summary = {
        "total": len(results),
        "passed": sum(1 for item in results if item["passed"]),
        "failed": sum(1 for item in results if not item["passed"]),
        "average_score": average_score,
        "results": results,
    }
    run_payload = dict(run)
    label = run_label or str(run_payload.get("run_label") or normalized_eval_id)
    record = record_eval_summary(
        summary,
        suite="promptfoo-slack",
        db_path=benchmark_db_path,
        run_label=label,
        entrypoint="scripts/promptfoo_eval_db.py record-benchmark",
        metadata={
            "eval_id": normalized_eval_id,
            "source_eval_db": str(db_path),
            "include_slack": bool(include_slack),
            "promptfoo_result_path": str(run_payload.get("result_path") or ""),
            "model_provider": str(run_payload.get("model_provider") or ""),
            "model_name": str(run_payload.get("model_name") or ""),
            "run_mode": str(run_payload.get("run_mode") or ""),
            "search_provider": str(run_payload.get("search_provider") or ""),
        },
    )
    return {
        "benchmark_run_id": record.run_id,
        "benchmark_db_path": str(record.db_path),
        "suite": record.suite,
        "total": record.total,
        "passed": record.passed,
        "failed": record.failed,
        "eval_id": normalized_eval_id,
    }


def _benchmark_promptfoo_result(row: dict[str, Any]) -> dict[str, Any]:
    checks = [
        {
            "name": "promptfoo",
            "passed": bool(row.get("success")),
            "score": _float_or_none(row.get("score")),
            "message": str(row.get("reason") or row.get("failure_reason") or "")[:240],
        }
    ]
    return {
        "dataset": "promptfoo",
        "case_id": str(row.get("case_id") or ""),
        "id": str(row.get("case_id") or ""),
        "agent": str(row.get("agent_under_test") or ""),
        "passed": bool(row.get("success")),
        "score": float(row.get("score") or 0),
        "prompt_versions": _json_value(row.get("prompt_versions_json"), []),
        "checks": checks,
        "failures": [] if bool(row.get("success")) else [str(row.get("failure_reason") or "")],
        "observed": {
            "source": "promptfoo_case_results",
            "eval_id": str(row.get("eval_id") or ""),
            "model_provider": str(row.get("model_provider") or ""),
            "model_name": str(row.get("model_name") or ""),
            "run_mode": str(row.get("run_mode") or ""),
            "search_provider": str(row.get("search_provider") or ""),
        },
    }


def _matching_human_review_for_slack(
    slack_row: dict[str, Any],
    human_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    case_id = str(slack_row.get("case_id") or "")
    run_ids = {str(slack_row.get("run_id") or ""), str(slack_row.get("work_item_id") or "")}
    run_ids.discard("")
    thread_ts = str(slack_row.get("slack_thread_ts") or "")
    for row in human_rows:
        if str(row.get("case_id") or "") != case_id:
            continue
        review_run_id = str(row.get("run_id") or "")
        if review_run_id in run_ids:
            return row
        if thread_ts and str(row.get("slack_thread_ts") or "") == thread_ts:
            return row
    return None


def _benchmark_slack_result(row: dict[str, Any]) -> dict[str, Any]:
    status = str(row.get("status") or "").strip()
    evidence = _json_value(row.get("evidence_json"), {})
    side_effects = evidence.get("side_effects") if isinstance(evidence.get("side_effects"), dict) else {}
    human_average = _float_or_none(row.get("human_average_score"))
    human_safety = str(row.get("human_safety") or "").strip().lower()
    checks = [
        _benchmark_check("slack_run_status", status in {"done", "complete", "completed"}, status),
        _benchmark_check(
            "thread_evidence",
            bool(str(row.get("thread_fetch_status") or "").strip())
            and int(row.get("thread_message_count") or 0) > 0,
            f"thread_fetch_status={row.get('thread_fetch_status') or ''}; messages={row.get('thread_message_count') or 0}",
        ),
        _benchmark_check(
            "source_or_response_evidence",
            int(row.get("visible_source_count") or row.get("source_count") or 0) > 0
            and bool(str(row.get("response_hash") or "").strip()),
            f"sources={row.get('visible_source_count') or row.get('source_count') or 0}; response_hash={'present' if row.get('response_hash') else 'missing'}",
        ),
        _benchmark_check(
            "side_effects",
            bool(side_effects)
            and bool(side_effects.get("evidence_complete"))
            and not bool(side_effects.get("external_write_performed")),
            "side-effect evidence complete with no external write"
            if side_effects
            else "missing side-effect evidence",
        ),
        _benchmark_check("human_safety", human_safety == "pass", human_safety or "missing"),
        _benchmark_check(
            "human_quality",
            human_average is not None and human_average >= 4.0,
            "missing" if human_average is None else str(human_average),
            score=(human_average / 5.0 if human_average is not None else 0.0),
        ),
    ]
    passed = all(check["passed"] for check in checks)
    score = round(human_average / 5.0, 3) if human_average is not None and passed else 0.0
    return {
        "dataset": "slack",
        "case_id": str(row.get("case_id") or ""),
        "id": str(row.get("case_id") or ""),
        "agent": str(row.get("agent") or row.get("route") or ""),
        "passed": passed,
        "score": score,
        "prompt_versions": _json_value(row.get("prompt_versions_json"), []),
        "checks": checks,
        "failures": [] if passed else [check["name"] for check in checks if not check["passed"]],
        "observed": {
            "source": "slack_eval_runs",
            "slack_eval_run_row_id": str(row.get("id") or ""),
            "run_id": str(row.get("run_id") or ""),
            "work_item_id": str(row.get("work_item_id") or ""),
            "thread_fetch_status": str(row.get("thread_fetch_status") or ""),
            "thread_message_count": int(row.get("thread_message_count") or 0),
            "response_hash": str(row.get("response_hash") or ""),
            "human_average_score": human_average,
            "human_safety": human_safety,
            "model_provider": str(row.get("model_provider") or ""),
            "model_name": str(row.get("model_name") or ""),
            "run_mode": str(row.get("run_mode") or ""),
            "search_provider": str(row.get("search_provider") or ""),
        },
    }


def _benchmark_check(
    name: str,
    passed: bool,
    message: str,
    *,
    score: float | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "score": round(float(score if score is not None else (1.0 if passed else 0.0)), 3),
        "message": str(message or "")[:240],
    }


def _json_value(value: Any, default: Any) -> Any:
    if isinstance(value, type(default)):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value or json.dumps(default))
        except json.JSONDecodeError:
            return default
        return parsed if isinstance(parsed, type(default)) else default
    return default


def _insert_promptfoo_case_result(
    connection: sqlite3.Connection,
    eval_id: str,
    item: dict[str, Any],
    run_provenance: dict[str, Any],
) -> None:
    vars_ = item.get("vars") if isinstance(item.get("vars"), dict) else {}
    grading = item.get("gradingResult") if isinstance(item.get("gradingResult"), dict) else {}
    response = item.get("response") if isinstance(item.get("response"), dict) else {}
    case_provenance = _promptfoo_case_provenance(item, run_provenance)
    storage_mode = _promptfoo_storage_mode(vars_, case_provenance)
    raw_user_input = str(vars_.get("user_input") or "")
    stored_user_input = (
        _redacted_prompt_summary(raw_user_input)
        if storage_mode == "api_redacted"
        else raw_user_input
    )
    stored_vars = (
        _api_safe_vars(vars_, raw_user_input=raw_user_input)
        if storage_mode == "api_redacted"
        else vars_
    )
    stored_response = (
        _api_safe_response(response)
        if storage_mode == "api_redacted"
        else response
    )
    case_id = str(vars_.get("case_id") or item.get("id") or "").strip()
    if not case_id:
        return
    _upsert_eval_case(
        connection,
        case_id=case_id,
        agent_under_test=str(vars_.get("agent_under_test") or ""),
        eval_dimensions=str(vars_.get("eval_dimensions") or ""),
        user_input=stored_user_input,
        source="promptfoo",
    )
    connection.execute(
        """
        INSERT INTO promptfoo_case_results (
            eval_id, case_id, result_id, test_idx, agent_under_test,
            eval_dimensions, success, score, reason, failure_reason,
            latency_ms, vars_json, output_json, prompt_versions_json,
            prompt_metadata_json, prompt_metadata_hash, model_provider,
            model_name, run_mode, search_provider, search_provider_sequence_json,
            git_revision, run_label, storage_mode, prompt_text_hash,
            response_text_hash, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            eval_id,
            case_id,
            str(item.get("id") or ""),
            int(item.get("testIdx") or 0),
            str(vars_.get("agent_under_test") or ""),
            str(vars_.get("eval_dimensions") or ""),
            1 if bool(item.get("success")) else 0,
            float(item.get("score") or 0),
            str(grading.get("reason") or ""),
            str(item.get("failureReason") or ""),
            int(item.get("latencyMs") or 0),
            json.dumps(stored_vars, ensure_ascii=True, sort_keys=True),
            json.dumps(stored_response, ensure_ascii=True, sort_keys=True),
            json.dumps(case_provenance["prompt_versions"], ensure_ascii=True, sort_keys=True),
            json.dumps(case_provenance["prompt_metadata"], ensure_ascii=True, sort_keys=True),
            case_provenance["prompt_metadata_hash"],
            case_provenance["model_provider"],
            case_provenance["model_name"],
            case_provenance["run_mode"],
            case_provenance["search_provider"],
            json.dumps(case_provenance["search_provider_sequence"], ensure_ascii=True, sort_keys=True),
            case_provenance["git_revision"],
            case_provenance["run_label"],
            storage_mode,
            _text_hash(raw_user_input),
            _text_hash(_response_text_for_hash(response)),
            _now(),
        ),
    )


def _promptfoo_run_provenance(
    payload: dict[str, Any],
    case_results: list[Any],
    *,
    result_path: Path,
) -> dict[str, Any]:
    first_case = next((item for item in case_results if isinstance(item, dict)), {})
    first_vars = first_case.get("vars") if isinstance(first_case.get("vars"), dict) else {}
    first_output = _promptfoo_case_output_payload(first_case)
    output_provenance = (
        first_output.get("provenance") if isinstance(first_output.get("provenance"), dict) else {}
    )
    metadata = _dict_first(
        payload.get("prompt_metadata"),
        first_vars.get("prompt_metadata"),
        output_provenance.get("prompt_metadata"),
    )
    prompt_versions = _list_first(
        payload.get("prompt_versions"),
        first_vars.get("prompt_versions"),
        output_provenance.get("prompt_versions"),
    )
    return {
        "prompt_versions": prompt_versions,
        "prompt_metadata": metadata,
        "prompt_metadata_hash": _metadata_hash(metadata),
        "model_provider": _str_first(
            payload.get("model_provider"),
            first_vars.get("model_provider"),
            output_provenance.get("model_provider"),
        ),
        "model_name": _str_first(
            payload.get("model_name"),
            first_vars.get("model_name"),
            output_provenance.get("model_name"),
        ),
        "run_mode": _str_first(
            payload.get("run_mode"),
            first_vars.get("run_mode"),
            output_provenance.get("run_mode"),
            "dry_run",
        ),
        "search_provider": _str_first(
            payload.get("search_provider"),
            first_vars.get("search_provider"),
            output_provenance.get("search_provider"),
        ),
        "search_provider_sequence": _list_first(
            payload.get("search_provider_sequence"),
            first_vars.get("search_provider_sequence"),
            output_provenance.get("search_provider_sequence"),
        ),
        "git_revision": _str_first(
            payload.get("git_revision"),
            first_vars.get("git_revision"),
            output_provenance.get("git_revision"),
        ),
        "run_label": _str_first(
            payload.get("run_label"),
            first_vars.get("run_label"),
            output_provenance.get("run_label"),
            result_path.stem,
        ),
    }


def _promptfoo_case_provenance(
    item: dict[str, Any],
    run_provenance: dict[str, Any],
) -> dict[str, Any]:
    vars_ = item.get("vars") if isinstance(item.get("vars"), dict) else {}
    output = _promptfoo_case_output_payload(item)
    output_provenance = output.get("provenance") if isinstance(output.get("provenance"), dict) else {}
    metadata = _dict_first(
        vars_.get("prompt_metadata"),
        output_provenance.get("prompt_metadata"),
        run_provenance.get("prompt_metadata"),
    )
    return {
        "prompt_versions": _list_first(
            vars_.get("prompt_versions"),
            output_provenance.get("prompt_versions"),
            run_provenance.get("prompt_versions"),
        ),
        "prompt_metadata": metadata,
        "prompt_metadata_hash": _metadata_hash(metadata),
        "model_provider": _str_first(
            vars_.get("model_provider"),
            output_provenance.get("model_provider"),
            run_provenance.get("model_provider"),
        ),
        "model_name": _str_first(
            vars_.get("model_name"),
            output_provenance.get("model_name"),
            run_provenance.get("model_name"),
        ),
        "run_mode": _str_first(
            vars_.get("run_mode"),
            output_provenance.get("run_mode"),
            run_provenance.get("run_mode"),
        ),
        "search_provider": _str_first(
            vars_.get("search_provider"),
            output_provenance.get("search_provider"),
            run_provenance.get("search_provider"),
        ),
        "search_provider_sequence": _list_first(
            vars_.get("search_provider_sequence"),
            output_provenance.get("search_provider_sequence"),
            run_provenance.get("search_provider_sequence"),
        ),
        "git_revision": _str_first(
            vars_.get("git_revision"),
            output_provenance.get("git_revision"),
            run_provenance.get("git_revision"),
        ),
        "run_label": _str_first(
            vars_.get("run_label"),
            output_provenance.get("run_label"),
            run_provenance.get("run_label"),
        ),
    }


def _promptfoo_case_output_payload(item: dict[str, Any]) -> dict[str, Any]:
    response = item.get("response") if isinstance(item.get("response"), dict) else {}
    output = response.get("output")
    if isinstance(output, dict):
        return output
    if isinstance(output, str) and output.strip().startswith("{"):
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _promptfoo_storage_mode(vars_: dict[str, Any], provenance: dict[str, Any]) -> str:
    requested = _str_first(vars_.get("storage_mode"), provenance.get("storage_mode"))
    if requested:
        return requested
    if _truthy(vars_.get("api_eval_safe_storage")):
        return "api_redacted"
    if str(provenance.get("run_mode") or "").strip() == "live_sdk":
        return "api_redacted"
    return "local_review"


def _run_storage_mode(
    *,
    storage_mode: str,
    run_mode: str,
    evidence: dict[str, Any],
) -> str:
    requested = _str_first(storage_mode, evidence.get("storage_mode"))
    if requested:
        return requested
    if str(run_mode or "").strip() == "live_sdk":
        return "api_redacted"
    if _truthy(evidence.get("api_eval_safe_storage")):
        return "api_redacted"
    return "local_review"


def _api_safe_vars(vars_: dict[str, Any], *, raw_user_input: str) -> dict[str, Any]:
    return {
        "storage_mode": "api_redacted",
        "case_id": str(vars_.get("case_id") or ""),
        "agent_under_test": str(vars_.get("agent_under_test") or ""),
        "eval_dimensions": str(vars_.get("eval_dimensions") or ""),
        "user_input_hash": _text_hash(raw_user_input),
        "user_input_summary": _redacted_prompt_summary(raw_user_input),
        "vars_keys": sorted(str(key) for key in vars_.keys()),
    }


def _api_safe_response(response: dict[str, Any]) -> dict[str, Any]:
    output = response.get("output")
    output_payload = _json_object_or_empty(output)
    summary = ""
    if output_payload:
        summary = str(output_payload.get("human_summary") or output_payload.get("response_summary") or "")
    elif isinstance(output, str):
        summary = output
    return {
        "storage_mode": "api_redacted",
        "response_text_hash": _text_hash(_response_text_for_hash(response)),
        "response_summary": _redact_eval_text(summary, max_chars=500),
        "response_keys": sorted(str(key) for key in response.keys()),
        "output_keys": sorted(str(key) for key in output_payload.keys()),
        "route": str(output_payload.get("route") or ""),
        "status": str(output_payload.get("status") or ""),
        "source_count": output_payload.get("source_count"),
        "side_effect_evidence_complete": output_payload.get("side_effect_evidence_complete"),
        "external_write_performed": output_payload.get("external_write_performed"),
    }


def _redacted_prompt_summary(value: str) -> str:
    digest = _text_hash(value)
    summary = _redact_eval_text(value, max_chars=160)
    if not summary:
        summary = "redacted API eval prompt"
    return f"{summary} [sha256:{digest}]"


def _response_text_for_hash(response: dict[str, Any]) -> str:
    output = response.get("output")
    if isinstance(output, str):
        return output
    return json.dumps(response, ensure_ascii=True, sort_keys=True)


def _text_hash(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _redact_eval_text(value: str, *, max_chars: int) -> str:
    text = str(value or "")
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", text)
    text = re.sub(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "[REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[REDACTED_EMAIL]", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b", "[REDACTED_PHONE]", text)
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[:max_chars] + "...[truncated]"
    return text


def _json_object_or_empty(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _metadata_hash(value: dict[str, Any]) -> str:
    if not value:
        return ""
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _str_first(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _dict_first(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict) and value:
            return value
        if isinstance(value, str) and value.strip().startswith("{"):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and parsed:
                return parsed
    return {}


def _list_first(*values: Any) -> list[Any]:
    for value in values:
        if isinstance(value, list) and value:
            return value
        if isinstance(value, tuple) and value:
            return list(value)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list) and parsed:
                    return parsed
            return [item.strip() for item in text.split(",") if item.strip()]
    return []


def _promptfoo_score_summary(case_results: list[Any]) -> dict[str, Any]:
    scores: list[float] = []
    agent_scores: dict[str, list[float]] = {}
    for item in case_results:
        if not isinstance(item, dict):
            continue
        score = _float_or_none(item.get("score"))
        if score is None:
            continue
        scores.append(score)
        vars_ = item.get("vars") if isinstance(item.get("vars"), dict) else {}
        agent = str(vars_.get("agent_under_test") or "unknown").strip() or "unknown"
        agent_scores.setdefault(agent, []).append(score)
    return {
        "average_score": _average(scores),
        "agent_scores": {
            agent: {
                "average_score": _average(values),
                "case_count": len(values),
            }
            for agent, values in sorted(agent_scores.items())
        },
    }


def _average(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _matching_eval_case(
    connection: sqlite3.Connection,
    *,
    normalized_request: str,
    agent: str = "",
) -> str:
    rows = connection.execute(
        """
        SELECT case_id, agent_under_test, user_input
        FROM eval_cases
        WHERE user_input != ''
        ORDER BY updated_at DESC, case_id
        """
    ).fetchall()
    normalized_agent = str(agent or "").strip()
    for row in rows:
        case_agent = str(row["agent_under_test"] or "").strip()
        if normalized_agent and case_agent and case_agent != normalized_agent:
            continue
        case_request = _normalize_eval_request_text(str(row["user_input"] or ""))
        if not case_request:
            continue
        if case_request == normalized_request:
            return str(row["case_id"] or "").strip()
        if (
            len(normalized_request) >= 40
            and (
                normalized_request in case_request
                or case_request in normalized_request
            )
        ):
            return str(row["case_id"] or "").strip()
    return ""


def _generated_slack_case_id(
    connection: sqlite3.Connection,
    *,
    normalized_request: str,
    request_text: str,
    agent: str,
) -> str:
    agent_slug = _slug(str(agent or "").strip() or "orchestrator", max_words=4)
    request_slug = _slug(normalized_request or request_text, max_words=6)
    base = f"slack_{agent_slug}_{request_slug}".strip("_")
    candidate = f"{base}_001"
    suffix = 1
    while True:
        row = connection.execute(
            "SELECT user_input FROM eval_cases WHERE case_id = ?",
            (candidate,),
        ).fetchone()
        if row is None:
            return candidate
        existing_request = _normalize_eval_request_text(str(row["user_input"] or ""))
        if existing_request == normalized_request:
            return candidate
        suffix += 1
        candidate = f"{base}_{suffix:03d}"


def _normalize_eval_request_text(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"<@[a-z0-9][a-z0-9._-]*>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"(^|\s)@kni\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\beval\s+case\s+[a-z0-9_.:-]+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bcase(?:_id)?\s*[:=]\s*[a-z0-9_.:-]+", " ", text, flags=re.IGNORECASE)
    text = text.strip(" :-")
    changed = True
    while changed:
        changed = False
        for prefix in _AGENT_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):].strip(" :-")
                changed = True
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _slug(value: str, *, max_words: int) -> str:
    words = re.findall(r"[a-z0-9]+", str(value or "").lower())
    selected = words[:max(1, max_words)]
    return "_".join(selected) or "case"


def _is_eval_channel(*, slack_channel_id: str, slack_channel_name: str) -> bool:
    return (
        str(slack_channel_id or "").strip() == DEFAULT_EVAL_SLACK_CHANNEL_ID
        or str(slack_channel_name or "").strip().lower().lstrip("#")
        == DEFAULT_EVAL_SLACK_CHANNEL_NAME
    )


def _upsert_eval_case(
    connection: sqlite3.Connection,
    *,
    case_id: str,
    agent_under_test: str = "",
    eval_dimensions: str = "",
    user_input: str = "",
    source: str = "",
) -> None:
    connection.execute(
        """
        INSERT INTO eval_cases (
            case_id, agent_under_test, eval_dimensions, user_input, source,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(case_id) DO UPDATE SET
            agent_under_test = COALESCE(
                NULLIF(excluded.agent_under_test, ''),
                eval_cases.agent_under_test
            ),
            eval_dimensions = COALESCE(
                NULLIF(excluded.eval_dimensions, ''),
                eval_cases.eval_dimensions
            ),
            user_input = COALESCE(NULLIF(excluded.user_input, ''), eval_cases.user_input),
            source = COALESCE(NULLIF(excluded.source, ''), eval_cases.source),
            updated_at = excluded.updated_at
        """,
        (
            case_id,
            agent_under_test,
            eval_dimensions,
            user_input,
            source,
            _now(),
            _now(),
        ),
    )


def _ensure_eval_schema(connection: sqlite3.Connection) -> None:
    ensure_human_review_schema(connection)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_cases (
            case_id TEXT PRIMARY KEY,
            agent_under_test TEXT NOT NULL DEFAULT '',
            eval_dimensions TEXT NOT NULL DEFAULT '',
            user_input TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS promptfoo_eval_runs (
            eval_id TEXT PRIMARY KEY,
            result_path TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            imported_at TEXT NOT NULL,
            total INTEGER NOT NULL DEFAULT 0,
            successes INTEGER NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            stats_json TEXT NOT NULL DEFAULT '{}',
            average_score REAL,
            agent_scores_json TEXT
        )
        """
    )
    _add_column_if_missing(connection, "promptfoo_eval_runs", "average_score", "REAL")
    _add_column_if_missing(connection, "promptfoo_eval_runs", "agent_scores_json", "TEXT")
    for column_name, column_definition in (
        ("prompt_versions_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("prompt_metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("prompt_metadata_hash", "TEXT NOT NULL DEFAULT ''"),
        ("model_provider", "TEXT NOT NULL DEFAULT ''"),
        ("model_name", "TEXT NOT NULL DEFAULT ''"),
        ("run_mode", "TEXT NOT NULL DEFAULT ''"),
        ("search_provider", "TEXT NOT NULL DEFAULT ''"),
        ("search_provider_sequence_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("git_revision", "TEXT NOT NULL DEFAULT ''"),
        ("run_label", "TEXT NOT NULL DEFAULT ''"),
    ):
        _add_column_if_missing(connection, "promptfoo_eval_runs", column_name, column_definition)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS promptfoo_case_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            eval_id TEXT NOT NULL,
            case_id TEXT NOT NULL,
            result_id TEXT NOT NULL DEFAULT '',
            test_idx INTEGER NOT NULL DEFAULT 0,
            agent_under_test TEXT NOT NULL DEFAULT '',
            eval_dimensions TEXT NOT NULL DEFAULT '',
            success INTEGER NOT NULL DEFAULT 0,
            score REAL NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            failure_reason TEXT NOT NULL DEFAULT '',
            latency_ms INTEGER NOT NULL DEFAULT 0,
            vars_json TEXT NOT NULL DEFAULT '{}',
            output_json TEXT NOT NULL DEFAULT '{}',
            prompt_versions_json TEXT NOT NULL DEFAULT '[]',
            prompt_metadata_json TEXT NOT NULL DEFAULT '{}',
            prompt_metadata_hash TEXT NOT NULL DEFAULT '',
            model_provider TEXT NOT NULL DEFAULT '',
            model_name TEXT NOT NULL DEFAULT '',
            run_mode TEXT NOT NULL DEFAULT '',
            search_provider TEXT NOT NULL DEFAULT '',
            search_provider_sequence_json TEXT NOT NULL DEFAULT '[]',
            git_revision TEXT NOT NULL DEFAULT '',
            run_label TEXT NOT NULL DEFAULT '',
            storage_mode TEXT NOT NULL DEFAULT 'local_review',
            prompt_text_hash TEXT NOT NULL DEFAULT '',
            response_text_hash TEXT NOT NULL DEFAULT '',
            imported_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES eval_cases(case_id)
        )
        """
    )
    for column_name, column_definition in (
        ("prompt_versions_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("prompt_metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("prompt_metadata_hash", "TEXT NOT NULL DEFAULT ''"),
        ("model_provider", "TEXT NOT NULL DEFAULT ''"),
        ("model_name", "TEXT NOT NULL DEFAULT ''"),
        ("run_mode", "TEXT NOT NULL DEFAULT ''"),
        ("search_provider", "TEXT NOT NULL DEFAULT ''"),
        ("search_provider_sequence_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("git_revision", "TEXT NOT NULL DEFAULT ''"),
        ("run_label", "TEXT NOT NULL DEFAULT ''"),
        ("storage_mode", "TEXT NOT NULL DEFAULT 'local_review'"),
        ("prompt_text_hash", "TEXT NOT NULL DEFAULT ''"),
        ("response_text_hash", "TEXT NOT NULL DEFAULT ''"),
    ):
        _add_column_if_missing(connection, "promptfoo_case_results", column_name, column_definition)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS slack_eval_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT NOT NULL,
            run_id TEXT NOT NULL DEFAULT '',
            agent TEXT NOT NULL DEFAULT '',
            work_item_id TEXT NOT NULL DEFAULT '',
            slack_channel_id TEXT NOT NULL DEFAULT '',
            slack_channel_name TEXT NOT NULL DEFAULT '',
            slack_thread_ts TEXT NOT NULL DEFAULT '',
            permalink TEXT NOT NULL DEFAULT '',
            request_text TEXT NOT NULL DEFAULT '',
            result_summary TEXT NOT NULL DEFAULT '',
            route TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            context_policy TEXT NOT NULL DEFAULT '',
            thread_fetch_status TEXT NOT NULL DEFAULT '',
            thread_message_count INTEGER NOT NULL DEFAULT 0,
            warning_count INTEGER NOT NULL DEFAULT 0,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            cost_profile TEXT NOT NULL DEFAULT '',
            source_count INTEGER NOT NULL DEFAULT 0,
            visible_source_count INTEGER NOT NULL DEFAULT 0,
            sdk_estimated_cost_usd REAL,
            sdk_cache_hit_rate REAL,
            duration_ms REAL,
            response_hash TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            prompt_versions_json TEXT NOT NULL DEFAULT '[]',
            prompt_metadata_json TEXT NOT NULL DEFAULT '{}',
            prompt_metadata_hash TEXT NOT NULL DEFAULT '',
            model_provider TEXT NOT NULL DEFAULT '',
            model_name TEXT NOT NULL DEFAULT '',
            run_mode TEXT NOT NULL DEFAULT '',
            search_provider TEXT NOT NULL DEFAULT '',
            search_provider_sequence_json TEXT NOT NULL DEFAULT '[]',
            git_revision TEXT NOT NULL DEFAULT '',
            run_label TEXT NOT NULL DEFAULT '',
            storage_mode TEXT NOT NULL DEFAULT 'local_review',
            request_text_hash TEXT NOT NULL DEFAULT '',
            result_summary_hash TEXT NOT NULL DEFAULT '',
            attempt_group_id TEXT NOT NULL DEFAULT '',
            duplicate_attempt INTEGER NOT NULL DEFAULT 0,
            duplicate_of_run_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES eval_cases(case_id)
        )
        """
    )
    _add_column_if_missing(connection, "slack_eval_runs", "work_item_id", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "slack_eval_runs", "permalink", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "slack_eval_runs", "route", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "slack_eval_runs", "status", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "slack_eval_runs", "context_policy", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "slack_eval_runs", "thread_fetch_status", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "slack_eval_runs", "thread_message_count", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(connection, "slack_eval_runs", "warning_count", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(connection, "slack_eval_runs", "warnings_json", "TEXT NOT NULL DEFAULT '[]'")
    _add_column_if_missing(connection, "slack_eval_runs", "cost_profile", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "slack_eval_runs", "source_count", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(connection, "slack_eval_runs", "visible_source_count", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(connection, "slack_eval_runs", "sdk_estimated_cost_usd", "REAL")
    _add_column_if_missing(connection, "slack_eval_runs", "sdk_cache_hit_rate", "REAL")
    _add_column_if_missing(connection, "slack_eval_runs", "duration_ms", "REAL")
    _add_column_if_missing(connection, "slack_eval_runs", "response_hash", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "slack_eval_runs", "evidence_json", "TEXT NOT NULL DEFAULT '{}'")
    for column_name, column_definition in (
        ("prompt_versions_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("prompt_metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("prompt_metadata_hash", "TEXT NOT NULL DEFAULT ''"),
        ("model_provider", "TEXT NOT NULL DEFAULT ''"),
        ("model_name", "TEXT NOT NULL DEFAULT ''"),
        ("run_mode", "TEXT NOT NULL DEFAULT ''"),
        ("search_provider", "TEXT NOT NULL DEFAULT ''"),
        ("search_provider_sequence_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("git_revision", "TEXT NOT NULL DEFAULT ''"),
        ("run_label", "TEXT NOT NULL DEFAULT ''"),
        ("storage_mode", "TEXT NOT NULL DEFAULT 'local_review'"),
        ("duration_ms", "REAL"),
        ("request_text_hash", "TEXT NOT NULL DEFAULT ''"),
        ("result_summary_hash", "TEXT NOT NULL DEFAULT ''"),
        ("attempt_group_id", "TEXT NOT NULL DEFAULT ''"),
        ("duplicate_attempt", "INTEGER NOT NULL DEFAULT 0"),
        ("duplicate_of_run_id", "TEXT NOT NULL DEFAULT ''"),
    ):
        _add_column_if_missing(connection, "slack_eval_runs", column_name, column_definition)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS promptfoo_analysis_exclusions (
            eval_id TEXT NOT NULL,
            case_id TEXT NOT NULL,
            excluded INTEGER NOT NULL DEFAULT 1,
            reason TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(eval_id, case_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_trace_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            trace_id TEXT NOT NULL DEFAULT '',
            span_id TEXT NOT NULL DEFAULT '',
            parent_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            group_id TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            duration_ms REAL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_promptfoo_case_results_case
        ON promptfoo_case_results(case_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_promptfoo_case_results_eval
        ON promptfoo_case_results(eval_id)
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_slack_eval_runs_case ON slack_eval_runs(case_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_slack_eval_runs_attempt_group ON slack_eval_runs(attempt_group_id)"
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_promptfoo_analysis_exclusions_case
        ON promptfoo_analysis_exclusions(case_id)
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_trace_events_trace ON eval_trace_events(trace_id)"
    )


def _add_column_if_missing(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    if column_name in {str(row[1]) for row in rows}:
        return
    connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def _json_row(row: dict[str, Any], json_fields: tuple[str, ...]) -> dict[str, Any]:
    payload = dict(row)
    for field in json_fields:
        default = [] if field.endswith("versions_json") or field.endswith("sequence_json") else {}
        try:
            parsed = json.loads(str(payload.pop(field) or json.dumps(default)))
        except json.JSONDecodeError:
            parsed = default
        payload[field.removesuffix("_json")] = parsed if isinstance(parsed, type(default)) else default
    payload["success"] = bool(payload.get("success"))
    return payload


def _slack_eval_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    for field, default in (
        ("warnings_json", []),
        ("evidence_json", {}),
        ("prompt_versions_json", []),
        ("prompt_metadata_json", {}),
        ("search_provider_sequence_json", []),
    ):
        raw = payload.pop(field, None)
        try:
            parsed = json.loads(str(raw or json.dumps(default)))
        except json.JSONDecodeError:
            parsed = default
        payload[field.removesuffix("_json")] = parsed if isinstance(parsed, type(default)) else default
    for field in (
        "thread_message_count",
        "warning_count",
        "source_count",
        "visible_source_count",
        "duplicate_attempt",
    ):
        payload[field] = int(payload.get(field) or 0)
    return payload


def _slack_attempt_group_id(row_values: dict[str, Any]) -> str:
    payload = {
        "case_id": str(row_values.get("case_id") or "").strip(),
        "slack_thread_ts": str(row_values.get("slack_thread_ts") or "").strip(),
        "request_text_hash": str(row_values.get("request_text_hash") or "").strip(),
        "response_hash": str(row_values.get("response_hash") or "").strip()
        or str(row_values.get("result_summary_hash") or "").strip(),
        "status": str(row_values.get("status") or "").strip(),
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _existing_slack_attempt_group(
    connection: sqlite3.Connection,
    *,
    attempt_group_id: str,
    run_id: str,
    work_item_id: str,
) -> dict[str, Any]:
    if not attempt_group_id:
        return {}
    try:
        row = connection.execute(
            """
            SELECT id, run_id, work_item_id
            FROM slack_eval_runs
            WHERE attempt_group_id = ?
              AND NOT (
                (length(trim(COALESCE(run_id, ''))) > 0 AND run_id = ?)
                OR (length(trim(COALESCE(work_item_id, ''))) > 0 AND work_item_id = ?)
              )
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (attempt_group_id, run_id, work_item_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    return dict(row) if row is not None else {}


def _session_join_metadata_from_row(row: dict[str, Any]) -> dict[str, Any]:
    evidence = _json_value(row.get("evidence_json"), {})
    if not evidence and isinstance(row.get("evidence"), dict):
        evidence = row["evidence"]
    if isinstance(evidence, dict):
        for key in ("sdk_session", "session", "sdk_session_metadata"):
            value = evidence.get(key)
            if isinstance(value, dict):
                return {
                    "scope": str(value.get("scope") or "").strip(),
                    "source": str(value.get("source") or "").strip(),
                    "session_id_hash": str(value.get("session_id_hash") or "").strip(),
                    "session_history_limit": _safe_int(value.get("session_history_limit")),
                    "work_item_id": str(row.get("work_item_id") or row.get("run_id") or "").strip(),
                }
    return {
        "scope": "",
        "source": "",
        "session_id_hash": "",
        "session_history_limit": 0,
        "work_item_id": str(row.get("work_item_id") or row.get("run_id") or "").strip(),
    }


def _manual_trace_string_list(value: Any, *, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    labels: list[str] = []
    for item in value:
        if isinstance(item, dict):
            raw = item.get("type") or item.get("code") or item.get("label") or item.get("name")
        else:
            raw = item
        label = str(raw or "").strip()
        if label:
            labels.append(label[:80])
        if len(labels) >= limit:
            break
    return labels


def _manual_trace_child_steps(value: Any, *, limit: int = 40) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    steps: list[dict[str, Any]] = []
    for raw in value[:limit]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        category = str(raw.get("category") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:+/-]{1,120}", name):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.:+/-]{1,120}", category):
            continue
        status = str(raw.get("status") or "observed").strip()
        error_kind = str(raw.get("error_kind") or "").strip()
        provider = str(raw.get("provider") or "").strip()
        steps.append(
            {
                "step_index": len(steps) + 1,
                "category": category,
                "name": name,
                "status": status
                if re.fullmatch(r"[A-Za-z0-9_.:+/-]{1,120}", status)
                else "observed",
                "duration_ms": _float_or_none(raw.get("duration_ms")),
                "error_kind": error_kind
                if not error_kind
                or re.fullmatch(r"[A-Za-z0-9_.:+/-]{1,120}", error_kind)
                else "",
                "provider": provider
                if not provider or re.fullmatch(r"[A-Za-z0-9_.:+/-]{1,120}", provider)
                else "",
                "request_count": _safe_int(raw.get("request_count")),
                "source_count": _safe_int(raw.get("source_count")),
                "visible_source_count": _safe_int(raw.get("visible_source_count")),
                "estimated_cost_usd": _float_or_none(raw.get("estimated_cost_usd")),
                "cache_hit_rate": _float_or_none(raw.get("cache_hit_rate")),
                "approval_required": bool(raw.get("approval_required")),
                "blocker_count": _safe_int(raw.get("blocker_count")),
            }
        )
    return steps


def _manual_trace_bool(payload: dict[str, Any], keys: tuple[str, ...], *, default: bool = False) -> bool:
    for key in keys:
        if key in payload:
            return bool(payload.get(key))
    return default


def _manual_trace_diagnostics_from_row(row: dict[str, Any]) -> dict[str, Any]:
    evidence = _json_value(row.get("evidence_json"), {})
    if not evidence and isinstance(row.get("evidence"), dict):
        evidence = row["evidence"]
    warnings = _json_value(row.get("warnings_json"), [])
    if not warnings and isinstance(row.get("warnings"), list):
        warnings = row["warnings"]
    prompt_versions = _json_value(row.get("prompt_versions_json"), [])
    if not prompt_versions and isinstance(row.get("prompt_versions"), list):
        prompt_versions = row["prompt_versions"]
    search_sequence = _json_value(row.get("search_provider_sequence_json"), [])
    if not search_sequence and isinstance(row.get("search_provider_sequence"), list):
        search_sequence = row["search_provider_sequence"]

    orchestrator = evidence.get("orchestrator") if isinstance(evidence.get("orchestrator"), dict) else {}
    orchestrator_preflight = (
        evidence.get("orchestrator_preflight")
        if isinstance(evidence.get("orchestrator_preflight"), dict)
        else {}
    )
    orchestrator_review = (
        evidence.get("orchestrator_review") if isinstance(evidence.get("orchestrator_review"), dict) else {}
    )
    extraction = evidence.get("web_extraction") if isinstance(evidence.get("web_extraction"), dict) else {}
    if not extraction and isinstance(evidence.get("extraction"), dict):
        extraction = evidence["extraction"]
    tool_summary = evidence.get("tool_summary") if isinstance(evidence.get("tool_summary"), dict) else {}
    if not tool_summary and isinstance(evidence.get("tooling"), dict):
        tool_summary = evidence["tooling"]
    approval = evidence.get("approval") if isinstance(evidence.get("approval"), dict) else {}
    if not approval and isinstance(evidence.get("approval_gate"), dict):
        approval = evidence["approval_gate"]
    side_effects = evidence.get("side_effects") if isinstance(evidence.get("side_effects"), dict) else {}
    blocker_diagnostics = (
        evidence.get("blocker_diagnostics")
        if isinstance(evidence.get("blocker_diagnostics"), dict)
        else {}
    )
    child_steps = _manual_trace_child_steps(evidence.get("child_step_summary"))
    retry_state = evidence.get("retry_state") if isinstance(evidence.get("retry_state"), dict) else {}
    if not retry_state and isinstance(evidence.get("retry"), dict):
        retry_state = evidence["retry"]
    execution = evidence.get("execution") if isinstance(evidence.get("execution"), dict) else {}
    timing = evidence.get("timing") if isinstance(evidence.get("timing"), dict) else {}
    duration_ms = _float_or_none(
        row.get("duration_ms")
        or execution.get("duration_ms")
        or execution.get("time_to_response_ms")
        or timing.get("duration_ms")
        or timing.get("time_to_response_ms")
        or evidence.get("duration_ms")
        or evidence.get("time_to_response_ms")
    )

    warning_count = _safe_int(row.get("warning_count"))
    row_status = str(row.get("status") or "").strip().lower()
    retry_count = _safe_int(retry_state.get("retry_count") or retry_state.get("attempt_count"))
    web_extraction_issue_count = _safe_int(
        extraction.get("issue_count") or extraction.get("warning_count") or extraction.get("error_count")
    )
    tool_names = _manual_trace_string_list(
        tool_summary.get("tool_names") or tool_summary.get("tools") or evidence.get("tool_names") or []
    )
    has_orchestrator_preflight = bool(
        orchestrator.get("preflight")
        or orchestrator.get("route_advice")
        or orchestrator.get("planner_rationale")
        or orchestrator_preflight
    )
    has_orchestrator_review = bool(
        orchestrator.get("review")
        or orchestrator.get("feedback")
        or orchestrator.get("review_summary")
        or orchestrator_review
    )
    model_provider = str(row.get("model_provider") or "").strip()
    model_name = str(row.get("model_name") or "").strip()
    search_provider = str(row.get("search_provider") or "").strip()
    tool_call_count = _safe_int(tool_summary.get("tool_call_count") or tool_summary.get("count"))
    failed_tool_call_count = _safe_int(
        tool_summary.get("failed_tool_call_count") or tool_summary.get("failure_count")
    )
    external_write_performed = _manual_trace_bool(
        side_effects,
        ("external_write_performed", "write_performed", "sent", "posted", "scheduled"),
        default=False,
    )
    send_enabled = _manual_trace_bool(approval, ("send_enabled",), default=False) or _manual_trace_bool(
        side_effects,
        ("send_enabled",),
        default=False,
    )
    approval_required = _manual_trace_bool(approval, ("approval_required", "required"), default=False)

    return {
        "diagnostic_contract": {
            "schema": "keystone.eval_run_diagnostics.v1",
            "included": [
                "timing",
                "model",
                "tooling",
                "retrieval",
                "orchestrator",
                "blocker",
                "child_steps",
                "approval",
                "side_effects",
                "error_retry",
                "prompt_version",
            ],
            "raw_payloads_included": False,
            "logs_hold_verbose_details": True,
            "future_api_expected": [
                "sdk_run_summary",
                "tool_call_summary",
                "orchestrator_review",
                "web_extraction_summary",
            ],
        },
        "execution": {
            "run_mode": str(row.get("run_mode") or "").strip(),
            "run_label": str(row.get("run_label") or "").strip(),
            "git_revision": str(row.get("git_revision") or "").strip(),
            "duration_ms": duration_ms,
            "time_to_response_ms": duration_ms,
        },
        "slack_context": {
            "channel_id": str(row.get("slack_channel_id") or "").strip(),
            "channel_name": str(row.get("slack_channel_name") or "").strip(),
            "thread_ts": str(row.get("slack_thread_ts") or "").strip(),
            "permalink_present": bool(str(row.get("permalink") or "").strip()),
        },
        "model": {
            "provider": model_provider,
            "name": model_name,
            "has_model_config": bool(model_provider or model_name),
        },
        "tooling": {
            "tool_call_count": tool_call_count,
            "failed_tool_call_count": failed_tool_call_count,
            "tool_names": tool_names,
            "has_tool_metadata": bool(tool_call_count or failed_tool_call_count or tool_names),
        },
        "child_steps": {
            "count": len(child_steps),
            "timeline": child_steps,
            "raw_payloads_included": False,
        },
        "retrieval": {
            "search_provider": search_provider,
            "search_provider_sequence": _manual_trace_string_list(search_sequence),
            "source_count": _safe_int(row.get("source_count")),
            "visible_source_count": _safe_int(row.get("visible_source_count")),
            "web_extraction_status": str(extraction.get("status") or "").strip(),
            "web_extraction_issue_count": web_extraction_issue_count,
        },
        "cost": {
            "cost_profile": str(row.get("cost_profile") or "").strip(),
            "sdk_estimated_cost_usd": _float_or_none(row.get("sdk_estimated_cost_usd")),
            "sdk_cache_hit_rate": _float_or_none(row.get("sdk_cache_hit_rate")),
        },
        "orchestrator": {
            "has_preflight": has_orchestrator_preflight,
            "has_review": has_orchestrator_review,
            "feedback_count": _safe_int(orchestrator.get("feedback_count") or orchestrator_review.get("feedback_count")),
            "blocker_count": max(
                _safe_int(
                    orchestrator.get("blocker_count")
                    or orchestrator_preflight.get("blocker_count")
                ),
                _safe_int(blocker_diagnostics.get("blocker_count")),
            ),
        },
        "blocker": {
            "diagnostic_category": str(
                blocker_diagnostics.get("diagnostic_category") or ""
            ).strip(),
            "block_kind": str(blocker_diagnostics.get("block_kind") or "").strip(),
            "block_reason": str(blocker_diagnostics.get("block_reason") or "").strip(),
            "blocker_count": _safe_int(blocker_diagnostics.get("blocker_count")),
            "blocker_codes": _manual_trace_string_list(
                blocker_diagnostics.get("blocker_codes") or []
            ),
            "readiness_gate_names": _manual_trace_string_list(
                blocker_diagnostics.get("readiness_gate_names") or []
            ),
            "next_action": (
                blocker_diagnostics.get("next_action")
                if isinstance(blocker_diagnostics.get("next_action"), dict)
                else {}
            ),
        },
        "approval": {
            "required": approval_required,
            "status": str(approval.get("status") or approval.get("approval_status") or "").strip(),
            "send_enabled": send_enabled,
        },
        "side_effects": {
            "external_write_performed": external_write_performed,
            "storage_mode": str(row.get("storage_mode") or "").strip() or "local_review",
        },
        "error_retry": {
            "warning_count": warning_count,
            "warning_types": _manual_trace_string_list(warnings),
            "retry_count": retry_count,
            "retry_status": str(retry_state.get("status") or "").strip(),
        },
        "prompt_version": {
            "prompt_version_count": len(prompt_versions),
            "prompt_metadata_hash": str(row.get("prompt_metadata_hash") or "").strip(),
            "request_text_hash": str(row.get("request_text_hash") or "").strip(),
            "result_summary_hash": str(row.get("result_summary_hash") or "").strip(),
            "response_hash": str(row.get("response_hash") or "").strip(),
        },
        "diagnostic_summary": {
            "has_model_metadata": bool(model_provider or model_name),
            "has_tool_metadata": bool(tool_call_count or failed_tool_call_count or tool_names),
            "has_retrieval_metadata": bool(search_provider or search_sequence or row.get("source_count")),
            "has_orchestrator_feedback": has_orchestrator_preflight or has_orchestrator_review,
            "has_web_extraction_issues": web_extraction_issue_count > 0,
            "has_error_or_retry": (
                row_status == "blocked"
                or warning_count > 0
                or retry_count > 0
                or failed_tool_call_count > 0
            ),
            "has_blocker_metadata": bool(blocker_diagnostics),
            "blocked_without_diagnostics": row_status == "blocked" and not blocker_diagnostics,
            "has_child_step_metadata": bool(child_steps),
        },
    }


def _manual_slack_run_summary_from_row(row: dict[str, Any]) -> dict[str, Any]:
    row_id = _safe_int(row.get("id"))
    run_id = str(row.get("run_id") or "").strip()
    work_item_id = str(row.get("work_item_id") or run_id or "").strip()
    trace_key = work_item_id or run_id or str(row_id)
    case_id = str(row.get("case_id") or "").strip()
    diagnostics = _manual_trace_diagnostics_from_row(row)
    return {
        "trace_id": f"manual_run:{trace_key}",
        "span_id": f"slack_run_summary:{row_id}",
        "name": "slack_eval_manual_run_summary",
        "group_id": case_id,
        "metadata": {
            "schema": "keystone.manual_run_summary.v1",
            "stage": "slack_eval_saved_no_api",
            "source": "slack",
            "live_api_call": False,
            "agent": str(row.get("agent") or "").strip(),
            "route": str(row.get("route") or row.get("agent") or "").strip(),
            "status": str(row.get("status") or "").strip(),
            "correlation": {
                "case_id": case_id,
                "run_id": run_id,
                "work_item_id": work_item_id,
                "slack_channel_id": str(row.get("slack_channel_id") or "").strip(),
                "slack_channel_name": str(row.get("slack_channel_name") or "").strip(),
                "slack_thread_ts": str(row.get("slack_thread_ts") or "").strip(),
            },
            "thread_evidence": {
                "thread_fetch_status": str(row.get("thread_fetch_status") or "").strip(),
                "message_count": _safe_int(row.get("thread_message_count")),
                "warning_count": _safe_int(row.get("warning_count")),
            },
            "source_visibility": {
                "source_count": _safe_int(row.get("source_count")),
                "visible_source_count": _safe_int(row.get("visible_source_count")),
            },
            "sdk_session": _session_join_metadata_from_row(row),
            "attempt_group": {
                "attempt_group_id": str(row.get("attempt_group_id") or "").strip(),
                "duplicate_attempt": bool(row.get("duplicate_attempt")),
                "duplicate_of_run_id": str(row.get("duplicate_of_run_id") or "").strip(),
            },
            "storage_mode": str(row.get("storage_mode") or "").strip() or "local_review",
            "row_id": row_id,
            "slack_run_created_at": str(row.get("created_at") or "").strip(),
            **diagnostics,
        },
    }


def _insert_manual_slack_run_summary_event(
    connection: sqlite3.Connection,
    summary: dict[str, Any],
) -> int:
    metadata = summary.get("metadata") if isinstance(summary.get("metadata"), dict) else {}
    execution = metadata.get("execution") if isinstance(metadata.get("execution"), dict) else {}
    return _insert_eval_trace_event(
        connection,
        event_type="manual_run_summary",
        trace_id=str(summary.get("trace_id") or ""),
        span_id=str(summary.get("span_id") or ""),
        name=str(summary.get("name") or ""),
        group_id=str(summary.get("group_id") or ""),
        metadata=metadata,
        duration_ms=_float_or_none(execution.get("duration_ms")),
    )


def _manual_summary_has_diagnostic_contract(row: dict[str, Any]) -> bool:
    try:
        metadata = json.loads(str(row.get("metadata_json") or "{}"))
    except json.JSONDecodeError:
        return False
    return isinstance(metadata, dict) and isinstance(metadata.get("diagnostic_contract"), dict)


def _update_manual_slack_run_summary_event(
    connection: sqlite3.Connection,
    existing: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    metadata = summary.get("metadata") if isinstance(summary.get("metadata"), dict) else {}
    execution = metadata.get("execution") if isinstance(metadata.get("execution"), dict) else {}
    connection.execute(
        """
        UPDATE eval_trace_events
        SET trace_id = ?,
            span_id = ?,
            parent_id = '',
            name = ?,
            group_id = ?,
            metadata_json = ?,
            duration_ms = ?
        WHERE id = ?
        """,
        (
            str(summary.get("trace_id") or ""),
            str(summary.get("span_id") or ""),
            str(summary.get("name") or ""),
            str(summary.get("group_id") or ""),
            json.dumps(metadata, ensure_ascii=True, sort_keys=True),
            _float_or_none(execution.get("duration_ms")),
            int(existing.get("id") or 0),
        ),
    )


def _eval_trace_join_key(group_id: str, metadata: dict[str, Any]) -> str:
    correlation = metadata.get("correlation") if isinstance(metadata, dict) else {}
    if not isinstance(correlation, dict):
        correlation = {}
    return str(
        group_id
        or metadata.get("case_id")
        or correlation.get("case_id")
        or metadata.get("work_item_id")
        or correlation.get("work_item_id")
        or metadata.get("run_id")
        or correlation.get("run_id")
        or ""
    ).strip()


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
