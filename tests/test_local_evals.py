from __future__ import annotations

import json
from pathlib import Path

from scripts.run_local_evals import (
    DEFAULT_EVAL_DIR,
    LocalEvalCase,
    main,
    run_cases,
    run_local_evals,
)

REQUIRED_DATASETS = {
    "gmail_triage.jsonl",
    "orchestrator_routing.jsonl",
    "safety_refusals.jsonl",
    "source_attribution.jsonl",
    "opportunity_scoring.jsonl",
    "outreach_copy_constraints.jsonl",
}

REQUIRED_TASKS = {
    "gmail_triage",
    "orchestrator_routing",
    "safety_refusal",
    "source_attribution",
    "opportunity_scoring",
    "outreach_copy_constraints",
}


def _jsonl_rows(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_seed_eval_datasets_exist_and_cover_required_tasks() -> None:
    dataset_names = {path.name for path in DEFAULT_EVAL_DIR.glob("*.jsonl")}
    rows = [row for dataset in REQUIRED_DATASETS for row in _jsonl_rows(DEFAULT_EVAL_DIR / dataset)]
    tasks = {row["task"] for row in rows}

    assert REQUIRED_DATASETS <= dataset_names
    assert REQUIRED_TASKS <= tasks
    assert all(isinstance(row.get("id"), str) and row["id"] for row in rows)
    assert all(isinstance(row.get("input"), dict) for row in rows)
    assert all(isinstance(row.get("expected"), dict) for row in rows)
    assert all(isinstance(row.get("validates_prompts"), list) for row in rows)
    assert all(row["validates_prompts"] for row in rows)


def test_local_evals_pass_offline(monkeypatch) -> None:
    def fail_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("local evals must not call the network")

    monkeypatch.setattr("keystone_agents.tools.serper_tool.requests.post", fail_network)

    summary = run_local_evals()

    assert summary.total >= len(REQUIRED_DATASETS)
    assert summary.failed == 0
    assert summary.to_dict()["prompt_metadata"]
    assert all(result.prompt_metadata for result in summary.results)


def test_local_eval_runner_subset_json_output(capsys) -> None:
    exit_code = main(["--dataset", "gmail_triage", "--json"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["failed"] == 0
    assert output["prompt_metadata"]
    assert {result["dataset"] for result in output["results"]} == {"gmail_triage"}
    assert all(result["prompt_versions"] for result in output["results"])
    assert all(
        result["prompt_versions"] == result["validates_prompts"] for result in output["results"]
    )


def test_unknown_eval_task_fails_closed() -> None:
    result = run_cases(
        [
            LocalEvalCase(
                dataset="fixture",
                line_number=1,
                case_id="unknown",
                task="not_a_task",
                input_payload={},
                expected={},
            )
        ]
    )[0]

    assert result.passed is False
    assert "unknown task" in result.failures[0]
    assert result.prompt_metadata == []
