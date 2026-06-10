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
    "chief_of_staff.jsonl",
    "gmail_triage.jsonl",
    "orchestrator_routing.jsonl",
    "safety_refusals.jsonl",
    "source_attribution.jsonl",
    "opportunity_scoring.jsonl",
    "outreach_copy_constraints.jsonl",
    "skill_gate_failures.jsonl",
}

REQUIRED_TASKS = {
    "chief_of_staff",
    "gmail_triage",
    "orchestrator_routing",
    "safety_refusal",
    "source_attribution",
    "opportunity_scoring",
    "outreach_copy_constraints",
    "skill_gate_failures",
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
    assert "skill_task_matrix.jsonl" in dataset_names


def test_local_evals_pass_offline(monkeypatch) -> None:
    def fail_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("local evals must not call the network")

    monkeypatch.setattr("keystone_agents.tools.serper_tool.requests.post", fail_network)

    summary = run_local_evals()

    assert summary.total >= len(REQUIRED_DATASETS)
    assert summary.failed == 0
    assert summary.to_dict()["prompt_metadata"]
    assert all(result.prompt_metadata for result in summary.results)
    assert any(result.dataset == "skill_task_matrix" for result in summary.results)
    skill_behavior_results = [result for result in summary.results if result.validates_skills]
    assert skill_behavior_results
    assert all(result.passed for result in skill_behavior_results)
    assert {"slack", "computer"} <= {result.surface for result in skill_behavior_results}
    covered_specialist_skills = {
        skill
        for result in skill_behavior_results
        for skill in result.validates_skills
        if skill.endswith("_specialist_contracts")
    }
    assert {
        "gmail_triage_specialist_contracts",
        "business_research_specialist_contracts",
        "chief_of_staff_specialist_contracts",
        "opportunity_scout_specialist_contracts",
        "orchestrator_specialist_contracts",
        "outreach_composer_specialist_contracts",
    } <= covered_specialist_skills


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


def test_local_eval_runner_exposes_behavior_skill_metadata(capsys) -> None:
    exit_code = main(["--dataset", "outreach_copy_constraints", "--json"])
    output = json.loads(capsys.readouterr().out)
    behavior_results = [result for result in output["results"] if result.get("validates_skills")]

    assert exit_code == 0
    assert behavior_results
    assert any(result["surface"] == "computer" for result in behavior_results)
    assert all(
        "outreach_composer_specialist_contracts" in result["validates_skills"]
        for result in behavior_results
    )


def test_local_eval_runner_exposes_research_and_scout_skill_metadata(capsys) -> None:
    exit_code = main(
        ["--dataset", "source_attribution", "--dataset", "opportunity_scoring", "--json"]
    )
    output = json.loads(capsys.readouterr().out)
    behavior_results = [result for result in output["results"] if result.get("validates_skills")]
    specialist_skills = {
        skill
        for result in behavior_results
        for skill in result["validates_skills"]
        if skill.endswith("_specialist_contracts")
    }

    assert exit_code == 0
    assert output["failed"] == 0
    assert behavior_results
    assert {"slack", "computer"} <= {result["surface"] for result in behavior_results}
    assert {
        "business_research_specialist_contracts",
        "opportunity_scout_specialist_contracts",
    } <= specialist_skills


def test_local_eval_runner_exposes_control_plane_skill_metadata(capsys) -> None:
    exit_code = main(["--dataset", "orchestrator_routing", "--dataset", "chief_of_staff", "--json"])
    output = json.loads(capsys.readouterr().out)
    behavior_results = [result for result in output["results"] if result.get("validates_skills")]
    specialist_skills = {
        skill
        for result in behavior_results
        for skill in result["validates_skills"]
        if skill.endswith("_specialist_contracts")
    }

    assert exit_code == 0
    assert output["failed"] == 0
    assert behavior_results
    assert {"slack", "computer"} <= {result["surface"] for result in behavior_results}
    assert {
        "orchestrator_specialist_contracts",
        "chief_of_staff_specialist_contracts",
    } <= specialist_skills


def test_local_eval_runner_proves_skill_gate_failures_degrade_safely(capsys) -> None:
    exit_code = main(["--dataset", "skill_gate_failures", "--json"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["failed"] == 0
    assert {result["dataset"] for result in output["results"]} == {"skill_gate_failures"}
    statuses = {result["observed"]["status"] for result in output["results"]}
    assert {"blocked", "limited"} <= statuses
    assert all(result["validates_skills"] for result in output["results"])


def test_local_eval_runner_skill_task_matrix_subset_json_output(capsys) -> None:
    exit_code = main(["--dataset", "skill_task_matrix", "--json"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["failed"] == 0
    assert {result["dataset"] for result in output["results"]} == {"skill_task_matrix"}
    assert {result["observed"]["surface"] for result in output["results"]} >= {
        "slack",
        "computer",
    }
    assert all(result["observed"]["selection_reasons"] for result in output["results"])


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
