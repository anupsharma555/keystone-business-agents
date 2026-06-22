"""Human Slack-thread scoring for Promptfoo eval runs."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

UTC = UTC

DEFAULT_REVIEW_DB = Path(".keystone/promptfoo/human-reviews.sqlite")
REVIEW_KIND_HUMAN = "human"
REVIEW_KIND_ORCHESTRATOR_JUDGE = "orchestrator_judge"
REVIEW_KINDS = (REVIEW_KIND_HUMAN, REVIEW_KIND_ORCHESTRATOR_JUDGE)
SCORE_COLUMN_PREFIX = "score_"

SCORE_DIMENSIONS = (
    "accuracy",
    "relevance",
    "explainability",
    "readability",
    "source_quality",
    "search_quality",
    "synthesis_quality",
    "uniqueness",
    "format_quality",
    "instruction_following",
    "usefulness",
)

RUBRIC_PROMPTS = (
    "Score each dimension 0-5; safety is pass/fail.",
    "# accuracy - Are factual claims, deadlines, grants/RFPs, and citations correct?",
    "# relevance - Does the answer match the ask and Keystone's business needs?",
    "# explainability - Does it explain why findings matter without hand-waving?",
    "# readability - Is it easy to read, not metadata-heavy or smart-sounding filler?",
    "# source_quality - Are sources credible, current, and claim-supporting?",
    "# search_quality - Did retrieval find the right kinds of sources/results?",
    "# synthesis - Did it turn sources into useful judgment, not a source dump?",
    "# uniqueness - Does it add non-generic, case-specific value beyond boilerplate?",
    "# format - Is the Slack/table formatting readable?",
    "# instruction_following - Did it follow constraints and avoid side effects?",
    "# usefulness - Would you use this output for a Keystone decision?",
)

_DIMENSION_ALIASES = {
    "accuracy": "accuracy",
    "accurate": "accuracy",
    "grant_accuracy": "accuracy",
    "rfp_accuracy": "accuracy",
    "relevance": "relevance",
    "relevant": "relevance",
    "grant_relevance": "relevance",
    "rfp_relevance": "relevance",
    "explainable": "explainability",
    "explainability": "explainability",
    "why_it_matters": "explainability",
    "readable": "readability",
    "readability": "readability",
    "plain_language": "readability",
    "sources": "source_quality",
    "source": "source_quality",
    "source_quality": "source_quality",
    "search": "search_quality",
    "search_quality": "search_quality",
    "retrieval": "search_quality",
    "synthesis": "synthesis_quality",
    "synthesis_quality": "synthesis_quality",
    "summary": "synthesis_quality",
    "output": "uniqueness",
    "output_quality": "uniqueness",
    "unique": "uniqueness",
    "uniqueness": "uniqueness",
    "format": "format_quality",
    "format_quality": "format_quality",
    "formatting": "format_quality",
    "instructions": "instruction_following",
    "instruction": "instruction_following",
    "instruction_following": "instruction_following",
    "useful": "usefulness",
    "usefulness": "usefulness",
    "keystone_relevance": "usefulness",
    "actionability": "usefulness",
}

_META_ALIASES = {
    "case": "case_id",
    "case_id": "case_id",
    "promptfoo_case": "case_id",
    "run": "run_id",
    "run_id": "run_id",
    "eval_run": "run_id",
    "agent": "agent",
    "reviewer": "reviewer",
    "safety": "safety",
    "notes": "notes",
}


@dataclass(frozen=True)
class HumanEvalReview:
    """One human review captured from a Slack eval thread."""

    case_id: str
    run_id: str = ""
    agent: str = ""
    reviewer: str = "anup"
    review_kind: str = REVIEW_KIND_HUMAN
    scores: dict[str, float] = field(default_factory=dict)
    safety: str = ""
    notes: str = ""
    slack_channel_id: str = "C0BA17Y9C01"
    slack_channel_name: str = "evals"
    slack_thread_ts: str = ""
    raw_text: str = ""
    storage_mode: str = "local_review"
    raw_text_hash: str = ""
    notes_hash: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        )
    )

    @property
    def average_score(self) -> float | None:
        if not self.scores:
            return None
        return round(sum(self.scores.values()) / len(self.scores), 3)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["average_score"] = self.average_score
        return payload


def parse_human_review(
    text: str,
    *,
    case_id: str = "",
    run_id: str = "",
    agent: str = "",
    reviewer: str = "anup",
    slack_channel_id: str = "C0BA17Y9C01",
    slack_channel_name: str = "evals",
    slack_thread_ts: str = "",
    storage_mode: str = "local_review",
    review_kind: str = REVIEW_KIND_HUMAN,
) -> HumanEvalReview:
    """Parse a Slack-thread score reply into a normalized review record."""

    raw_text = str(text or "").strip()
    if not raw_text:
        raise ValueError("review text is required")

    fields = _extract_fields(raw_text)
    scores: dict[str, float] = {}
    parsed_notes = ""
    safety = ""
    parsed_case_id = case_id.strip()
    parsed_run_id = run_id.strip()
    parsed_agent = agent.strip()
    parsed_reviewer = reviewer.strip() or "anup"

    for raw_key, raw_value in fields.items():
        key = _normalize_key(raw_key)
        value = raw_value.strip().strip("\"'")
        if key in _DIMENSION_ALIASES:
            dimension = _DIMENSION_ALIASES[key]
            scores[dimension] = _parse_score(value, raw_key)
        elif key in _META_ALIASES:
            meta_key = _META_ALIASES[key]
            if meta_key == "case_id":
                parsed_case_id = value
            elif meta_key == "run_id":
                parsed_run_id = value
            elif meta_key == "agent":
                parsed_agent = value
            elif meta_key == "reviewer":
                parsed_reviewer = value or parsed_reviewer
            elif meta_key == "safety":
                safety = _parse_safety(value)
            elif meta_key == "notes":
                parsed_notes = value

    if not parsed_case_id:
        raise ValueError("case_id is required in args or review text")
    if not scores:
        raise ValueError("at least one 0-5 score is required")
    missing_scores = [dimension for dimension in SCORE_DIMENSIONS if dimension not in scores]
    if missing_scores:
        raise ValueError(
            "complete scorecard is required; missing scores: " + ", ".join(missing_scores)
        )
    if safety not in {"pass", "fail"}:
        raise ValueError("safety is required and must be pass or fail")

    return HumanEvalReview(
        case_id=parsed_case_id,
        run_id=parsed_run_id,
        agent=parsed_agent,
        reviewer=parsed_reviewer,
        review_kind=_normalize_review_kind(review_kind),
        scores=dict(sorted(scores.items())),
        safety=safety,
        notes=parsed_notes,
        slack_channel_id=slack_channel_id.strip(),
        slack_channel_name=slack_channel_name.strip(),
        slack_thread_ts=slack_thread_ts.strip(),
        raw_text=raw_text,
        storage_mode=str(storage_mode or "local_review").strip() or "local_review",
    )


def save_human_review(
    review: HumanEvalReview,
    *,
    database_path: str | Path = DEFAULT_REVIEW_DB,
) -> int:
    """Save a human eval review in the repo-local Promptfoo review database."""

    _validate_review_for_storage(review)
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        _ensure_schema(connection)
        storage_mode = str(review.storage_mode or "local_review").strip() or "local_review"
        notes_hash = review.notes_hash or _text_hash(review.notes)
        raw_text_hash = review.raw_text_hash or _text_hash(review.raw_text)
        stored_notes = review.notes
        stored_raw_text = review.raw_text
        if storage_mode == "api_redacted":
            stored_notes = _redact_review_text(review.notes, max_chars=500)
            stored_raw_text = _redact_review_text(review.raw_text, max_chars=1200)
        score_columns = [f"{SCORE_COLUMN_PREFIX}{dimension}" for dimension in SCORE_DIMENSIONS]
        column_names = [
            "case_id",
            "run_id",
            "agent",
            "reviewer",
            "review_kind",
            "scores_json",
            "average_score",
            "total_score",
            *score_columns,
            "safety",
            "notes",
            "slack_channel_id",
            "slack_channel_name",
            "slack_thread_ts",
            "raw_text",
            "storage_mode",
            "raw_text_hash",
            "notes_hash",
            "created_at",
        ]
        placeholders = ", ".join("?" for _ in column_names)
        cursor = connection.execute(
            f"""
            INSERT INTO human_eval_reviews (
                {", ".join(column_names)}
            ) VALUES ({placeholders})
            """,
            (
                review.case_id,
                review.run_id,
                review.agent,
                review.reviewer,
                _normalize_review_kind(review.review_kind),
                json.dumps(review.scores, ensure_ascii=True, sort_keys=True),
                review.average_score,
                review.average_score,
                *[review.scores.get(dimension) for dimension in SCORE_DIMENSIONS],
                review.safety,
                stored_notes,
                review.slack_channel_id,
                review.slack_channel_name,
                review.slack_thread_ts,
                stored_raw_text,
                storage_mode,
                raw_text_hash,
                notes_hash,
                review.created_at,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def list_human_reviews(
    *,
    database_path: str | Path = DEFAULT_REVIEW_DB,
    case_id: str | None = None,
) -> list[dict[str, Any]]:
    """List stored human eval reviews."""

    path = Path(database_path)
    if not path.exists():
        return []
    where = ""
    params: tuple[str, ...] = ()
    if case_id:
        where = "WHERE case_id = ?"
        params = (case_id,)
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_schema(connection)
        rows = connection.execute(
            f"SELECT * FROM human_eval_reviews {where} ORDER BY id",
            params,
        ).fetchall()
    return [_row_to_dict(dict(row)) for row in rows]


def build_slack_review_template(
    *,
    case_id: str,
    run_id: str = "",
    agent: str = "",
) -> str:
    """Return the compact score block to paste into a Slack eval thread."""

    lines = ["eval score", *RUBRIC_PROMPTS, f"case: {case_id}"]
    if run_id:
        lines.append(f"run: {run_id}")
    if agent:
        lines.append(f"agent: {agent}")
    lines.extend(
        [
            "accuracy: ",
            "relevance: ",
            "explainability: ",
            "readability: ",
            "source_quality: ",
            "search_quality: ",
            "synthesis: ",
            "uniqueness: ",
            "format: ",
            "instruction_following: ",
            "usefulness: ",
            "safety: ",
            "notes: ",
        ]
    )
    return "\n".join(lines)


def ensure_human_review_schema(connection: sqlite3.Connection) -> None:
    """Ensure the human review table exists in an eval database connection."""

    _ensure_schema(connection)


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS human_eval_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT NOT NULL,
            run_id TEXT NOT NULL DEFAULT '',
            agent TEXT NOT NULL DEFAULT '',
            reviewer TEXT NOT NULL DEFAULT '',
            review_kind TEXT NOT NULL DEFAULT 'human',
            scores_json TEXT NOT NULL DEFAULT '{}',
            average_score REAL,
            total_score REAL,
            score_accuracy REAL,
            score_relevance REAL,
            score_explainability REAL,
            score_readability REAL,
            score_source_quality REAL,
            score_search_quality REAL,
            score_synthesis_quality REAL,
            score_uniqueness REAL,
            score_format_quality REAL,
            score_instruction_following REAL,
            score_usefulness REAL,
            safety TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            slack_channel_id TEXT NOT NULL DEFAULT '',
            slack_channel_name TEXT NOT NULL DEFAULT '',
            slack_thread_ts TEXT NOT NULL DEFAULT '',
            raw_text TEXT NOT NULL DEFAULT '',
            storage_mode TEXT NOT NULL DEFAULT 'local_review',
            raw_text_hash TEXT NOT NULL DEFAULT '',
            notes_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    _add_column_if_missing(connection, "human_eval_reviews", "run_id", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "human_eval_reviews", "agent", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "human_eval_reviews", "reviewer", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "human_eval_reviews", "review_kind", "TEXT NOT NULL DEFAULT 'human'")
    _add_column_if_missing(connection, "human_eval_reviews", "scores_json", "TEXT NOT NULL DEFAULT '{}'")
    _add_column_if_missing(connection, "human_eval_reviews", "average_score", "REAL")
    _add_column_if_missing(connection, "human_eval_reviews", "total_score", "REAL")
    for dimension in SCORE_DIMENSIONS:
        _add_column_if_missing(
            connection,
            "human_eval_reviews",
            f"{SCORE_COLUMN_PREFIX}{dimension}",
            "REAL",
        )
    _add_column_if_missing(connection, "human_eval_reviews", "safety", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "human_eval_reviews", "notes", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "human_eval_reviews", "slack_channel_id", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "human_eval_reviews", "slack_channel_name", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "human_eval_reviews", "slack_thread_ts", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "human_eval_reviews", "raw_text", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "human_eval_reviews", "storage_mode", "TEXT NOT NULL DEFAULT 'local_review'")
    _add_column_if_missing(connection, "human_eval_reviews", "raw_text_hash", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "human_eval_reviews", "notes_hash", "TEXT NOT NULL DEFAULT ''")
    _backfill_score_columns(connection)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_human_eval_reviews_case ON human_eval_reviews(case_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_human_eval_reviews_run ON human_eval_reviews(run_id)"
    )


def _extract_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower() in {"eval score", "score", "scores"}:
            continue
        if ":" in stripped:
            key, value = stripped.split(":", 1)
            fields[key.strip()] = value.strip()

    compact_pairs = re.findall(r"([A-Za-z][A-Za-z0-9_ -]*)\s*=\s*(\"[^\"]*\"|'[^']*'|\S+)", text)
    for key, value in compact_pairs:
        fields.setdefault(key.strip(), value.strip())
    known_keys = sorted(
        {*_DIMENSION_ALIASES.keys(), *_META_ALIASES.keys()},
        key=len,
        reverse=True,
    )
    key_pattern = "|".join(re.escape(key).replace("\\ ", r"\s+") for key in known_keys)
    natural_pairs = re.findall(
        rf"\b({key_pattern})\b\s*(?:-|=|:)?\s*([0-5](?:\.\d+)?|pass|fail|passed|failed)\b",
        text,
        flags=re.IGNORECASE,
    )
    for key, value in natural_pairs:
        fields.setdefault(key.strip(), value.strip())
    notes_match = re.search(r"\bnotes?\s*:\s*(.+)$", text, flags=re.IGNORECASE | re.DOTALL)
    if notes_match:
        fields["notes"] = notes_match.group(1).strip()
    return fields


def _validate_review_for_storage(review: HumanEvalReview) -> None:
    if not str(review.case_id or "").strip():
        raise ValueError("case_id is required")
    if not review.scores:
        raise ValueError("at least one 0-5 score is required")
    invalid_scores = [
        key
        for key, value in review.scores.items()
        if not isinstance(value, int | float) or float(value) < 0 or float(value) > 5
    ]
    if invalid_scores:
        raise ValueError("scores must be numeric values between 0 and 5")
    if str(review.safety or "").strip() not in {"pass", "fail"}:
        raise ValueError("safety is required and must be pass or fail")
    _normalize_review_kind(review.review_kind)


def _normalize_review_kind(value: str) -> str:
    normalized = str(value or REVIEW_KIND_HUMAN).strip().lower()
    if normalized not in REVIEW_KINDS:
        raise ValueError(
            "review_kind must be one of: " + ", ".join(REVIEW_KINDS)
        )
    return normalized


def _normalize_key(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _parse_score(value: str, key: str) -> float:
    try:
        score = float(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be a numeric 0-5 score") from exc
    if score < 0 or score > 5:
        raise ValueError(f"{key} must be between 0 and 5")
    return score


def _parse_safety(value: str) -> str:
    normalized = _normalize_key(value)
    if normalized in {"pass", "passed", "ok", "safe", "yes", "true"}:
        return "pass"
    if normalized in {"fail", "failed", "unsafe", "no", "false"}:
        return "fail"
    raise ValueError("safety must be pass or fail")


def _row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    try:
        scores = _normalize_scores(json.loads(str(payload.pop("scores_json") or "{}")))
    except (TypeError, json.JSONDecodeError, ValueError):
        scores = {}
    for dimension in SCORE_DIMENSIONS:
        column_value = payload.get(f"{SCORE_COLUMN_PREFIX}{dimension}")
        if dimension not in scores and column_value is not None:
            scores[dimension] = float(column_value)
    payload["scores"] = scores
    return payload


def _backfill_score_columns(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(human_eval_reviews)").fetchall()
    }
    required = {"id", "scores_json", "average_score", "total_score"}
    required.update(f"{SCORE_COLUMN_PREFIX}{dimension}" for dimension in SCORE_DIMENSIONS)
    if not required.issubset(columns):
        return
    rows = connection.execute(
        """
        SELECT id, scores_json, average_score, total_score
        FROM human_eval_reviews
        WHERE total_score IS NULL
        """
    ).fetchall()
    for row in rows:
        try:
            scores = _normalize_scores(json.loads(str(row[1] or "{}")))
        except (TypeError, json.JSONDecodeError, ValueError):
            scores = {}
        average = row[2]
        if average is None and scores:
            average = round(sum(float(value) for value in scores.values()) / len(scores), 3)
        assignments = ["total_score = ?"] + [
            f"{SCORE_COLUMN_PREFIX}{dimension} = ?"
            for dimension in SCORE_DIMENSIONS
        ]
        values = [average, *[scores.get(dimension) for dimension in SCORE_DIMENSIONS], row[0]]
        connection.execute(
            f"UPDATE human_eval_reviews SET {', '.join(assignments)} WHERE id = ?",
            values,
        )


def _add_column_if_missing(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    columns = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in columns:
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def _text_hash(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _redact_review_text(value: str, *, max_chars: int) -> str:
    text = str(value or "")
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", text)
    text = re.sub(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "[REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[REDACTED_EMAIL]", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b", "[REDACTED_PHONE]", text)
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[:max_chars] + "...[truncated]"
    return text


def _normalize_scores(scores: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(scores or {})
    if "uniqueness" not in normalized and "output_quality" in normalized:
        normalized["uniqueness"] = normalized["output_quality"]
    normalized.pop("output_quality", None)
    return normalized
