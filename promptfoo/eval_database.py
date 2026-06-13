"""Local eval database linking Promptfoo results, Slack runs, and human scores."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc

from promptfoo.human_review import (
    DEFAULT_REVIEW_DB,
    ensure_human_review_schema,
    list_human_reviews,
)

DEFAULT_EVAL_DB = DEFAULT_REVIEW_DB
DEFAULT_EVAL_SLACK_CHANNEL_ID = "C0BA17Y9C01"
DEFAULT_EVAL_SLACK_CHANNEL_NAME = "evals"
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

    db_path = Path(database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        _ensure_eval_schema(connection)
        connection.execute(
            """
            INSERT OR REPLACE INTO promptfoo_eval_runs (
                eval_id, result_path, created_at, imported_at, total, successes,
                failures, errors, stats_json, average_score, agent_scores_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        connection.execute("DELETE FROM promptfoo_case_results WHERE eval_id = ?", (eval_id,))
        for item in case_results:
            if isinstance(item, dict):
                _insert_promptfoo_case_result(connection, eval_id, item)
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
    slack_channel_id: str = "C0BA17Y9C01",
    slack_channel_name: str = "evals",
    slack_thread_ts: str = "",
    request_text: str = "",
    result_summary: str = "",
    database_path: str | Path = DEFAULT_EVAL_DB,
) -> int:
    """Record a real Slack agent run for a Promptfoo-linked eval case."""

    normalized_case_id = str(case_id or "").strip()
    if not normalized_case_id:
        raise ValueError("case_id is required")
    db_path = Path(database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        _ensure_eval_schema(connection)
        _upsert_eval_case(
            connection,
            case_id=normalized_case_id,
            agent_under_test=agent,
            user_input=request_text,
            source="slack",
        )
        cursor = connection.execute(
            """
            INSERT INTO slack_eval_runs (
                case_id, run_id, agent, slack_channel_id, slack_channel_name,
                slack_thread_ts, request_text, result_summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_case_id,
                str(run_id or "").strip(),
                str(agent or "").strip(),
                str(slack_channel_id or "").strip(),
                str(slack_channel_name or "").strip(),
                str(slack_thread_ts or "").strip(),
                str(request_text or "").strip(),
                str(result_summary or "").strip(),
                _now(),
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


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
    normalized_request = _normalize_eval_request_text(request_text)
    if not normalized_request:
        return ""
    db_path = Path(database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_eval_schema(connection)
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
    with sqlite3.connect(db_path) as connection:
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
    db_path = Path(database_path)
    promptfoo_results: list[dict[str, Any]] = []
    slack_runs: list[dict[str, Any]] = []
    case: dict[str, Any] | None = None
    if db_path.exists():
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            _ensure_eval_schema(connection)
            case_row = connection.execute(
                "SELECT * FROM eval_cases WHERE case_id = ?",
                (normalized_case_id,),
            ).fetchone()
            if case_row:
                case = dict(case_row)
            promptfoo_rows = connection.execute(
                """
                SELECT p.*,
                       COALESCE(e.excluded, 0) AS analysis_excluded,
                       COALESCE(e.reason, '') AS analysis_exclusion_reason
                FROM promptfoo_case_results p
                LEFT JOIN promptfoo_analysis_exclusions e
                  ON e.eval_id = p.eval_id AND e.case_id = p.case_id
                WHERE p.case_id = ?
                ORDER BY p.imported_at DESC, p.id DESC
                """,
                (normalized_case_id,),
            ).fetchall()
            promptfoo_results = [
                _json_row(dict(row), ("vars_json", "output_json"))
                for row in promptfoo_rows
            ]
            slack_rows = connection.execute(
                """
                SELECT * FROM slack_eval_runs
                WHERE case_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (normalized_case_id,),
            ).fetchall()
            slack_runs = [dict(row) for row in slack_rows]

    human_reviews = list_human_reviews(database_path=db_path, case_id=normalized_case_id)
    latest_promptfoo = promptfoo_results[0] if promptfoo_results else None
    latest_human = human_reviews[-1] if human_reviews else None
    return {
        "case_id": normalized_case_id,
        "database_path": str(db_path),
        "case": case,
        "latest_promptfoo": latest_promptfoo,
        "latest_human_review": latest_human,
        "promptfoo_result_count": len(promptfoo_results),
        "human_review_count": len(human_reviews),
        "slack_run_count": len(slack_runs),
        "promptfoo_results": promptfoo_results,
        "human_reviews": human_reviews,
        "slack_runs": slack_runs,
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
    with sqlite3.connect(db_path) as connection:
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


def list_eval_cases(
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List cases with latest Promptfoo and human-review status."""

    db_path = Path(database_path)
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as connection:
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
                ORDER BY id DESC
                LIMIT 1
            )
            ORDER BY c.updated_at DESC, c.case_id
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [dict(row) for row in rows]


def _insert_promptfoo_case_result(
    connection: sqlite3.Connection,
    eval_id: str,
    item: dict[str, Any],
) -> None:
    vars_ = item.get("vars") if isinstance(item.get("vars"), dict) else {}
    grading = item.get("gradingResult") if isinstance(item.get("gradingResult"), dict) else {}
    response = item.get("response") if isinstance(item.get("response"), dict) else {}
    case_id = str(vars_.get("case_id") or item.get("id") or "").strip()
    if not case_id:
        return
    _upsert_eval_case(
        connection,
        case_id=case_id,
        agent_under_test=str(vars_.get("agent_under_test") or ""),
        eval_dimensions=str(vars_.get("eval_dimensions") or ""),
        user_input=str(vars_.get("user_input") or ""),
        source="promptfoo",
    )
    connection.execute(
        """
        INSERT INTO promptfoo_case_results (
            eval_id, case_id, result_id, test_idx, agent_under_test,
            eval_dimensions, success, score, reason, failure_reason,
            latency_ms, vars_json, output_json, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            json.dumps(vars_, ensure_ascii=True, sort_keys=True),
            json.dumps(response, ensure_ascii=True, sort_keys=True),
            _now(),
        ),
    )


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
            imported_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES eval_cases(case_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS slack_eval_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT NOT NULL,
            run_id TEXT NOT NULL DEFAULT '',
            agent TEXT NOT NULL DEFAULT '',
            slack_channel_id TEXT NOT NULL DEFAULT '',
            slack_channel_name TEXT NOT NULL DEFAULT '',
            slack_thread_ts TEXT NOT NULL DEFAULT '',
            request_text TEXT NOT NULL DEFAULT '',
            result_summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES eval_cases(case_id)
        )
        """
    )
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
        """
        CREATE INDEX IF NOT EXISTS idx_promptfoo_analysis_exclusions_case
        ON promptfoo_analysis_exclusions(case_id)
        """
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
        payload[field.removesuffix("_json")] = json.loads(str(payload.pop(field) or "{}"))
    payload["success"] = bool(payload.get("success"))
    return payload


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
