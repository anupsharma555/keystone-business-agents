"""Human Slack-thread scoring for Promptfoo eval runs."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc

DEFAULT_REVIEW_DB = Path(".keystone/promptfoo/human-reviews.sqlite")

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
    scores: dict[str, float] = field(default_factory=dict)
    safety: str = "pass"
    notes: str = ""
    slack_channel_id: str = "C0BA17Y9C01"
    slack_channel_name: str = "evals"
    slack_thread_ts: str = ""
    raw_text: str = ""
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
) -> HumanEvalReview:
    """Parse a Slack-thread score reply into a normalized review record."""

    raw_text = str(text or "").strip()
    if not raw_text:
        raise ValueError("review text is required")

    fields = _extract_fields(raw_text)
    scores: dict[str, float] = {}
    parsed_notes = ""
    safety = "pass"
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

    return HumanEvalReview(
        case_id=parsed_case_id,
        run_id=parsed_run_id,
        agent=parsed_agent,
        reviewer=parsed_reviewer,
        scores=dict(sorted(scores.items())),
        safety=safety,
        notes=parsed_notes,
        slack_channel_id=slack_channel_id.strip(),
        slack_channel_name=slack_channel_name.strip(),
        slack_thread_ts=slack_thread_ts.strip(),
        raw_text=raw_text,
    )


def save_human_review(
    review: HumanEvalReview,
    *,
    database_path: str | Path = DEFAULT_REVIEW_DB,
) -> int:
    """Save a human eval review in the repo-local Promptfoo review database."""

    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        _ensure_schema(connection)
        cursor = connection.execute(
            """
            INSERT INTO human_eval_reviews (
                case_id, run_id, agent, reviewer, scores_json, average_score,
                safety, notes, slack_channel_id, slack_channel_name,
                slack_thread_ts, raw_text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review.case_id,
                review.run_id,
                review.agent,
                review.reviewer,
                json.dumps(review.scores, ensure_ascii=True, sort_keys=True),
                review.average_score,
                review.safety,
                review.notes,
                review.slack_channel_id,
                review.slack_channel_name,
                review.slack_thread_ts,
                review.raw_text,
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
    with sqlite3.connect(path) as connection:
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
            "safety: pass",
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
            scores_json TEXT NOT NULL DEFAULT '{}',
            average_score REAL,
            safety TEXT NOT NULL DEFAULT 'pass',
            notes TEXT NOT NULL DEFAULT '',
            slack_channel_id TEXT NOT NULL DEFAULT '',
            slack_channel_name TEXT NOT NULL DEFAULT '',
            slack_thread_ts TEXT NOT NULL DEFAULT '',
            raw_text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
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
    payload["scores"] = _normalize_scores(json.loads(str(payload.pop("scores_json") or "{}")))
    return payload


def _normalize_scores(scores: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(scores or {})
    if "uniqueness" not in normalized and "output_quality" in normalized:
        normalized["uniqueness"] = normalized["output_quality"]
    normalized.pop("output_quality", None)
    return normalized
