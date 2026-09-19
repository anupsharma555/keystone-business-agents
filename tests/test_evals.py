from __future__ import annotations

import json
from pathlib import Path

from keystone_agents.evals import (
    DEFAULT_EVAL_DIR,
    EvalSuiteResult,
    generate_eval_report,
    run_static_evals,
    score_output_against_expected,
)
from scripts.run_evals import main as run_evals_main

EVAL_FILES = {
    "gmail_triage_cases.json",
    "business_research_analyst_cases.json",
    "opportunity_scout_cases.json",
    "outreach_composer_cases.json",
}


def test_eval_files_load() -> None:
    paths = {path.name for path in DEFAULT_EVAL_DIR.glob("*.json")}

    assert EVAL_FILES <= paths
    for filename in EVAL_FILES:
        rows = json.loads((DEFAULT_EVAL_DIR / filename).read_text(encoding="utf-8"))
        assert rows
        assert all(row["id"] for row in rows)
        assert all(row["agent"] for row in rows)
        assert all(isinstance(row["input"], dict) for row in rows)
        assert all(isinstance(row["expected"], dict) for row in rows)


def test_static_eval_scoring_runs() -> None:
    summary = run_static_evals()
    payload = summary.to_dict()

    assert isinstance(summary, EvalSuiteResult)
    assert summary.total >= 4
    assert summary.failed == 0
    assert summary.average_score == 1.0
    assert payload["datasets"]
    assert "skill_labels" in payload
    assert "action_boundary_enforcement" in payload["skill_labels"]
    assert "evidence_attribution_and_claim_mapping" in payload["skill_labels"]
    assert payload["prompt_metadata"]
    assert all(result["prompt_versions"] for result in payload["results"])
    assert all(result["skill_labels"] for result in payload["results"])
    assert all(check["skill_labels"] for result in payload["results"] for check in result["checks"])
    assert all(result["dataset"] for result in payload["results"])


def test_scout_strategic_fit_requires_typed_domain_signal_rationale_and_evidence() -> None:
    expected = {
        "strategic_fit": {
            "opportunity_types": ["behavioral health AI"],
            "required_signals": ["validation study"],
            "source_confidence_min": 70,
        }
    }
    valid_record = {
        "company_name": "KBA Test Health",
        "opportunity_type": "behavioral health AI",
        "source_signals": ["validation study"],
        "keystone_fit_reason": "Supported behavioral-health validation fit.",
        "source_quality_summary": {"overall_score": 82},
        "approval_required_before_outreach": True,
    }

    valid = score_output_against_expected(
        {"records": [valid_record], "outreach_generated": False},
        expected,
        agent="scout",
    )
    assert next(check for check in valid.checks if check.name == "strategic_fit").passed

    invalid_records = {
        "wrong_domain": {
            **valid_record,
            "opportunity_type": "contract or RFP opportunity",
        },
        "unsupported_signal": {
            **valid_record,
            "source_signals": ["conference activity"],
        },
        "empty_rationale": {
            **valid_record,
            "keystone_fit_reason": "",
        },
        "inadequate_evidence": {
            **valid_record,
            "source_quality_summary": {"overall_score": 40},
        },
    }
    for failure_kind, record in invalid_records.items():
        result = score_output_against_expected(
            {"records": [record], "outreach_generated": False},
            expected,
            agent="scout",
        )
        strategic_fit = next(
            check for check in result.checks if check.name == "strategic_fit"
        )
        assert strategic_fit.passed is False, failure_kind


def test_static_eval_subset_runs() -> None:
    summary = run_static_evals(agent="gmail")

    assert summary.total == 7
    assert {result.agent for result in summary.results} == {"gmail"}
    assert summary.failed == 0


def test_failing_case_is_detected() -> None:
    score = score_output_against_expected(
        {
            "category": "newsletter",
            "needs_reply": False,
            "risk_flags": [],
            "draft_reply": "",
            "send_enabled": True,
        },
        {
            "category": "consulting_opportunity",
            "needs_reply": True,
            "risk_flags_exact": [],
            "draft_required": True,
            "draft_quality": {"required_terms": ["Thanks"]},
        },
        agent="gmail",
    )

    assert score.passed is False
    assert score.score < 1.0
    assert any(check.name == "no_auto_send" and not check.passed for check in score.checks)


def test_eval_report_renders() -> None:
    report = generate_eval_report(run_static_evals(agent="outreach"))

    assert "# Keystone Static Eval Report" in report
    assert "outreach/curebase_approved_context_email" in report
    assert "Skill labels" in report
    assert "Average score" in report
    assert "Prompt versions" in report
    assert "outreach_composer@2026-09-11.2" in report


def test_eval_cli_json_output(capsys) -> None:
    exit_code = run_evals_main(["--agent", "scout", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["failed"] == 0
    assert payload["prompt_metadata"]
    assert {result["agent"] for result in payload["results"]} == {"scout"}
    assert all(result["prompt_versions"] for result in payload["results"])


def test_no_live_apis_required(monkeypatch) -> None:
    def fail_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("static evals must not call live APIs")

    monkeypatch.setattr("keystone_agents.tools.serper_tool.requests.post", fail_network)

    summary = run_static_evals()

    assert summary.failed == 0


def test_custom_eval_dir_loads(tmp_path: Path) -> None:
    custom = tmp_path / "evals"
    custom.mkdir()
    (custom / "gmail_triage_cases.json").write_text(
        json.dumps(
            [
                {
                    "id": "inline_vendor",
                    "agent": "gmail",
                    "input": {
                        "subject": "Vendor demo",
                        "body": "Can we book a demo for a qualified leads platform?",
                    },
                    "expected": {
                        "category": "vendor",
                        "needs_reply": False,
                        "risk_flags_exact": [],
                        "draft_required": False,
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    summary = run_static_evals(agent="gmail", eval_dir=custom)

    assert summary.total == 1
    assert summary.failed == 0
