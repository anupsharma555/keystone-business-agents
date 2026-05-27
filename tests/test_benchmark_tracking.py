from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from keystone_agents.benchmark_tracking import (
    record_eval_summary,
    summarize_benchmarks,
)
from keystone_agents.evals import run_static_evals
from scripts.run_evals import main as run_evals_main
from scripts.run_local_evals import main as run_local_evals_main
from scripts.summarize_benchmark_results import main as summarize_benchmark_main


def test_record_static_eval_summary_tracks_scores_without_observed_payload(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "benchmark.sqlite"
    summary = run_static_evals(agent="gmail")

    record = record_eval_summary(
        summary.to_dict(),
        suite="static",
        db_path=db_path,
        run_label="unit-test",
        entrypoint="pytest",
    )

    assert record.total == summary.total
    assert record.failed == 0
    with sqlite3.connect(db_path) as conn:
        run_count = conn.execute("SELECT COUNT(*) FROM benchmark_runs").fetchone()[0]
        case_rows = conn.execute(
            "SELECT case_id, score, passed, observed_keys_json FROM benchmark_case_scores"
        ).fetchall()

    assert run_count == 1
    assert len(case_rows) == summary.total
    assert all(row[1] == 1.0 for row in case_rows)
    assert all(row[2] == 1 for row in case_rows)
    assert all(isinstance(json.loads(row[3]), list) for row in case_rows)


def test_benchmark_summary_reports_recent_runs(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "benchmark.sqlite"
    summary = {
        "total": 1,
        "passed": 1,
        "failed": 0,
        "average_score": 0.75,
        "results": [
            {
                "id": "route_example",
                "dataset": "orchestrator_routing",
                "task": "orchestrator_routing",
                "passed": True,
                "score": 0.75,
                "prompt_versions": ["orchestrator@2026-04-26.1"],
                "observed": {"route": "business_research_analyst"},
            }
        ],
    }
    record_eval_summary(summary, suite="local", db_path=db_path, run_label="baseline")

    result = summarize_benchmarks(db_path)
    assert result["runs"][0]["suite"] == "local"
    assert result["case_trends"][0]["case_id"] == "route_example"
    assert result["case_trends"][0]["average_score"] == 0.75

    exit_code = summarize_benchmark_main(["--benchmark-db", str(db_path), "--json"])
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["runs"][0]["run_label"] == "baseline"


def test_eval_cli_record_benchmark_keeps_json_stdout(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "benchmark.sqlite"

    exit_code = run_evals_main(
        [
            "--agent",
            "gmail",
            "--json",
            "--record-benchmark",
            "--benchmark-db",
            str(db_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(captured.out)["failed"] == 0
    assert "Recorded benchmark run" in captured.err


def test_local_eval_cli_record_benchmark_keeps_json_stdout(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "benchmark.sqlite"

    exit_code = run_local_evals_main(
        [
            "--dataset",
            "gmail_triage",
            "--json",
            "--record-benchmark",
            "--benchmark-db",
            str(db_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(captured.out)["failed"] == 0
    assert "Recorded benchmark run" in captured.err
