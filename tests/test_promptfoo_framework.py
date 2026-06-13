from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

import yaml
import pytest
import scripts.check_eval_slack_readiness as readiness
import scripts.run_eval_slack_test_server as slack_test_server
from promptfoo.assertions.kba_slack_invariants import grade_output
from promptfoo.eval_dashboard import render_dashboard, render_review_form
from promptfoo.eval_dashboard_server import (
    legacy_dashboard_redirect_target,
    save_human_review_payload,
    workflow_readiness_response,
)
from promptfoo.eval_database import (
    eval_case_status,
    eval_context_from_slack_thread,
    import_promptfoo_results,
    list_eval_cases,
    record_slack_eval_run,
    resolve_slack_eval_case_id,
    set_promptfoo_analysis_exclusion,
)
from promptfoo.human_review import (
    build_slack_review_template,
    list_human_reviews,
    parse_human_review,
    save_human_review,
)
from promptfoo.providers import keystone_agent_provider


def test_eval_slack_readiness_script_json(capsys, monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["check_eval_slack_readiness.py", "--json"])

    assert readiness.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pass"
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["prompt coverage"]["status"] == "pass"
    assert checks["eval channel config"]["status"] == "pass"
    assert checks["keystone-slack bridge contract"]["status"] in {"pass", "warn"}
    assert checks["keystone-slack runtime env"]["status"] == "pass"
    assert checks["keystone-slack scope declaration"]["status"] in {"pass", "warn"}
    assert checks["keystone-slack socket status"]["status"] == "pass"
    assert checks["slack bridge eval record"]["status"] == "pass"
    assert checks["dashboard link target"]["status"] == "pass"
    assert checks["slack bridge script output"]["status"] == "pass"
    assert checks["dashboard workflow readiness"]["status"] == "pass"
    assert "local-only workflow contract" in checks["dashboard workflow readiness"]["detail"]
    assert checks["fallback review cli"]["status"] == "pass"
    assert checks["score save cli"]["status"] == "pass"
    assert checks["dashboard human review"]["status"] == "pass"
    assert checks["status cli"]["status"] == "pass"
    assert checks["app mention thread flow"]["status"] == "pass"
    assert payload["tomorrow"]["dashboard_server"] == (
        "Run `export SLACK_CONFIGURED_BOT_SCOPES=app_mentions:read,chat:write,channels:history,groups:history` "
        "then `npm run eval:slack:strict-live-test-server`; paste the printed committed eval prompt for the "
        "shown agent into #evals and leave the server running before clicking Slack case links."
    )


def test_eval_slack_readiness_strict_fails_on_warnings(capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["check_eval_slack_readiness.py", "--json", "--strict"],
    )
    monkeypatch.setattr(
        readiness,
        "_run_checks",
        lambda **_: [
            {"name": "scope declaration", "status": "warn", "detail": "manual check needed"}
        ],
    )

    assert readiness.main() == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "fail"
    assert payload["strict"] is True
    assert payload["warning_count"] == 1


def test_eval_slack_scope_declaration_accepts_shell_metadata(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KEYSTONE_SLACK_REPO", str(tmp_path))
    monkeypatch.setenv(
        "SLACK_CONFIGURED_BOT_SCOPES",
        "app_mentions:read,chat:write,channels:history,groups:history",
    )

    result = readiness._keystone_slack_scope_declaration_check()

    assert result["status"] == "pass"


def test_eval_slack_live_read_probe_checks_token_history_and_replies(monkeypatch, tmp_path) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_slack_api_call(token: str, method: str, params: dict[str, str] | None = None):
        assert token == "xoxb-unit-test"
        payload = dict(params or {})
        calls.append((method, payload))
        if method == "conversations.history":
            return {"ok": True, "messages": [{"ts": "1800000000.000100"}]}
        return {"ok": True}

    monkeypatch.setenv("KEYSTONE_SLACK_REPO", str(tmp_path))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-unit-test")
    monkeypatch.setattr(readiness, "_slack_api_call", fake_slack_api_call)

    result = readiness._keystone_slack_live_read_probe()

    assert result["status"] == "pass"
    assert calls == [
        ("auth.test", {}),
        ("conversations.info", {"channel": "C0BA17Y9C01"}),
        ("conversations.history", {"channel": "C0BA17Y9C01", "limit": "1"}),
        (
            "conversations.replies",
            {"channel": "C0BA17Y9C01", "ts": "1800000000.000100", "limit": "1"},
        ),
    ]


def test_eval_slack_strict_test_server_stops_before_dashboard(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 7)

    def fail_dashboard() -> int:
        raise AssertionError("dashboard server should not start after failed readiness")

    monkeypatch.setattr(
        "sys.argv",
        [
            "run_eval_slack_test_server.py",
            "--strict-readiness",
            "--live-slack-probe",
        ],
    )
    monkeypatch.setattr(slack_test_server.subprocess, "run", fake_run)
    monkeypatch.setattr(slack_test_server, "dashboard_server_main", fail_dashboard)

    assert slack_test_server.main() == 7
    assert calls
    assert calls[0][-2:] == ["--strict", "--live-slack-probe"]


def test_eval_slack_test_server_clears_wrapper_args_before_dashboard(monkeypatch) -> None:
    calls: list[list[str]] = []
    observed_dashboard_argv: list[str] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0)

    def fake_dashboard() -> int:
        observed_dashboard_argv.extend(slack_test_server.sys.argv)
        return 0

    monkeypatch.setattr(
        "sys.argv",
        [
            "run_eval_slack_test_server.py",
            "--strict-readiness",
            "--live-slack-probe",
        ],
    )
    monkeypatch.setattr(slack_test_server.subprocess, "run", fake_run)
    monkeypatch.setattr(slack_test_server, "dashboard_server_main", fake_dashboard)

    assert slack_test_server.main() == 0
    assert calls[0][-2:] == ["--strict", "--live-slack-probe"]
    assert observed_dashboard_argv == ["run_eval_slack_test_server.py"]


def test_eval_slack_test_server_prints_committed_agent_prompt(monkeypatch, capsys) -> None:
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0)

    def fake_dashboard() -> int:
        return 0

    monkeypatch.setattr(
        "sys.argv",
        [
            "run_eval_slack_test_server.py",
            "--skip-readiness",
            "--agent",
            "chief_of_staff",
        ],
    )
    monkeypatch.setattr(slack_test_server.subprocess, "run", fake_run)
    monkeypatch.setattr(slack_test_server, "dashboard_server_main", fake_dashboard)

    assert slack_test_server.main() == 0
    output = capsys.readouterr().out
    assert "Start with this committed eval prompt in #evals:" in output
    assert "Agent: chief_of_staff" in output
    assert "Case dashboard: http://127.0.0.1:8769/dashboard?case=" in output
    assert "Paste: @KNI chief of staff" in output


def test_eval_slack_test_server_default_prompt_is_committed_eval_case() -> None:
    starter = slack_test_server._resolve_starter_eval_prompt(Path.cwd())

    assert starter["case_id"] == slack_test_server.DEFAULT_START_CASE_ID
    assert starter["agent"] == "business_research_analyst"
    assert starter["user_input"].startswith("@KNI business research analyst")
    assert starter["dashboard_url"].endswith(f"?case={slack_test_server.DEFAULT_START_CASE_ID}")


def test_promptfoo_seed_pack_has_unique_15_case_agent_coverage() -> None:
    counts: Counter[str] = Counter()
    prompts: defaultdict[str, list[str]] = defaultdict(list)
    for path in sorted(Path("promptfoo/tests").glob("*.yaml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        cases = loaded.get("tests", []) if isinstance(loaded, dict) else loaded
        for case in cases:
            variables = (case or {}).get("vars") or {}
            agent = variables.get("agent_under_test")
            prompt = variables.get("user_input")
            if not agent:
                continue
            counts[agent] += 1
            if prompt:
                prompts[agent].append(prompt)

    assert counts == {
        "business_research_analyst": 15,
        "chief_of_staff": 15,
        "gmail_triage": 15,
        "opportunity_scout": 15,
        "orchestrator": 15,
        "outreach_composer": 15,
    }
    assert {
        agent: len(values) - len(set(values))
        for agent, values in sorted(prompts.items())
    } == {
        "business_research_analyst": 0,
        "chief_of_staff": 0,
        "gmail_triage": 0,
        "opportunity_scout": 0,
        "orchestrator": 0,
        "outreach_composer": 0,
    }


def test_promptfoo_source_provided_cases_are_context_complete() -> None:
    failures: list[str] = []
    for path in sorted(Path("promptfoo/tests").glob("*.yaml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        cases = loaded.get("tests", []) if isinstance(loaded, dict) else loaded
        for case in cases:
            variables = (case or {}).get("vars") or {}
            case_id = str(variables.get("case_id") or "")
            dimensions = str(variables.get("eval_dimensions") or "")
            user_input = str(variables.get("user_input") or "").lower()
            source_or_thread_case = (
                "source_provided" in dimensions
                or "this thread" in user_input
                or "linked" in user_input
                or "source excerpt" in user_input
                or "approved facts" in user_input
                or "approved brief" in user_input
                or "pasted" in user_input
            )
            expected_status = str(variables.get("expected_status") or "")
            if not source_or_thread_case or expected_status == "blocked":
                continue
            messages = ((variables.get("slack_context") or {}).get("thread_messages") or [])
            text = " ".join(str(message.get("text") or "") for message in messages)
            if not messages:
                failures.append(f"{path}:{case_id}: missing thread_messages")
                continue
            if "example.org" in text or "Links:" in text or len(text.strip()) < 80:
                failures.append(f"{path}:{case_id}: thread context is not self-contained")

    assert failures == []


def test_promptfoo_assertion_accepts_source_grounded_slack_output() -> None:
    output = json.dumps(
        {
            "provider_status": "ok",
            "status": "done",
            "route": "opportunity_scout",
            "human_summary": "NeuroFlow is relevant to behavioral health. fixture://source",
            "source_count": 1,
            "slack_context_attached": True,
            "can_send_email": False,
            "send_enabled": False,
            "forbidden_actions": ["send_email"],
        }
    )
    result = grade_output(
        output,
        {
            "vars": {
                "expected_route": "opportunity_scout",
                "min_source_count": 1,
                "required_terms": ["behavioral health"],
            }
        },
    )

    assert result["pass"] is True
    assert result["score"] == 1.0


def test_promptfoo_assertion_rejects_missing_visible_sources() -> None:
    output = json.dumps(
        {
            "provider_status": "ok",
            "status": "done",
            "route": "company_research",
            "human_summary": "Lindus Health appears relevant.",
            "source_count": 1,
            "slack_context_attached": True,
            "can_send_email": False,
            "send_enabled": False,
            "forbidden_actions": ["send_email"],
        }
    )
    result = grade_output(
        output,
        {"vars": {"expected_route": "company_research", "min_source_count": 1}},
    )

    assert result["pass"] is False
    assert "visible_source_count" in result["reason"]


def test_promptfoo_assertion_checks_tooling_and_source_metadata() -> None:
    output = json.dumps(
        {
            "provider_status": "ok",
            "status": "done",
            "route": "opportunity_scout",
            "human_summary": "RFP source fixture://government/rfp",
            "source_count": 1,
            "source_urls": ["fixture://government/rfp"],
            "source_types": ["government"],
            "artifact_count": 1,
            "artifact_types": ["opportunity"],
            "context_pack_type": "opportunity",
            "workflow": ["opportunity_scout", "business_research_analyst"],
            "next_action_agent": "business_research_analyst",
            "slack_context_attached": True,
            "can_send_email": False,
            "send_enabled": False,
            "forbidden_actions": ["send_email"],
            "live_sdk": False,
            "live_search": False,
        }
    )
    result = grade_output(
        output,
        {
            "vars": {
                "expected_route": "opportunity_scout",
                "expected_pack_type": "opportunity",
                "expected_next_action_agent": "business_research_analyst",
                "required_artifact_types": "opportunity",
                "required_source_types": "government",
                "required_source_url_prefixes": "fixture://government/",
                "required_workflow_routes": "opportunity_scout, business_research_analyst",
                "min_source_count": 1,
                "min_artifact_count": 1,
            }
        },
    )

    assert result["pass"] is True


def test_promptfoo_assertion_rejects_metadata_style_summary() -> None:
    output = json.dumps(
        {
            "provider_status": "ok",
            "status": "done",
            "route": "opportunity_scout",
            "human_summary": "provider_status=ok source_count=3 route_result=opportunity_scout",
            "source_count": 3,
            "slack_context_attached": True,
            "can_send_email": False,
            "send_enabled": False,
            "forbidden_actions": ["send_email"],
        }
    )

    result = grade_output(
        output,
        {
            "vars": {
                "expected_route": "opportunity_scout",
                "min_source_count": 1,
                "require_visible_sources": False,
            }
        },
    )

    assert result["pass"] is False
    assert "metadata" in result["reason"]


def test_human_review_parses_slack_thread_scores_and_stores_them(tmp_path) -> None:
    review = parse_human_review(
        """
        eval score
        case: slack_behavioral_health_rfp_001
        run: eval-2026-06-11
        agent: opportunity_scout
        accuracy: 4
        relevance: 5
        explainability: 4
        readability: 5
        source_quality: 4
        search_quality: 3
        synthesis: 4
        output: 4
        format: 5
        instruction_following: 5
        usefulness: 5
        safety: pass
        notes: Accurate and readable; one RFP was only adjacent.
        """,
        slack_thread_ts="1781201244.891169",
    )

    assert review.case_id == "slack_behavioral_health_rfp_001"
    assert review.run_id == "eval-2026-06-11"
    assert review.slack_channel_id == "C0BA17Y9C01"
    assert review.slack_channel_name == "evals"
    assert review.safety == "pass"
    assert review.scores["accuracy"] == 4
    assert review.scores["readability"] == 5
    assert review.scores["uniqueness"] == 4
    assert "output_quality" not in review.scores
    assert review.average_score == 4.364

    database_path = tmp_path / "human-reviews.sqlite"
    row_id = save_human_review(review, database_path=database_path)
    stored = list_human_reviews(database_path=database_path)

    assert row_id == 1
    assert stored[0]["case_id"] == review.case_id
    assert stored[0]["scores"]["explainability"] == 4
    assert stored[0]["slack_thread_ts"] == "1781201244.891169"


def test_human_review_template_includes_readability_and_explainability() -> None:
    template = build_slack_review_template(
        case_id="slack_behavioral_health_rfp_001",
        run_id="eval-2026-06-11",
        agent="opportunity_scout",
    )

    assert "explainability:" in template
    assert "readability:" in template
    assert "source_quality:" in template
    assert "Score each dimension 0-5" in template
    assert "not metadata-heavy" in template


def test_eval_database_imports_promptfoo_results_and_links_human_review(tmp_path) -> None:
    results_path = tmp_path / "latest-eval.json"
    results_path.write_text(
        json.dumps(
            {
                "evalId": "eval-example",
                "results": {
                    "timestamp": "2026-06-11T18:00:00Z",
                    "stats": {"successes": 1, "failures": 0, "errors": 0},
                    "results": [
                        {
                            "id": "result-1",
                            "testIdx": 0,
                            "success": True,
                            "score": 1,
                            "latencyMs": 123,
                            "vars": {
                                "case_id": "slack_behavioral_health_rfp_001",
                                "agent_under_test": "opportunity_scout",
                                "eval_dimensions": "retrieval, synthesis",
                                "user_input": "@KNI opportunity scout find grants",
                            },
                            "gradingResult": {"reason": "All assertions passed"},
                            "response": {"output": "{\"route\":\"opportunity_scout\"}"},
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "evals.sqlite"

    summary = import_promptfoo_results(results_path, database_path=database_path)
    review = parse_human_review(
        """
        eval score
        case: slack_behavioral_health_rfp_001
        run: sbar_example
        agent: opportunity_scout
        accuracy: 4
        relevance: 5
        safety: pass
        notes: Good enough to use.
        """,
        slack_thread_ts="1781202023.470699",
    )
    save_human_review(review, database_path=database_path)

    status = eval_case_status(
        "slack_behavioral_health_rfp_001",
        database_path=database_path,
    )
    cases = list_eval_cases(database_path=database_path)

    assert summary.total == 1
    assert status["latest_promptfoo"]["success"] is True
    assert status["latest_promptfoo"]["reason"] == "All assertions passed"
    assert status["latest_human_review"]["average_score"] == 4.5
    assert status["promptfoo_result_count"] == 1
    assert status["human_review_count"] == 1
    assert cases[0]["case_id"] == "slack_behavioral_health_rfp_001"
    assert cases[0]["latest_promptfoo_success"] == 1


def test_promptfoo_import_records_eval_run_average_scores(tmp_path) -> None:
    results_path = tmp_path / "latest-eval.json"
    results_path.write_text(
        json.dumps(
            {
                "evalId": "eval-score-rollup",
                "results": {
                    "timestamp": "2026-06-11T18:00:00Z",
                    "stats": {"successes": 1, "failures": 1, "errors": 0},
                    "results": [
                        {
                            "id": "result-1",
                            "testIdx": 0,
                            "success": True,
                            "score": 1,
                            "vars": {
                                "case_id": "slack_rollup_opp_001",
                                "agent_under_test": "opportunity_scout",
                                "user_input": "@KNI opportunity scout find companies",
                            },
                            "gradingResult": {"reason": "All assertions passed"},
                            "response": {"output": "{}"},
                        },
                        {
                            "id": "result-2",
                            "testIdx": 1,
                            "success": False,
                            "score": 0.5,
                            "vars": {
                                "case_id": "slack_rollup_bra_001",
                                "agent_under_test": "business_research_analyst",
                                "user_input": "@KNI business research analyst research company",
                            },
                            "gradingResult": {"reason": "One assertion failed"},
                            "response": {"output": "{}"},
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "evals.sqlite"

    import_promptfoo_results(results_path, database_path=database_path)
    output_path = render_dashboard(
        database_path=database_path,
        output_path=tmp_path / "dashboard.html",
    )

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT average_score, agent_scores_json
            FROM promptfoo_eval_runs
            WHERE eval_id = 'eval-score-rollup'
            """
        ).fetchone()
    agent_scores = json.loads(row[1])
    assert row[0] == 0.75
    assert agent_scores["opportunity_scout"]["average_score"] == 1.0
    assert agent_scores["opportunity_scout"]["case_count"] == 1
    assert agent_scores["business_research_analyst"]["average_score"] == 0.5

    html = output_path.read_text(encoding="utf-8")
    assert "Machine Avg / 5" in html
    assert "Runs & Scoring" in html
    assert "Case-level scoring surface for Promptfoo checks" in html
    script_match = re.search(
        r'<script id="eval-data" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert script_match is not None
    data = json.loads(script_match.group(1))
    assert data["summary"]["latest_eval_average_score"] == 0.75
    assert data["eval_runs"][0]["average_score"] == 0.75
    assert data["eval_runs"][0]["agent_scores"] == agent_scores


def test_promptfoo_dashboard_does_not_backfill_scores_for_older_runs(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE promptfoo_eval_runs (
                eval_id TEXT PRIMARY KEY,
                result_path TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                imported_at TEXT NOT NULL,
                total INTEGER NOT NULL DEFAULT 0,
                successes INTEGER NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                stats_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO promptfoo_eval_runs (
                eval_id, result_path, created_at, imported_at, total,
                successes, failures, errors, stats_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "eval-before-rollup",
                "old.json",
                "2026-06-10T18:00:00Z",
                "2026-06-10T18:00:01Z",
                2,
                2,
                0,
                0,
                "{}",
            ),
        )
        connection.commit()

    output_path = render_dashboard(
        database_path=database_path,
        output_path=tmp_path / "dashboard.html",
    )

    html = output_path.read_text(encoding="utf-8")
    script_match = re.search(
        r'<script id="eval-data" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert script_match is not None
    data = json.loads(script_match.group(1))
    assert data["summary"]["latest_eval_average_score"] is None
    assert data["eval_runs"][0]["average_score"] is None
    assert data["eval_runs"][0]["agent_scores"] == {}


def test_eval_database_resolves_natural_slack_ask_to_existing_promptfoo_case(
    tmp_path,
) -> None:
    results_path = tmp_path / "latest-eval.json"
    results_path.write_text(
        json.dumps(
            {
                "evalId": "eval-course",
                "results": {
                    "timestamp": "2026-06-11T18:00:00Z",
                    "stats": {"successes": 1, "failures": 0, "errors": 0},
                    "results": [
                        {
                            "id": "result-1",
                            "testIdx": 0,
                            "success": True,
                            "score": 1,
                            "vars": {
                                "case_id": "slack_agents_sdk_course_001",
                                "agent_under_test": "opportunity_scout",
                                "user_input": (
                                    "@KNI opportunity scout -- find three Agents SDK "
                                    "courses that are reasonably cost and good for "
                                    "someone with some experience"
                                ),
                            },
                            "gradingResult": {"reason": "All assertions passed"},
                            "response": {
                                "output": json.dumps(
                                    {
                                        "human_summary": (
                                            "Course scan found source-backed options."
                                        )
                                    }
                                )
                            },
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "evals.sqlite"
    import_promptfoo_results(results_path, database_path=database_path)

    case_id = resolve_slack_eval_case_id(
        request_text=(
            "find three Agents SDK courses that are reasonably cost and good "
            "for someone with some experience"
        ),
        agent="opportunity_scout",
        slack_channel_id="C0BA17Y9C01",
        slack_channel_name="evals",
        database_path=database_path,
    )

    assert case_id == "slack_agents_sdk_course_001"


def test_eval_database_recovers_case_from_slack_thread(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_agents_sdk_course_001",
        run_id="wi_course",
        agent="opportunity_scout",
        slack_channel_id="C0BA17Y9C01",
        slack_channel_name="evals",
        slack_thread_ts="1781206953.875749",
        request_text="find three Agents SDK courses",
        database_path=database_path,
    )

    fields = eval_context_from_slack_thread(
        slack_thread_ts="1781206953.875749",
        slack_channel_id="C0BA17Y9C01",
        slack_channel_name="evals",
        database_path=database_path,
    )

    assert fields == {
        "case_id": "slack_agents_sdk_course_001",
        "run_id": "wi_course",
        "agent": "opportunity_scout",
    }


def test_eval_dashboard_renders_promptfoo_slack_and_human_state(tmp_path) -> None:
    results_path = tmp_path / "latest-eval.json"
    results_path.write_text(
        json.dumps(
            {
                "evalId": "eval-dashboard",
                "results": {
                    "timestamp": "2026-06-11T18:00:00Z",
                    "stats": {"successes": 1, "failures": 0, "errors": 0},
                    "results": [
                        {
                            "id": "result-1",
                            "testIdx": 0,
                            "success": True,
                            "score": 1,
                            "vars": {
                                "case_id": "slack_agents_sdk_course_001",
                                "agent_under_test": "opportunity_scout",
                                "eval_dimensions": "routing, human_scoring",
                                "user_input": "@KNI opportunity scout -- find courses",
                            },
                            "gradingResult": {"reason": "All assertions passed"},
                            "response": {
                                "output": json.dumps(
                                    {
                                        "human_summary": (
                                            "Course scan found source-backed options."
                                        )
                                    }
                                )
                            },
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "evals.sqlite"
    import_promptfoo_results(results_path, database_path=database_path)
    save_human_review(
        parse_human_review(
            """
            eval score
            case: slack_agents_sdk_course_001
            run: wi_course
            agent: opportunity_scout
            accuracy: 4
            relevance: 5
            safety: pass
            notes: Useful course scan.
            """,
            slack_thread_ts="1781206953.875749",
        ),
        database_path=database_path,
    )
    output_path = render_dashboard(
        database_path=database_path,
        output_path=tmp_path / "dashboard.html",
    )

    html = output_path.read_text(encoding="utf-8")
    assert "Keystone Eval Dashboard" in html
    assert "slack_agents_sdk_course_001" in html
    assert "opportunity_scout" in html
    assert "Human Avg / 5" in html
    assert "Machine Pass Rate" in html
    assert "Scoring notes" in html
    assert "Promptfoo scores are backend assertion checks" in html
    assert "Human Quality Review" in html
    assert "Promptfoo Run Analysis" in html
    assert "Case Changes Across Runs" in html
    assert "Slack Eval Conversation Flow" in html
    assert "Cost-safe #evals loop. Stage bars use saved database rows" in html
    assert "workflow-stage-bar" in html
    assert "Source checks" in html
    assert "Thread preview" in html
    assert "what should appear in #evals after one agent run" in html
    assert "Suggested Slack test run" in html
    assert "workflow-disclosure" in html
    assert "Expected live calls when enabled" in html
    assert "Expected in-thread communication flow" in html
    assert "Human review form" in html
    assert "Promptfoo machine-check status" in html
    assert "do not rerun Promptfoo from Slack thread" in html
    assert "slack_company_research_001" in html
    assert "copy into #evals" in html
    assert "no extra agent response is needed" in html
    assert "Score from the linked form" in html
    assert "@KNI how is this eval doing?" in html
    assert "without extra model calls" in html
    assert "Secondary diagnostics" in html
    assert "Planned chart backlog" in html
    assert "Review completion funnel" in html
    assert "Machine vs human coverage" in html
    assert "Cost-safe run volume" in html
    assert "Prompt score distribution" in html
    assert "Agent score comparison" in html
    assert "Dimension readiness heatmap" in html
    assert "Machine trend line" in html
    assert "Human review trend line" in html
    assert "One AI agent call per root eval" in html
    assert "Interaction guardrails" in html
    assert "Future live trigger" in html
    assert "Live now" in html
    assert "No API call from dashboard copy" in html
    assert "Use dry-run fixtures/cache first; cap live retrieval to accepted root run" in html
    assert "No model call; form uses saved case, run id, thread, and response context" in html
    assert "No model call; no Slack post" in html
    assert "Machine score /5" in html
    assert "Human avg /5" in html
    assert "uniqueness" in html
    assert "display_prompt_number" in html
    assert "Useful course scan." in html
    assert "text-block" in html
    assert "#2454a6" not in html
    script_match = re.search(
        r'<script id="eval-data" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert script_match is not None
    data = json.loads(script_match.group(1))
    case = next(item for item in data["cases"] if item["case_id"] == "slack_agents_sdk_course_001")
    assert case["user_input"].startswith("@KNI opportunity scout -- find three Agents SDK courses")
    assert case["response_text"] == ""
    assert case["display_prompt_number"] == "001"
    assert case["human_scores"]["accuracy"] == 4
    assert data["analysis"]["run_trends"]
    assert data["analysis"]["agent_score_trends"]["series"]["all"]
    assert "opportunity_scout" in data["analysis"]["agent_score_trends"]["agents"]
    assert "Average Score Over Time" in html
    assert 'id="analysis-agent-trend-filter"' in html
    assert "human_case_trends" in data["analysis"]
    assert data["workflow_readiness"]["mode"] == "local_preview"
    assert data["workflow_readiness"]["live_api_calls"] is False
    assert data["workflow_readiness"]["counts"]["total_cases"] > 0
    starter_plan = data["workflow_readiness"]["starter_run_plan"]
    assert starter_plan["channel"] == "#evals"
    assert starter_plan["case_id"] == "slack_company_research_001"
    assert starter_plan["display_case_id"].endswith("_012")
    assert starter_plan["paste_text"].startswith("@KNI business research analyst")
    assert starter_plan["local_only_now"] is True
    assert starter_plan["dashboard_url"].endswith("?case=slack_company_research_001")
    assert starter_plan["human_review_url"].endswith("?case=slack_company_research_001")
    assert starter_plan["status_reply"] == "@KNI how is this eval doing?"
    assert any(item["speaker"] == "KNI eval context" for item in starter_plan["thread_sequence"])
    assert any(item["step"] == "Promptfoo summary" for item in starter_plan["expected_live_calls_when_enabled"])
    assert len(starter_plan["expected_live_calls_when_enabled"]) >= 4
    interactions = data["workflow_readiness"]["interactions"]
    assert [item["interaction"] for item in interactions] == [
        "Prompt copy",
        "Agent thread reply",
        "Promptfoo machine summary",
        "Retrieval/source evidence",
        "Human review form open",
        "Human review save",
        "Analysis inclusion",
    ]
    assert all(item["live_api_call_now"] is False for item in interactions)
    assert interactions[0]["cost_guardrail"] == "No API call from dashboard copy"
    assert interactions[2]["cost_guardrail"] == "Use imported Promptfoo result by case_id; do not rerun Promptfoo from Slack thread"
    assert interactions[3]["cost_guardrail"] == "Use dry-run fixtures/cache first; cap live retrieval to accepted root run"
    assert interactions[4]["cost_guardrail"] == "No model call; form uses saved case, run id, thread, and response context"
    assert interactions[5]["cost_guardrail"] == "No model call; no Slack post"
    assert data["score_dimensions"]
    assert "review-form" in html
    assert "Score saving is disabled until this case has a recorded Promptfoo or Slack response." in html


def test_eval_dashboard_analysis_exclusion_updates_database_and_analysis(tmp_path) -> None:
    results_path = tmp_path / "latest-eval.json"
    results_path.write_text(
        json.dumps(
            {
                "evalId": "eval-dashboard",
                "results": {
                    "timestamp": "2026-06-11T18:00:00Z",
                    "stats": {"successes": 1, "failures": 0, "errors": 0},
                    "results": [
                        {
                            "id": "result-1",
                            "testIdx": 0,
                            "success": True,
                            "score": 1,
                            "vars": {
                                "case_id": "slack_agents_sdk_course_001",
                                "agent_under_test": "opportunity_scout",
                                "eval_dimensions": "routing, human_scoring",
                                "user_input": (
                                    "@KNI opportunity scout -- find three Agents SDK courses "
                                    "that are reasonably cost and good for someone with some experience"
                                ),
                            },
                            "gradingResult": {"reason": "All assertions passed"},
                            "response": {"output": "{}"},
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "evals.sqlite"
    import_promptfoo_results(results_path, database_path=database_path)

    set_promptfoo_analysis_exclusion(
        eval_id="eval-dashboard",
        case_id="slack_agents_sdk_course_001",
        excluded=True,
        reason="duplicate retry",
        database_path=database_path,
    )
    output_path = render_dashboard(
        database_path=database_path,
        output_path=tmp_path / "dashboard.html",
    )

    html = output_path.read_text(encoding="utf-8")
    script_match = re.search(
        r'<script id="eval-data" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert script_match is not None
    data = json.loads(script_match.group(1))
    case = next(item for item in data["cases"] if item["case_id"] == "slack_agents_sdk_course_001")
    assert case["analysis_excluded"] is True
    assert case["analysis_exclusion_reason"] == "duplicate retry"
    assert data["analysis"]["run_trends"] == []
    assert data["analysis"]["case_trends"] == []
    assert "Include in analysis" in html
    assert "analysis excluded" in html


def test_eval_dashboard_coverage_uses_seed_pack_not_ad_hoc_slack_cases(tmp_path) -> None:
    results_path = tmp_path / "extra-eval.json"
    results_path.write_text(
        json.dumps(
            {
                "evalId": "eval-extra-case",
                "results": {
                    "timestamp": "2026-06-11T18:00:00Z",
                    "stats": {"successes": 1, "failures": 0, "errors": 0},
                    "results": [
                        {
                            "id": "result-extra",
                            "testIdx": 0,
                            "success": True,
                            "score": 1,
                            "vars": {
                                "case_id": "slack_runtime_extra_bra_001",
                                "agent_under_test": "business_research_analyst",
                                "eval_dimensions": "slack_interface",
                                "user_input": "@KNI business research analyst summarize the current thread",
                            },
                            "gradingResult": {"reason": "All assertions passed"},
                            "response": {"output": "{}"},
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "evals.sqlite"
    import_promptfoo_results(results_path, database_path=database_path)

    output_path = render_dashboard(
        database_path=database_path,
        output_path=tmp_path / "dashboard.html",
    )

    html = output_path.read_text(encoding="utf-8")
    script_match = re.search(
        r'<script id="eval-data" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert script_match is not None
    data = json.loads(script_match.group(1))
    assert data["summary"]["agents"]["business_research_analyst"] == 15
    assert data["summary"]["coverage"]["business_research_analyst"]["count"] == 15
    assert data["summary"]["coverage_source"] == "promptfoo seed cases"
    assert "Save human review" in html
    assert 'data-score="${escapeHtml(dimension)}"' in html


def test_eval_dashboard_server_saves_form_review_payload(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    result = save_human_review_payload(
        {
            "case_id": "slack_agents_sdk_course_001",
            "run_id": "wi_course",
            "agent": "opportunity_scout",
            "scores": {
                "accuracy": 4,
                "relevance": 5,
                "readability": 4,
                "usefulness": 5,
            },
            "safety": "pass",
            "notes": "Useful and readable.",
            "slack_thread_ts": "1781206953.875749",
        },
        database_path=database_path,
    )

    assert result["case_id"] == "slack_agents_sdk_course_001"
    assert result["average_score"] == 4.5
    stored = list_human_reviews(
        database_path=database_path,
        case_id="slack_agents_sdk_course_001",
    )
    assert stored[0]["run_id"] == "wi_course"
    assert stored[0]["scores"]["relevance"] == 5
    assert stored[0]["notes"] == "Useful and readable."


def test_eval_review_form_blocks_unrun_case_until_response_exists(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"

    html = render_review_form(
        case_id="slack_agents_sdk_course_001",
        database_path=database_path,
    )

    assert "Score saving is disabled until this case has a recorded Promptfoo or Slack response." in html
    assert '<button type="submit" disabled>Save human review</button>' in html
    assert '<option value="" selected>tbd</option>' in html
    with pytest.raises(ValueError, match="recorded Promptfoo or Slack response"):
        save_human_review_payload(
            {
                "case_id": "slack_agents_sdk_course_001",
                "scores": {"accuracy": 4},
                "safety": "pass",
            },
            database_path=database_path,
            require_recorded_response=True,
        )


def test_eval_review_form_allows_scoring_recorded_response(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_agents_sdk_course_001",
        run_id="wi_course",
        agent="opportunity_scout",
        slack_channel_id="C0BA17Y9C01",
        slack_channel_name="evals",
        slack_thread_ts="1781206953.875749",
        request_text="@KNI opportunity scout -- find three Agents SDK courses",
        result_summary="Course scan complete.",
        database_path=database_path,
    )

    html = render_review_form(
        case_id="slack_agents_sdk_course_001",
        database_path=database_path,
    )
    result = save_human_review_payload(
        {
            "case_id": "slack_agents_sdk_course_001",
            "scores": {"accuracy": 4, "relevance": 5},
            "safety": "pass",
        },
        database_path=database_path,
        require_recorded_response=True,
    )

    assert '<button type="submit">Save human review</button>' in html
    assert "Course scan complete." in html
    assert result["average_score"] == 4.5


def test_eval_dashboard_server_exposes_local_workflow_readiness_contract(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    results_path = tmp_path / "latest-eval.json"
    results_path.write_text(
        json.dumps(
            {
                "evalId": "eval-workflow-readiness",
                "results": {
                    "timestamp": "2026-06-11T18:00:00Z",
                    "stats": {"successes": 1, "failures": 0, "errors": 0},
                    "results": [
                        {
                            "id": "result-1",
                            "testIdx": 0,
                            "success": True,
                            "score": 1,
                            "vars": {
                                "case_id": "slack_agents_sdk_course_001",
                                "agent_under_test": "opportunity_scout",
                                "eval_dimensions": "routing, retrieval, source_visibility",
                                "user_input": "@KNI opportunity scout -- find three Agents SDK courses",
                            },
                            "gradingResult": {"reason": "All assertions passed"},
                            "response": {"output": "{\"human_summary\":\"Course scan complete.\"}"},
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    import_promptfoo_results(results_path, database_path=database_path)

    payload = workflow_readiness_response(database_path=database_path)

    readiness = payload["workflow_readiness"]
    assert payload["status"] == "ok"
    assert readiness["mode"] == "local_preview"
    assert readiness["live_api_calls"] is False
    assert readiness["counts"]["total_cases"] > 0
    assert readiness["counts"]["retrieval_source_cases"] > 0
    assert readiness["starter_run_plan"]["channel"] == "#evals"
    assert readiness["starter_run_plan"]["case_id"] == "slack_company_research_001"
    assert readiness["starter_run_plan"]["paste_text"].startswith("@KNI business research analyst")
    assert readiness["starter_run_plan"]["local_only_now"] is True
    assert readiness["starter_run_plan"]["human_review_url"].endswith("?case=slack_company_research_001")
    assert any(item["speaker"] == "KNI eval context" for item in readiness["starter_run_plan"]["thread_sequence"])
    assert any(item["step"] == "Promptfoo summary" for item in readiness["starter_run_plan"]["expected_live_calls_when_enabled"])
    assert len(readiness["starter_run_plan"]["expected_live_calls_when_enabled"]) >= 4
    assert [item["interaction"] for item in readiness["interactions"]] == [
        "Prompt copy",
        "Agent thread reply",
        "Promptfoo machine summary",
        "Retrieval/source evidence",
        "Human review form open",
        "Human review save",
        "Analysis inclusion",
    ]
    assert all(item["live_api_call_now"] is False for item in readiness["interactions"])
    assert readiness["interactions"][0]["cost_guardrail"] == "No API call from dashboard copy"
    assert readiness["interactions"][2]["cost_guardrail"] == "Use imported Promptfoo result by case_id; do not rerun Promptfoo from Slack thread"
    assert readiness["interactions"][3]["cost_guardrail"] == "Use dry-run fixtures/cache first; cap live retrieval to accepted root run"
    assert readiness["interactions"][4]["cost_guardrail"] == "No model call; form uses saved case, run id, thread, and response context"


def test_eval_dashboard_server_redirects_legacy_dashboard_url() -> None:
    assert (
        legacy_dashboard_redirect_target("/.keystone/promptfoo/dashboard.html", "case=abc")
        == "http://127.0.0.1:8769/dashboard?case=abc"
    )
    assert legacy_dashboard_redirect_target("/dashboard", "") is None


def test_promptfoo_provider_compacts_ask_agent_payload(monkeypatch) -> None:
    stdout_payload = {
        "status": "done",
        "route": "opportunity_scout",
        "human_summary": "Source-backed result fixture://source",
        "orchestrator_preflight": {
            "route_result": {
                "route": "opportunity_scout",
                "target_agent": "Opportunity Scout Agent",
                "approval_required": True,
                "can_send_email": False,
                "send_enabled": False,
                "forbidden_actions": ["send_email"],
            }
        },
        "work_item": {
            "status": "done",
            "current_route": "opportunity_scout",
            "sources": [
                {
                    "url": "fixture://source",
                    "title": "Fixture source",
                    "source_type": "fixture",
                }
            ],
            "artifact_refs": [
                {
                    "artifact_id": "1",
                    "artifact_type": "opportunity",
                    "metadata": {
                        "source_refs": [
                            {
                                "url": "fixture://artifact-source",
                                "title": "Artifact source",
                                "source_type": "government",
                            }
                        ]
                    },
                }
            ],
            "next_action": {"agent": "business_research_analyst", "requires_approval": False},
            "target": {
                "metadata": {
                    "slack_context": {"channel_id": "C0BA17Y9C01"},
                    "manager_loop_efficiency": {
                        "final_synthesis_executed": True,
                        "live_sdk": False,
                        "live_search": False,
                    },
                }
            },
        },
    }

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(stdout_payload),
            stderr="",
        )

    monkeypatch.setattr(keystone_agent_provider.subprocess, "run", fake_run)
    result = keystone_agent_provider.call_api(
        "@KNI orchestrator agent \"find companies\"",
        {"config": {"python": ".venv/bin/python", "agent": "orchestrator"}},
        {
            "vars": {
                "surface": "slack",
                "user_input": "@KNI orchestrator agent \"find companies\"",
                "slack_context": {
                    "selected_message": {
                        "ts": "1800000000.000100",
                        "user_id": "U_EVAL",
                        "username": "anup",
                        "text": "@KNI orchestrator agent \"find companies\"",
                        "permalink": "https://kni.slack.com/archives/C/p1800000000000100",
                    }
                },
            }
        },
    )

    output = json.loads(result["output"])
    assert output["provider_status"] == "ok"
    assert output["route"] == "opportunity_scout"
    assert output["source_count"] == 2
    assert output["artifact_types"] == ["opportunity"]
    assert output["source_types"] == ["fixture", "government"]
    assert output["next_action_agent"] == "business_research_analyst"
    assert output["final_synthesis_executed"] is True
    assert output["slack_context_attached"] is True
    assert output["send_enabled"] is False


def test_promptfoo_provider_passes_natural_slack_text_as_input(monkeypatch) -> None:
    stdout_payload = {
        "status": "blocked",
        "route": "opportunity_scout",
        "human_summary": "Needs live search.",
        "orchestrator_preflight": {"route_result": {"route": "opportunity_scout"}},
        "work_item": {"status": "blocked", "current_route": "opportunity_scout"},
    }
    captured: dict[str, list[str]] = {}

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        captured["command"] = list(args[0])
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(stdout_payload),
            stderr="",
        )

    monkeypatch.setattr(keystone_agent_provider.subprocess, "run", fake_run)
    request_text = (
        "@KNI opportunity scout -- find three Agents SDK courses that are reasonably cost"
    )

    result = keystone_agent_provider.call_api(
        request_text,
        {"config": {"python": ".venv/bin/python", "agent": "orchestrator"}},
        {"vars": {"user_input": request_text}},
    )

    output = json.loads(result["output"])
    command = captured["command"]
    assert output["provider_status"] == "ok"
    assert "--input" not in command
    assert "--agent" not in command
    assert request_text in command
    assert command.count("--json") == 1
    assert command.index("--json") > command.index(request_text)
