"""Local benchmark result tracking for Keystone eval runs.

The tracker stores eval scores over time without persisting raw request bodies,
email bodies, drafts, or observed agent output.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class BenchmarkRunRecord:
    run_id: str
    db_path: Path
    suite: str
    total: int
    passed: int
    failed: int


def default_benchmark_db_path() -> Path:
    override = os.environ.get("KEYSTONE_BENCHMARK_DB")
    if override:
        return Path(override).expanduser()
    keystone_home = Path(os.environ.get("KEYSTONE_HOME", PROJECT_ROOT / ".keystone")).expanduser()
    return keystone_home / "state" / "benchmark_evals.sqlite"


def init_benchmark_db(db_path: str | Path | None = None) -> Path:
    path = Path(db_path) if db_path is not None else default_benchmark_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS benchmark_runs (
                run_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                suite TEXT NOT NULL,
                run_label TEXT NOT NULL DEFAULT '',
                entrypoint TEXT NOT NULL DEFAULT '',
                total INTEGER NOT NULL,
                passed INTEGER NOT NULL,
                failed INTEGER NOT NULL,
                average_score REAL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS benchmark_case_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                suite TEXT NOT NULL,
                dataset TEXT NOT NULL,
                case_id TEXT NOT NULL,
                agent_or_task TEXT NOT NULL,
                passed INTEGER NOT NULL,
                score REAL NOT NULL,
                prompt_versions_json TEXT NOT NULL DEFAULT '[]',
                checks_json TEXT NOT NULL DEFAULT '[]',
                failures_json TEXT NOT NULL DEFAULT '[]',
                observed_keys_json TEXT NOT NULL DEFAULT '[]',
                FOREIGN KEY(run_id) REFERENCES benchmark_runs(run_id)
            );

            CREATE INDEX IF NOT EXISTS idx_benchmark_case_scores_case
            ON benchmark_case_scores(case_id, dataset, suite, created_at);

            CREATE INDEX IF NOT EXISTS idx_benchmark_case_scores_suite
            ON benchmark_case_scores(suite, dataset, agent_or_task, created_at);
            """
        )
    return path


def record_eval_summary(
    summary: Mapping[str, Any],
    *,
    suite: str,
    db_path: str | Path | None = None,
    run_label: str = "",
    entrypoint: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> BenchmarkRunRecord:
    path = init_benchmark_db(db_path)
    run_id = f"bench_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    created_at = datetime.now(UTC).isoformat()
    results = [item for item in summary.get("results", []) if isinstance(item, Mapping)]
    total = int(summary.get("total") or len(results))
    passed = int(summary.get("passed") or sum(1 for item in results if item.get("passed")))
    failed = int(summary.get("failed") or max(total - passed, 0))
    average_score = _optional_float(summary.get("average_score"))
    if average_score is None and results:
        average_score = round(
            sum(_case_score(item) for item in results) / len(results),
            3,
        )

    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO benchmark_runs (
                run_id, created_at, suite, run_label, entrypoint, total, passed,
                failed, average_score, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                created_at,
                suite,
                run_label,
                entrypoint,
                total,
                passed,
                failed,
                average_score,
                _json(metadata or {}),
            ),
        )
        conn.executemany(
            """
            INSERT INTO benchmark_case_scores (
                run_id, created_at, suite, dataset, case_id, agent_or_task,
                passed, score, prompt_versions_json, checks_json, failures_json,
                observed_keys_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    created_at,
                    suite,
                    str(item.get("dataset") or ""),
                    str(item.get("id") or item.get("case_id") or ""),
                    str(item.get("agent") or item.get("task") or ""),
                    1 if bool(item.get("passed")) else 0,
                    _case_score(item),
                    _json(item.get("prompt_versions") or []),
                    _json(_compact_checks(item.get("checks"))),
                    _json(item.get("failures") or []),
                    _json(_observed_keys(item.get("observed"))),
                )
                for item in results
            ],
        )
    return BenchmarkRunRecord(
        run_id=run_id,
        db_path=path,
        suite=suite,
        total=total,
        passed=passed,
        failed=failed,
    )


def summarize_benchmarks(
    db_path: str | Path | None = None,
    *,
    limit_runs: int = 10,
) -> dict[str, Any]:
    path = Path(db_path) if db_path is not None else default_benchmark_db_path()
    if not path.exists():
        return {"db_path": str(path), "runs": [], "case_trends": []}

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        runs = [
            dict(row)
            for row in conn.execute(
                """
                SELECT run_id, created_at, suite, run_label, entrypoint, total,
                       passed, failed, average_score
                FROM benchmark_runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit_runs,),
            )
        ]
        case_trends = [
            dict(row)
            for row in conn.execute(
                """
                SELECT suite, dataset, agent_or_task, case_id,
                       COUNT(*) AS runs,
                       ROUND(AVG(score), 3) AS average_score,
                       ROUND(AVG(passed), 3) AS pass_rate,
                       MAX(created_at) AS last_seen
                FROM benchmark_case_scores
                GROUP BY suite, dataset, agent_or_task, case_id
                ORDER BY last_seen DESC, suite, dataset, case_id
                LIMIT 200
                """
            )
        ]
    return {"db_path": str(path), "runs": runs, "case_trends": case_trends}


def format_benchmark_summary(summary: Mapping[str, Any]) -> str:
    lines = [f"Benchmark DB: {summary.get('db_path')}"]
    runs = list(summary.get("runs") or [])
    if not runs:
        return "\n".join([*lines, "No benchmark runs recorded."])

    lines.append("Recent runs:")
    for run in runs:
        label = f" label={run['run_label']}" if run.get("run_label") else ""
        score = run.get("average_score")
        score_text = f" avg={score}" if score is not None else ""
        lines.append(
            f"- {run['created_at']} {run['suite']}{label}: "
            f"{run['passed']}/{run['total']} passed{score_text}"
        )

    trends = list(summary.get("case_trends") or [])
    if trends:
        lines.append("Case trends:")
        for row in trends[:25]:
            lines.append(
                f"- {row['suite']}/{row['dataset']}/{row['case_id']}: "
                f"runs={row['runs']} pass_rate={row['pass_rate']} avg={row['average_score']}"
            )
    return "\n".join(lines)


def _case_score(item: Mapping[str, Any]) -> float:
    score = _optional_float(item.get("score"))
    if score is not None:
        return score
    return 1.0 if bool(item.get("passed")) else 0.0


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compact_checks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    checks: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        checks.append(
            {
                "name": str(item.get("name") or ""),
                "passed": bool(item.get("passed")),
                "score": _optional_float(item.get("score")),
                "message": str(item.get("message") or "")[:240],
            }
        )
    return checks


def _observed_keys(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    return sorted(str(key) for key in value.keys())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)
