from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import keystone_agents.cli as cli
from keystone_agents.cli import main
from keystone_agents.manual_request import infer_manual_request_plan
from keystone_agents.orchestrator.preflight_context import (
    MANUAL_REQUEST_PLAN_ENV,
    ORCHESTRATOR_PREFLIGHT_ENV,
    ORCHESTRATOR_ROUTE_RESULT_ENV,
)
from keystone_agents.schemas.operational_context import ZoteroContextResult
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkflowRunResult,
    WorkItem,
    WorkItemKind,
    WorkItemRoute,
    WorkItemStatus,
)
from keystone_agents.sdk import ToolGuardrailViolation
from keystone_agents.storage.sqlite_store import SQLiteStore
from promptfoo.eval_database import (
    eval_case_status,
    import_promptfoo_results,
    list_eval_trace_events,
    record_slack_eval_run,
)
from promptfoo.human_review import list_human_reviews


def _fake_orchestrator_preflight(
    request_text,
    *,
    requested_agent=None,
    live_manual_plan=False,
    **kwargs,
):
    del live_manual_plan, kwargs
    plan = infer_manual_request_plan(request_text, requested_agent=requested_agent)
    result = cli.route_request(request_text, manual_plan=plan)
    return cli.OrchestratorPreflight(
        request_text=str(request_text or ""),
        requested_agent=plan.requested_agent,
        advisory_only=plan.requested_agent not in {None, "orchestrator"},
        selected_agent=str(plan.requested_agent or result.route),
        blocked_by_orchestrator=bool(result.refused),
        execution_allowed=not bool(result.refused),
        block_kind="send" if result.refused else "",
        block_reason=result.stop_reason or "",
        manual_request_plan=plan,
        route_result=result,
    )


def test_cli_health_smoke(capsys) -> None:
    exit_code = main(["health", "--database-url", ":memory:"])

    assert exit_code == 0
    assert "Overall status:" in capsys.readouterr().out


def test_manual_plan_advice_preserves_gmail_as_first_stage() -> None:
    plan = infer_manual_request_plan(
        "Review Gmail today, select the strongest opportunity, then research the company.",
        requested_agent="gmail_triage",
    ).model_copy(
        update={
            "target_agent": "business_research_analyst",
            "intent": "gmail_triage",
        }
    )

    assert cli._route_with_manual_plan_advice("orchestrator", plan) == "gmail_triage"


def test_cli_promptfoo_agent_eval_mode_disables_eval_helpers(monkeypatch) -> None:
    monkeypatch.delenv("KEYSTONE_PROMPTFOO_EVAL", raising=False)
    assert cli._promptfoo_agent_eval_mode() is False

    monkeypatch.setenv("KEYSTONE_PROMPTFOO_EVAL", "true")
    assert cli._promptfoo_agent_eval_mode() is True


def test_cli_langgraph_helper_preserves_manager_step_limit(monkeypatch) -> None:
    import keystone_agents.langgraph_workflow as langgraph_workflow

    captured: dict[str, object] = {}
    graph_result = WorkflowRunResult(
        work_item=WorkItem(kind=WorkItemKind.RESEARCH_BRIEF, title="Graph result"),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary="Graph path used.",
    )

    class FakeOutcome:
        result = graph_result

    def fake_run_work_item_langgraph(request, **kwargs):  # type: ignore[no-untyped-def]
        captured["request"] = request
        captured.update(kwargs)
        return FakeOutcome()

    monkeypatch.setattr(
        langgraph_workflow,
        "run_work_item_langgraph",
        fake_run_work_item_langgraph,
    )
    request = WorkflowRunRequest(request_text="@KNI chief of staff summarize eval gaps")

    outcome = cli._run_work_item_langgraph_for_request(request, max_manager_steps=1)

    assert outcome.result is graph_result
    assert captured["request"] is request
    assert captured["manager_loop"] is True
    assert captured["max_manager_steps"] == 1


def test_cli_init_db_uses_explicit_database_url(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "keystone.db"

    exit_code = main(["init-db", "--database-url", f"sqlite:///{database_path}"])

    assert exit_code == 0
    assert database_path.exists()
    assert "Initialized SQLite database:" in capsys.readouterr().out


def test_cli_automations_list_and_audit(tmp_path: Path, capsys) -> None:
    database_url = f"sqlite:///{tmp_path / 'automations.db'}"

    list_exit = main(["automations", "list", "--database-url", database_url])
    audit_exit = main(["automations", "audit", "--database-url", database_url, "--json"])

    assert list_exit == 0
    assert audit_exit == 0
    output = capsys.readouterr().out
    assert "Weekly Opportunity Scan" in output
    assert "automation_specs" in output


def test_cli_route_stays_dry_run(capsys) -> None:
    exit_code = main(["route", "--input", "research Curebase"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Agent: orchestrator" in output
    assert "Send enabled: False" in output


def test_cli_ask_routes_unmentioned_input_through_work_item(tmp_path: Path, capsys) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask.db'}"
    exit_code = main(["ask", "--database-url", database_url, "research", "Curebase"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "WorkItem:" in output
    assert "Route: business_research_analyst" in output
    assert "Manual plan: business_research_analyst / company_research" in output
    assert "Artifacts: company_profile:" in output


def test_cli_ask_json_reports_actual_execution_and_graph_metadata(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask-execution.db'}"
    monkeypatch.setenv("KEYSTONE_WORKITEM_LANGGRAPH", "true")

    exit_code = main(
        [
            "ask",
            "--json",
            "--live-sdk",
            "--max-openai-requests",
            "0",
            "--database-url",
            database_url,
            (
                "Create a temporary Sheet named KBA_TEST_SHEET execution-metadata "
                "in KNIOps and prepare its cleanup plan. Run deterministically with "
                "no live SDK or model calls."
            ),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["_execution"] == {
        "langgraph": True,
        "live_sdk": False,
        "live_search": False,
        "openai_requests": 0,
    }
    assert payload["_langgraph"]["runtime"] in {
        "langgraph",
        "dependency_free_fallback",
    }
    assert "run_chief_of_staff" in payload["_langgraph"]["node_path"]


def test_cli_ask_eval_score_template_returns_prompted_rubric_json(capsys) -> None:
    exit_code = main(
        [
            "ask",
            "--json",
            "@KNI",
            "eval",
            "score",
            "template",
            "case",
            "slack_behavioral_health_rfp_001",
            "run",
            "sbar_example",
            "agent",
            "business_research_analyst",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "eval_score_template"
    assert payload["status"] == "done"
    assert payload["case_id"] == "slack_behavioral_health_rfp_001"
    assert payload["run_id"] == "sbar_example"
    assert payload["agent"] == "business_research_analyst"
    assert payload["slack_channel_id"] == "C0BA17Y9C01"
    assert "Score each dimension 0-5" in payload["human_summary"]
    assert "readability:" in payload["human_summary"]
    assert payload["dashboard_case_url"].endswith("?case=slack_behavioral_health_rfp_001")
    assert payload["dashboard_url"] == "http://127.0.0.1:8769/dashboard"
    assert payload["eval_thread_reply"]["scorecard_request"] == (
        "@KNI can you give me a scorecard for this eval?"
    )
    assert payload["send_enabled"] is False


def test_cli_ask_eval_score_template_plain_text(capsys) -> None:
    exit_code = main(
        [
            "ask",
            "@KNI",
            "eval",
            "scoring",
            "rubric",
            "case:slack_case_1",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert output.startswith("eval score")
    assert "case: slack_case_1" in output
    assert "metadata-heavy" in output


def test_cli_ask_eval_score_template_without_case_blocks_with_thread_guidance(capsys) -> None:
    exit_code = main(
        [
            "ask",
            "--json",
            "@KNI",
            "eval",
            "score",
            "template",
            "case",
            "<case_id>",
            "run",
            "<run_id>",
            "agent",
            "<agent_name>",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "eval_score_template"
    assert payload["status"] == "blocked"
    assert "could not resolve the eval case" in payload["human_summary"]
    assert "@KNI can you give me a scorecard for this eval?" in payload["human_summary"]


def test_cli_ask_eval_score_template_handles_wrapped_slack_followup(capsys) -> None:
    exit_code = main(
        [
            "ask",
            "--json",
            "@KNI",
            "workitem",
            "opportunity_scout",
            "continue",
            "this",
            "prior",
            "Slack",
            "thread.",
            "Linked",
            "WorkItem:",
            "wi_example",
            "Follow-up:",
            "eval",
            "score",
            "template",
            "case",
            "slack_behavioral_health_rfp_001",
            "run",
            "sbar_wrapped",
            "agent",
            "business_research_analyst",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "eval_score_template"
    assert payload["case_id"] == "slack_behavioral_health_rfp_001"
    assert payload["run_id"] == "sbar_wrapped"
    assert "accuracy:" in payload["human_summary"]


def test_cli_ask_eval_scorecard_infers_case_from_slack_thread_context(
    tmp_path: Path,
    capsys,
) -> None:
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1781202023.470699",
                "read_context": (
                    "Slack thread history digest:\n"
                    "Thread messages, oldest first:\n"
                    "- ts=1781202023.470699 author=UUSER title=Eval request: "
                    "@KNI business research analyst: eval case "
                    "slack_behavioral_health_rfp_001 Find 3 current grants/RFPs.\n"
                    "- ts=1781202025.859599 author=BKNI title=Business Agents Run Completed: "
                    "Run: sbar_bd1fe1d83aca4f93864a03c0618c5dfc "
                    "WorkItem: wi_e59cb39aeac24ec49d012b2f27cb244b "
                    "Route: business_research_analyst\n"
                ),
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "ask",
            "--json",
            "--context-file",
            str(context_file),
            "@KNI",
            "can",
            "you",
            "give",
            "me",
            "a",
            "scorecard",
            "for",
            "this",
            "eval?",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "eval_score_template"
    assert payload["case_id"] == "slack_behavioral_health_rfp_001"
    assert payload["run_id"] == "wi_e59cb39aeac24ec49d012b2f27cb244b"
    assert payload["agent"] == "business_research_analyst"
    assert "case: slack_behavioral_health_rfp_001" in payload["human_summary"]


def test_cli_ask_eval_score_reply_saves_human_review(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    review_db = tmp_path / "human-reviews.sqlite"
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(review_db))
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.selected_message_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1781201599.554659",
            }
        ),
        encoding="utf-8",
    )
    record_slack_eval_run(
        case_id="slack_behavioral_health_rfp_001",
        run_id="sbar_example",
        agent="business_research_analyst",
        slack_channel_id="C0BA17Y9C01",
        slack_channel_name="evals",
        slack_thread_ts="1781201599.554659",
        request_text="business research analyst eval case slack_behavioral_health_rfp_001",
        result_summary="Business Agents Run Completed",
        database_path=review_db,
    )
    score_block = """eval score
case: slack_behavioral_health_rfp_001
run: sbar_example
agent: business_research_analyst
accuracy: 4
relevance: 5
explainability: 4
readability: 5
source_quality: 4
search_quality: 4
synthesis: 4
output: 4
format: 5
instruction_following: 5
usefulness: 5
safety: pass
notes: Useful and source-backed enough for a first pass."""

    exit_code = main(
        [
            "ask",
            "--json",
            "--context-file",
            str(context_file),
            "@KNI",
            score_block,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "eval_score_saved"
    assert payload["status"] == "done"
    assert payload["case_id"] == "slack_behavioral_health_rfp_001"
    assert payload["run_id"] == "sbar_example"
    assert payload["average_score"] == 4.455
    assert payload["slack_channel_name"] == "evals"
    assert payload["slack_thread_ts"] == "1781201599.554659"
    assert payload["database_path"] == str(review_db)
    assert payload["dashboard_path"] == str((tmp_path / "dashboard.html").resolve())
    assert payload["dashboard_case_url"].endswith("?case=slack_behavioral_health_rfp_001")
    assert payload["refresh_targets"] == ["overview", "database", "runs_scoring", "analysis"]
    assert payload["trace_event_type"] == "human_review_saved"
    assert payload["eval_thread_reply"]["submit_evaluation_action"] == "Submit Evaluation"
    assert [
        action["action_id"] for action in payload["eval_thread_reply"]["slack_actions"]
    ] == ["kba_eval_review", "kba_eval_orchestrator_judge"]
    assert "human notes" in payload["eval_thread_reply"]["submit_evaluation_effect"]
    assert payload["eval_thread_reply"]["refresh_targets"] == [
        "overview",
        "database",
        "runs_scoring",
        "analysis",
    ]
    assert (tmp_path / "dashboard.html").exists()

    stored = list_human_reviews(database_path=review_db)
    assert len(stored) == 1
    assert stored[0]["scores"]["accuracy"] == 4
    assert stored[0]["scores"]["readability"] == 5


def test_cli_ask_natural_eval_score_reply_uses_slack_thread_context(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    review_db = tmp_path / "human-reviews.sqlite"
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(review_db))
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1781202023.470699",
                "read_context": (
                    "Original request: eval case slack_behavioral_health_rfp_001\n"
                    "Business Agents Run Completed Run: sbar_example "
                    "WorkItem: wi_e59cb39aeac24ec49d012b2f27cb244b "
                    "Route: business_research_analyst"
                ),
            }
        ),
        encoding="utf-8",
    )
    record_slack_eval_run(
        case_id="slack_behavioral_health_rfp_001",
        run_id="wi_e59cb39aeac24ec49d012b2f27cb244b",
        agent="business_research_analyst",
        slack_channel_id="C0BA17Y9C01",
        slack_channel_name="evals",
        slack_thread_ts="1781202023.470699",
        request_text="business research analyst eval case slack_behavioral_health_rfp_001",
        result_summary="Business Agents Run Completed",
        database_path=review_db,
    )

    score_reply = """Here are my scores:
accuracy 4
relevance 5
explainability 4
readability 5
source_quality 4
search_quality 4
synthesis 4
output 4
format 5
instruction_following 5
usefulness 5
safety pass
notes: Good enough for a first pass."""
    exit_code = main(
        [
            "ask",
            "--json",
            "--context-file",
            str(context_file),
            "@KNI",
            score_reply,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "eval_score_saved"
    assert payload["case_id"] == "slack_behavioral_health_rfp_001"
    assert payload["run_id"] == "wi_e59cb39aeac24ec49d012b2f27cb244b"
    assert payload["agent"] == "business_research_analyst"
    assert payload["average_score"] == 4.455
    assert payload["notes"] == "Good enough for a first pass."
    assert payload["dashboard_path"] == str((tmp_path / "dashboard.html").resolve())
    assert payload["dashboard_case_url"].endswith("?case=slack_behavioral_health_rfp_001")
    events = list_eval_trace_events(database_path=review_db, limit=5)
    review_event = next(event for event in events if event["event_type"] == "human_review_saved")
    assert review_event["group_id"] == "slack_behavioral_health_rfp_001"
    assert review_event["metadata"]["run_id"] == "wi_e59cb39aeac24ec49d012b2f27cb244b"


def test_cli_ask_eval_score_reply_requires_recorded_response(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    review_db = tmp_path / "human-reviews.sqlite"
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(review_db))
    score_reply = """eval score
case: slack_behavioral_health_rfp_001
run: wi_missing_response
agent: business_research_analyst
accuracy: 4
relevance: 5
explainability: 4
readability: 5
source_quality: 4
search_quality: 4
synthesis: 4
output: 4
format: 5
instruction_following: 5
usefulness: 5
safety: pass
notes: This should not save without a recorded response."""

    exit_code = main(["ask", "--json", "@KNI", score_reply])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "eval_score_saved"
    assert payload["status"] == "blocked"
    assert "recorded Promptfoo or Slack response" in payload["error"]


def test_cli_ask_eval_status_reports_promptfoo_and_human_scores(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    review_db = tmp_path / "evals.sqlite"
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(review_db))
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
                            "vars": {
                                "case_id": "slack_behavioral_health_rfp_001",
                                "agent_under_test": "opportunity_scout",
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
    import_promptfoo_results(results_path, database_path=review_db)
    score_block = """eval score
case: slack_behavioral_health_rfp_001
run: eval-example
agent: opportunity_scout
accuracy: 4
relevance: 5
explainability: 4
readability: 5
source_quality: 4
search_quality: 4
synthesis: 4
output: 4
format: 5
instruction_following: 5
usefulness: 5
safety: pass"""
    main(["ask", "@KNI", score_block])
    capsys.readouterr()

    exit_code = main(
        [
            "ask",
            "--json",
            "@KNI",
            "eval",
            "status",
            "case",
            "slack_behavioral_health_rfp_001",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "eval_status"
    assert payload["case_id"] == "slack_behavioral_health_rfp_001"
    assert "Promptfoo pass" in payload["human_summary"]
    assert "average 4.455/5" in payload["human_summary"]
    assert payload["dashboard_path"] == str((tmp_path / "dashboard.html").resolve())
    assert "<http://127.0.0.1:8769/dashboard?case=slack_behavioral_health_rfp_001|case dashboard>" in payload["human_summary"]
    assert payload["dashboard_case_url"].endswith("?case=slack_behavioral_health_rfp_001")
    assert payload["scorecard_request"] == "@KNI can you give me a scorecard for this eval?"
    assert payload["eval_thread_reply"]["submit_evaluation_action"] == "Submit Evaluation"


def test_cli_ask_natural_eval_status_infers_case_from_slack_thread_context(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    review_db = tmp_path / "evals.sqlite"
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(review_db))
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
                            "vars": {
                                "case_id": "slack_behavioral_health_rfp_001",
                                "agent_under_test": "opportunity_scout",
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
    import_promptfoo_results(results_path, database_path=review_db)
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1781202023.470699",
                "read_context": "Prior ask: eval case slack_behavioral_health_rfp_001",
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "ask",
            "--json",
            "--context-file",
            str(context_file),
            "@KNI",
            "how",
            "is",
            "this",
            "eval",
            "doing?",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "eval_status"
    assert payload["case_id"] == "slack_behavioral_health_rfp_001"
    assert "Promptfoo pass" in payload["human_summary"]
    assert payload["dashboard_path"] == str((tmp_path / "dashboard.html").resolve())
    assert payload["dashboard_case_url"].endswith("?case=slack_behavioral_health_rfp_001")
    assert payload["scorecard_request"] == "@KNI can you give me a scorecard for this eval?"


def test_hidden_eval_case_does_not_hijack_business_cleanup_status_request(
    tmp_path: Path,
) -> None:
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1783811494.407029",
                "read_context": "Hidden eval case slack_create_one_temporary_001",
            }
        ),
        encoding="utf-8",
    )

    payload = cli._eval_status_payload(
        (
            "Create one temporary Sheet, verify provider operations, and return "
            "the cleanup status and graph evidence."
        ),
        context_file_path=str(context_file),
    )

    assert payload is None


def test_cli_ask_agent_override_ignores_eval_context_without_eval_request(
    tmp_path: Path,
    capsys,
) -> None:
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C0ASJ6QU1FX",
                "channel_name": "ai-agents-workflow",
                "thread_ts": "1781818754.293029",
                "read_context": "Prior eval case slack_behavioral_health_rfp_001",
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "ask",
            "--agent",
            "opportunity_scout",
            "--json",
            "--context-file",
            str(context_file),
            "flexible",
            "case",
            "flex_opp_20260618_001_natural_opportunity_directions",
            "Can",
            "you",
            "look",
            "for",
            "2",
            "realistic",
            "Keystone",
            "opportunity",
            "directions",
            "around",
            "clinical",
            "AI",
            "evaluation?",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload.get("mode") != "eval_status"
    assert payload["selected_agent"] == "opportunity_scout"
    assert payload["route"] == "opportunity_scout"


def test_cli_ask_eval_case_with_slack_context_records_slack_eval_run(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    review_db = tmp_path / "evals.sqlite"
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(review_db))
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.selected_message_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1781202023.470699",
                "selected_message_ts": "1781202023.470699",
            }
        ),
        encoding="utf-8",
    )
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"

    exit_code = main(
        [
            "ask",
            "--json",
            "--database-url",
            database_url,
            "--context-file",
            str(context_file),
            "@KNI",
            "business",
            "research",
            "analyst:",
            "eval",
            "case",
            "slack_behavioral_health_rfp_001",
            "research",
            "Curebase",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["_eval_record"]["case_id"] == "slack_behavioral_health_rfp_001"
    assert payload["_eval_record"]["slack_thread_ts"] == "1781202023.470699"
    assert payload["_eval_record"]["run_id"].startswith("wi_")
    assert payload["_eval_record"]["dashboard_case_url"].endswith(
        "?case=slack_behavioral_health_rfp_001"
    )
    assert payload["_eval_record"]["scorecard_request"] == (
        "@KNI can you give me a scorecard for this eval?"
    )
    assert payload["_eval_record"]["dashboard_visibility"]["case_visible"] is True
    assert payload["_eval_record"]["dashboard_visibility"]["latest_run_visible"] is True
    assert payload["_eval_record"]["dashboard_visibility"]["trace_event_visible"] is True
    assert payload["_eval_record"]["post_save_state"]["merged_status"]["slack_run_count"] == 1
    assert payload["_eval_record"]["post_save_state"]["trace"]["manual_run_summary_present"] is True
    assert "/api/status?refresh=1" in payload["_eval_record"]["refresh_endpoints"]
    assert "/api/trace-diagnostics" in payload["_eval_record"]["refresh_endpoints"]
    assert payload["_eval_record"]["eval_thread_reply"]["dashboard_case_url"].endswith(
        "?case=slack_behavioral_health_rfp_001"
    )
    assert [
        action["action_id"]
        for action in payload["_eval_record"]["eval_thread_reply"]["slack_actions"]
    ] == ["kba_eval_review", "kba_eval_orchestrator_judge"]
    assert payload["_eval_record"]["review_case_url"].endswith(
        "?case=slack_behavioral_health_rfp_001"
    )
    assert "Eval: case `slack_behavioral_health_rfp_001`" in payload["human_summary"]
    assert "<http://127.0.0.1:8769/dashboard?case=slack_behavioral_health_rfp_001|case dashboard>" in payload["human_summary"]
    assert "<http://127.0.0.1:8769/review?case=slack_behavioral_health_rfp_001|score this case>" in payload["human_summary"]
    assert "press `Submit Evaluation` in Slack" in payload["human_summary"]

    status = eval_case_status("slack_behavioral_health_rfp_001", database_path=review_db)
    assert status["slack_run_count"] == 1
    assert status["slack_runs"][0]["run_id"] == payload["_eval_record"]["run_id"]
    assert status["slack_runs"][0]["agent"] == "business_research_analyst"
    assert status["slack_runs"][0]["work_item_id"] == payload["_eval_record"]["run_id"]
    assert status["slack_runs"][0]["thread_fetch_status"] in {"ok", "not_requested"}
    assert status["slack_runs"][0]["thread_message_count"] >= 0
    assert status["slack_runs"][0]["response_hash"]
    assert status["slack_runs"][0]["evidence"]["schema"] == "keystone.slack.eval_evidence.v1"


def test_cli_ask_records_hidden_eval_case_from_slack_context(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    review_db = tmp_path / "evals.sqlite"
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(review_db))
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1781206953.875749",
                "request_ts": "1781206953.875749",
                "thread_fetch_status": "ok",
                "thread_messages": [
                    {
                        "ts": "1781206953.875749",
                        "user": "U123",
                        "text": "opportunity scout eval case",
                    }
                ],
                "request_text": (
                    "opportunity scout -- find three Agents SDK courses that are "
                    "reasonably cost and good for someone with some experience"
                ),
                "eval": {
                    "case_id": "slack_agents_sdk_course_001",
                    "source": "slack_eval_channel_backend",
                    "visible_in_prompt": False,
                },
            }
        ),
        encoding="utf-8",
    )
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"

    exit_code = main(
        [
            "ask",
            "--json",
            "--database-url",
            database_url,
            "--context-file",
            str(context_file),
            "@KNI",
            "opportunity scout -- find three Agents SDK courses that are reasonably cost",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["_eval_record"]["case_id"] == "slack_agents_sdk_course_001"
    assert payload["_eval_record"]["slack_thread_ts"] == "1781206953.875749"
    assert payload["_eval_record"]["dashboard_case_url"].endswith(
        "?case=slack_agents_sdk_course_001"
    )
    assert "Eval: case `slack_agents_sdk_course_001`" in payload["human_summary"]
    assert "<http://127.0.0.1:8769/dashboard?case=slack_agents_sdk_course_001|case dashboard>" in payload["human_summary"]
    assert "<http://127.0.0.1:8769/review?case=slack_agents_sdk_course_001|score this case>" in payload["human_summary"]

    status = eval_case_status("slack_agents_sdk_course_001", database_path=review_db)
    assert status["slack_run_count"] == 1
    assert status["slack_runs"][0]["request_text"].startswith("find three Agents SDK courses")
    assert "eval case" not in status["slack_runs"][0]["request_text"].lower()
    assert status["slack_runs"][0]["thread_fetch_status"] == "ok"
    assert status["slack_runs"][0]["thread_message_count"] == 1
    assert status["slack_runs"][0]["response_hash"]
    assert status["slack_runs"][0]["evidence"]["source"] == "cli_slack_context"


def test_cli_hidden_eval_case_records_run_and_natural_human_review(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    review_db = tmp_path / "evals.sqlite"
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(review_db))
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1781206953.875749",
                "request_ts": "1781206953.875749",
                "request_text": (
                    "opportunity scout -- find three Agents SDK courses that are "
                    "reasonably cost and good for someone with some experience"
                ),
                "eval": {
                    "case_id": "slack_agents_sdk_course_001",
                    "source": "slack_eval_channel_backend",
                    "visible_in_prompt": False,
                },
            }
        ),
        encoding="utf-8",
    )
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"

    run_exit = main(
        [
            "ask",
            "--json",
            "--database-url",
            database_url,
            "--context-file",
            str(context_file),
            "@KNI",
            "opportunity scout -- find three Agents SDK courses that are reasonably cost",
        ]
    )

    assert run_exit == 0
    run_payload = json.loads(capsys.readouterr().out)
    run_id = run_payload["_eval_record"]["run_id"]
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1781206953.875749",
                "read_context": (
                    "Slack thread history digest\n"
                    f"Business Agents WorkItem Advanced WorkItem: {run_id} "
                    "Route: opportunity_scout\n"
                ),
                "eval": {
                    "case_id": "slack_agents_sdk_course_001",
                    "source": "slack_eval_channel_backend",
                    "visible_in_prompt": False,
                },
            }
        ),
        encoding="utf-8",
    )
    score_reply = """Here are my scores:
accuracy 4
relevance 4
explainability 5
readability 5
source_quality 4
search_quality 4
synthesis 4
output 5
format 5
instruction_following 5
usefulness 5
safety pass
notes: Strong answer, but course price recency should be rechecked."""

    score_exit = main(
        [
            "ask",
            "--json",
            "--context-file",
            str(context_file),
            "@KNI",
            score_reply,
        ]
    )

    assert score_exit == 0
    score_payload = json.loads(capsys.readouterr().out)
    assert score_payload["mode"] == "eval_score_saved"
    assert score_payload["case_id"] == "slack_agents_sdk_course_001"
    assert score_payload["run_id"] == run_id
    assert score_payload["agent"] == "opportunity_scout"
    assert score_payload["dashboard_path"] == str((tmp_path / "dashboard.html").resolve())
    assert score_payload["dashboard_case_url"].endswith("?case=slack_agents_sdk_course_001")

    status = eval_case_status("slack_agents_sdk_course_001", database_path=review_db)
    assert status["slack_run_count"] == 1
    assert status["latest_human_review"]["case_id"] == "slack_agents_sdk_course_001"
    assert status["latest_human_review"]["run_id"] == run_id
    assert status["latest_human_review"]["safety"] == "pass"


def test_cli_eval_channel_natural_ask_reuses_promptfoo_case_and_scores_by_thread(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    review_db = tmp_path / "evals.sqlite"
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(review_db))
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
                            "response": {"output": "{}"},
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    import_promptfoo_results(results_path, database_path=review_db)
    context_file = tmp_path / "slack-context.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "thread_ts": "1781207777.000100",
                "request_ts": "1781207777.000100",
                "request_text": (
                    "opportunity scout -- find three Agents SDK courses that are "
                    "reasonably cost and good for someone with some experience"
                ),
            }
        ),
        encoding="utf-8",
    )
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"

    run_exit = main(
        [
            "ask",
            "--json",
            "--database-url",
            database_url,
            "--context-file",
            str(context_file),
            "@KNI",
            "opportunity scout -- find three Agents SDK courses that are reasonably cost "
            "and good for someone with some experience",
        ]
    )

    assert run_exit == 0
    run_payload = json.loads(capsys.readouterr().out)
    run_id = run_payload["_eval_record"]["run_id"]
    assert run_payload["_eval_record"]["case_id"] == "slack_agents_sdk_course_001"
    assert run_payload["_eval_record"]["dashboard_path"] == str(
        (tmp_path / "dashboard.html").resolve()
    )
    assert run_payload["_eval_record"]["dashboard_case_url"].endswith(
        "?case=slack_agents_sdk_course_001"
    )
    status = eval_case_status("slack_agents_sdk_course_001", database_path=review_db)
    assert status["slack_run_count"] == 1
    assert "eval case" not in status["slack_runs"][0]["request_text"].lower()

    score_reply = """Looks good:
accuracy 4
relevance 5
explainability 5
readability 5
source_quality 4
search_quality 4
synthesis 4
output 5
format 5
instruction_following 5
usefulness 5
safety pass
notes: This is readable and useful."""
    score_exit = main(
        [
            "ask",
            "--json",
            "--context-file",
            str(context_file),
            "@KNI",
            score_reply,
        ]
    )

    assert score_exit == 0
    score_payload = json.loads(capsys.readouterr().out)
    assert score_payload["mode"] == "eval_score_saved"
    assert score_payload["case_id"] == "slack_agents_sdk_course_001"
    assert score_payload["run_id"] == run_id
    assert score_payload["agent"] == "opportunity_scout"
    assert score_payload["notes"] == "This is readable and useful."


def test_cli_work_items_advance_zotero_collection_outputs_research_brief(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    cache_dir = tmp_path / "zotero-cache"
    cache_dir.mkdir()
    collection_key = "LTA3U8I8"
    (cache_dir / "zotero_collections.json").write_text(
        json.dumps({"collections": {"LH 01 - REACH-tDCS & Lindus Trial Context": collection_key}}),
        encoding="utf-8",
    )
    (cache_dir / "zotero_items.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "data": {
                            "key": "ITEM1",
                            "title": "Remote tDCS randomized trial",
                            "url": "https://pubmed.ncbi.nlm.nih.gov/example/",
                            "DOI": "10.1000/example",
                            "abstractNote": "A randomized trial tested home-based tDCS for MDD.",
                            "itemType": "journalArticle",
                            "collections": [collection_key],
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYSTONE_ZOTERO_IMPORT_CACHE", str(cache_dir))
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"

    exit_code = main(
        [
            "work-items",
            "advance",
            "--input",
            (
                "Ask the Business Research Analyst to summarize the Zotero collection "
                "'LH 01 - REACH-tDCS & Lindus Trial Context' with one paragraph per source"
            ),
            "--database-url",
            database_url,
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Route: business_research_analyst" in output
    assert "Business Research Analyst attached a source-backed Zotero research brief" in output
    assert "Artifacts: research_brief:" in output
    assert "Remote tDCS randomized trial" in output


def test_cli_work_items_advance_uses_orchestrator_preflight_and_manager_loop(
    tmp_path: Path,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"

    exit_code = main(
        [
            "work-items",
            "advance",
            "--json",
            "--input",
            "research Curebase",
            "--database-url",
            database_url,
            "--max-manager-steps",
            "1",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["route"] == "business_research_analyst"
    assert payload["manual_request_plan"]["target_agent"] == "business_research_analyst"
    assert payload["orchestrator_preflight"]["selected_agent"] == "business_research_analyst"
    assert payload["orchestrator_preflight"]["preflight_memo"]["raw_request"] == (
        "research Curebase"
    )

    events = SQLiteStore(database_url).list_work_item_events(payload["work_item"]["id"])
    review_events = [event for event in events if event.event_type == "manager_loop_review"]
    assert review_events
    assert review_events[0].metadata["route"] == "business_research_analyst"
    assert review_events[0].metadata["review_decision"] in {"pass", "warn", "block"}


def test_cli_ask_colon_named_business_research_does_not_auto_handoff_to_scout(
    tmp_path: Path,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"

    exit_code = main(
        [
            "ask",
            "--no-live-sdk",
            "--database-url",
            database_url,
            "@KNI",
            "business",
            "research",
            "analyst:",
            "diagnostic",
            "case",
            "diag_business_research_no_live_route_001",
            "Research",
            "Abridge",
            "as",
            "a",
            "clinical",
            "documentation",
            "AI",
            "company.",
            "Return",
            "a",
            "concise",
            "Answer,",
            "Detailed",
            "Summary,",
            "and",
            "Useful",
            "references.",
            "Focus",
            "on",
            "product/workflow,",
            "healthcare",
            "buyer",
            "fit,",
            "evidence",
            "or",
            "deployment",
            "signals,",
            "and",
            "what",
            "remains",
            "unverified.",
            "Do",
            "not",
            "draft",
            "outreach,",
            "send,",
            "schedule,",
            "write",
            "files,",
            "create",
            "CRM",
            "records,",
            "publish,",
            "or",
            "post",
            "elsewhere.",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Manual plan: business_research_analyst / company_research" in output
    assert "Route: business_research_analyst" in output
    assert "Research: Abridge" in output
    assert "*Answer:*" in output
    assert "Abridge has source-backed company context" in output
    assert "Route: opportunity_scout" not in output
    assert "step 2 opportunity_scout" not in output
    stored_items = SQLiteStore(database_url).list_work_items(limit=1)
    assert stored_items
    assert stored_items[0].title == "Research: Abridge"


def test_cli_work_items_advance_preflight_uses_existing_specialist_route(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"
    store = SQLiteStore(database_url)
    work_item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        status=WorkItemStatus.BLOCKED,
        title="Research: OpenEvidence",
        request_text="business research analyst research OpenEvidence",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        last_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
    )
    store.save_work_item(work_item)
    captured: dict[str, object] = {}

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        captured["request_text"] = request_text
        captured["requested_agent"] = requested_agent
        captured["live_manual_plan"] = live_manual_plan
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_advance_work_item_manager_loop(request, **_kwargs):
        captured["manual_request_plan"] = request.manual_request_plan
        return WorkflowRunResult(
            work_item=work_item,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            status=WorkItemStatus.BLOCKED,
            advanced=True,
            human_summary="Specialist continuation accepted.",
            manual_request_plan=request.manual_request_plan,
            orchestrator_preflight=request.orchestrator_preflight,
        )

    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setenv("KNI_BUSINESS_AGENTS_LANGGRAPH", "false")
    monkeypatch.setattr(
        cli,
        "advance_work_item_manager_loop",
        fake_advance_work_item_manager_loop,
    )

    exit_code = main(
        [
            "work-items",
            "advance",
            work_item.id,
            "--json",
            "--live-sdk",
            "--input",
            "business research analyst research OpenEvidence\nFollow-up: look at partnerships",
            "--database-url",
            database_url,
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert captured["requested_agent"] == "business_research_analyst"
    assert captured["live_manual_plan"] is True
    assert captured["manual_request_plan"]["target_agent"] == "business_research_analyst"
    assert payload["orchestrator_preflight"]["selected_agent"] == "business_research_analyst"


def test_cli_work_items_advance_zotero_article_outputs_research_brief(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    cache_dir = tmp_path / "zotero-cache"
    cache_dir.mkdir()
    (cache_dir / "zotero_collections.json").write_text(
        json.dumps({"collections": {"LH 01 - REACH-tDCS": "LTA3U8I8"}}),
        encoding="utf-8",
    )
    (cache_dir / "zotero_items.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "data": {
                            "key": "AP9SKRPZ",
                            "title": (
                                "Study Details | NCT06976697 | Home-Based tDCS "
                                "Treatment Of Major Depressive Disorder"
                            ),
                            "url": "https://clinicaltrials.gov/study/NCT06976697",
                            "abstractNote": "",
                            "itemType": "webpage",
                            "collections": ["LTA3U8I8"],
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYSTONE_ZOTERO_IMPORT_CACHE", str(cache_dir))
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"

    exit_code = main(
        [
            "work-items",
            "advance",
            "--input",
            (
                "Ask the Business Research Analysit to find and summarize the Zotero "
                "article on the Lindus SOOMA trial. Include one paragraph summary, "
                "methods/design, inclusion/exclusion, and other relevant trial info"
            ),
            "--database-url",
            database_url,
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Route: business_research_analyst" in output
    assert "Business Research Analyst attached a source-backed Zotero research brief" in output
    assert "Artifacts: research_brief:" in output
    assert "NCT06976697" in output


def test_cli_ask_kni_mention_uses_work_item(tmp_path: Path, capsys) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask-kni.db'}"
    exit_code = main(
        [
            "ask",
            "--database-url",
            database_url,
            "@KNI",
            "business",
            "agent",
            "analyst",
            "research",
            "Lindus",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "WorkItem:" in output
    assert "Route: business_research_analyst" in output
    assert "Advanced: True" in output


def test_cli_ask_generic_kni_opportunity_to_outreach_runs_workflow(
    tmp_path: Path,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask-loop.db'}"
    exit_code = main(
        [
            "ask",
            "--json",
            "--database-url",
            database_url,
            "@KNI",
            "run",
            "one",
            "opportunity-to-outreach",
            "loop",
            "for",
            "behavioral",
            "health",
            "AI.",
            "Top",
            "1",
            "only.",
            "Post",
            "approval",
            "to",
            "this",
            "channel.",
            "Draft",
            "only,",
            "do",
            "not",
            "send.",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["workflow"] == "opportunity_to_outreach_loop"
    assert payload["selected_agent"] == "orchestrator"
    assert payload["topic"] == "behavioral health AI"
    assert payload["top_n"] == 1
    assert payload["send_enabled"] is False
    assert len(payload["output"]["items"]) == 1
    posts = payload["output"]["storage"]["slack_approval_posts"]
    assert len(posts) == 1
    assert posts[0]["status"] == "dry-run"
    assert posts[0]["send_enabled"] is False


def test_cli_ask_agent_override_keeps_direct_dry_run(capsys) -> None:
    exit_code = main(["ask", "--agent", "business_research_analyst", "research", "Lindus"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Agent: Business Research Analyst" in output
    assert "Mode: dry_run" in output


def test_cli_ask_agent_override_selects_specialist_json(capsys) -> None:
    exit_code = main(
        [
            "ask",
            "--agent",
            "opportunity_scout",
            "--json",
            "find psychiatry AI opportunities",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"selected_agent": "opportunity_scout"' in output
    assert '"send_enabled": false' in output


def test_cli_ask_context_agent_mention_uses_first_class_dry_run(
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)

    exit_code = main(
        [
            "ask",
            "--json",
            "@KNI",
            "airtable",
            "context",
            "agent",
            "identify",
            "schema",
            "mapping",
            "and",
            "record",
            "identity",
            "questions",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "airtable_context_agent"
    assert payload["route"] == "airtable_context_agent"
    assert payload["status"] == "done"
    assert payload["output"]["agent_name"] == "airtable_context_agent"
    assert payload["output"]["mode"] == "deterministic"
    assert "schema mapping" in payload["human_summary"]
    assert "record identity" in payload["human_summary"]
    assert payload["send_enabled"] is False
    assert payload["side_effects"]["external_write_performed"] is False


def test_cli_ask_kni_multistep_uses_backend_selected_langgraph(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'kni_multistep_graph.db'}"
    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)

    exit_code = main(
        [
            "ask",
            "--json",
            "--database-url",
            database_url,
            "@KNI",
            "research",
            "NeuroFlow",
            "and",
            "find",
            "matching",
            "opportunities",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    events = SQLiteStore(database_url).list_work_item_events(payload["work_item"]["id"])
    graph_event = next(event for event in events if event.event_type == "langgraph_orchestration")

    assert exit_code == 0
    assert payload["route"] in {
        WorkItemRoute.OPPORTUNITY_SCOUT.value,
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
    }
    assert any(event.event_type == "langgraph_orchestration" for event in events)
    assert any(event.event_type == "langgraph_manager_loop_completed" for event in events)
    assert {
        "run_opportunity_scout",
        "run_business_research",
    } <= set(graph_event.metadata["node_path"])


def test_cli_airtable_context_infers_finance_expense_receipt_target(
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)

    exit_code = main(
        [
            "ask",
            "--agent",
            "airtable_context_agent",
            "--json",
            "add a business expense to the airtable business expenses based on the receipt "
            "details which are: /tmp/example-business-cards-receipt.pdf",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    output = payload["output"]
    assert output["base_alias"] == "finance_tax_tracker"
    assert output["relevant_tables"] == ["Business Expenses"]
    assert "Total Expenses" in output["relevant_fields"]
    assert output["write_plan"]["operation"] == (
        "airtable_specialist_create_from_receipt_after_schema_and_approval"
    )
    assert output["write_plan"]["target"] == "finance_tax_tracker / Business Expenses"
    assert "receipt_local_path" in {
        item["key"] for item in output["write_plan"]["field_mapping"]
    }
    assert "Confirmed base/table/record identity" not in output["approval_needs"]
    assert "Confirmed expense-table schema field mapping" in output["approval_needs"]
    assert "not ask the operator to restate the base or table" in output["summary"]
    assert "Tax Payments" not in output["relevant_tables"]
    assert payload["send_enabled"] is False
    assert payload["side_effects"]["external_write_performed"] is False


def test_airtable_context_dry_run_keeps_named_tracker_expense_target() -> None:
    output = cli._context_agent_dry_run_output(
        "airtable_context_agent",
        (
            "Create one synthetic Business Expenses record in the configured Finance & "
            "Tax Tracker and link the receipt https://example.test/receipt.pdf. Then "
            "update and remove only that marked test record."
        ),
        None,
    )

    assert output is not None
    payload = output.model_dump(mode="json")
    assert payload["base_alias"] == "finance_tax_tracker"
    assert payload["relevant_tables"] == ["Business Expenses"]
    assert payload["write_plan"]["target"] == (
        "finance_tax_tracker / Business Expenses"
    )
    assert "Tax Payments" not in payload["summary"]


def test_chief_calendar_fast_path_infers_defaults_without_model_or_graph(
    capsys,
) -> None:
    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--no-live-sdk",
            "--json",
            "on November 4th add an all day calendar event that Frontiers in Human "
            "Dynamics paper Due Date with note submit the final paper",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "chief_of_staff"
    assert payload["calendar_action"]["start_date"].endswith("-11-04")
    assert payload["calendar_action"]["calendar_id"] == "primary"
    assert payload["calendar_action"]["timezone"] == "America/New_York"
    assert payload["calendar_action"]["description"] == "submit the final paper"
    assert payload["tool_receipt"]["status"] == "dry-run"
    assert payload["openai_requests"] == 0
    assert "manual_request_plan" not in payload


def test_chief_calendar_fast_path_executes_complete_live_write_immediately(
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_create(title: str, start_date: str, **kwargs: object) -> dict[str, object]:
        captured.update({"title": title, "start_date": start_date, **kwargs})
        return {
            "status": "success",
            "operation": "create_calendar_event",
            "event_id": "kba-calendar-event",
            "title": title,
            "start_date": start_date,
            "html_link": "https://calendar.test/event",
            "verification": {"status": "verified", "passed": True},
            "send_enabled": False,
        }

    monkeypatch.setattr(cli, "create_google_calendar_event_impl", fake_create)
    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--live-sdk",
            "--json",
            "on November 4th add an all day calendar event that Frontiers in Human "
            "Dynamics paper Due Date",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "done"
    assert payload["openai_requests"] == 0
    assert payload["side_effects"]["calendar_write_performed"] is True
    assert captured["live"] is True
    assert str(captured["approval_reference"]).startswith("calendar-direct:")


def test_chief_calendar_fast_path_resolves_natural_update_reference(
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_resolve(event_reference: str, **kwargs: object) -> dict[str, object]:
        captured.update({"event_reference": event_reference, "lookup": kwargs})
        return {
            "status": "success",
            "operation": "resolve_calendar_event",
            "event_reference": event_reference,
            "event_id": "kba-calendar-event",
            "title": "Frontiers in Human Dynamics paper Due Date",
            "start_date": "2026-11-04",
            "match_count": 1,
        }

    def fake_update(event_id: str, **kwargs: object) -> dict[str, object]:
        captured.update({"event_id": event_id, "update": kwargs})
        return {
            "status": "success",
            "operation": "update_calendar_event",
            "event_id": event_id,
            "title": "Frontiers in Human Dynamics paper Due Date",
            "start_date": "2026-11-04",
            "description_present": True,
            "verification": {"status": "verified", "passed": True},
            "send_enabled": False,
        }

    monkeypatch.setattr(cli, "resolve_google_calendar_event_impl", fake_resolve)
    monkeypatch.setattr(cli, "update_google_calendar_event_impl", fake_update)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--live-sdk",
            "--json",
            "change the note on the Frontiers in Human Dynamics paper Due Date event ",
            "to submit the final paper",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "done"
    assert payload["openai_requests"] == 0
    assert payload["calendar_action"]["event_id"] == ""
    assert payload["calendar_action"]["event_reference"] == (
        "Frontiers in Human Dynamics paper Due Date"
    )
    assert payload["calendar_lookup"]["match_count"] == 1
    assert captured["event_id"] == "kba-calendar-event"
    assert captured["update"]["description"] == "submit the final paper"
    assert captured["update"]["live"] is True


def test_chief_calendar_natural_update_dry_run_previews_lookup_without_write(
    capsys,
) -> None:
    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--no-live-sdk",
            "--json",
            "change the note on the Frontiers paper due date event to submit the final paper",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dry-run"
    assert payload["calendar_lookup"]["status"] == "dry-run"
    assert payload["calendar_lookup"]["event_reference"] == "Frontiers paper due date"
    assert payload["side_effects"]["calendar_write_performed"] is False
    assert payload["openai_requests"] == 0


def test_cli_no_live_chief_receipt_command_returns_bounded_write_plan(
    capsys,
    monkeypatch,
    tmp_path: Path,
) -> None:
    from keystone_agents.finance_expense_receipts import FinanceReceiptEvidence

    receipt_path = tmp_path / "example-business-cards-receipt.pdf"
    receipt_path.write_bytes(b"%PDF-1.4\nreceipt fixture")

    def fake_extract(path: str) -> FinanceReceiptEvidence:
        return FinanceReceiptEvidence(
            source_path=str(path),
            filename=Path(path).name,
            content_read=True,
            extraction_method="fixture",
            vendor="Example Print Inc.",
            receipt_date="2026-06-28",
            order_number="1002003",
            description="Business Cards",
            quantity="50",
            subtotal="31.00",
            shipping="45.80",
            total="76.80",
            currency="USD",
            payment_summary="credit card ending in 0000",
            estimated_tax_periods="Q3",
        )

    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(
        "keystone_agents.workflow_runner.extract_finance_receipt_evidence",
        fake_extract,
    )

    exit_code = main(
        [
            "ask",
            "--no-live-sdk",
            "--json",
            "--database-url",
            f"sqlite:///{tmp_path / 'ask.db'}",
            "@KNI",
            "chief",
            "of",
            "staff",
            "add",
            "a",
            "business",
            "expense",
            "to",
            "the",
            "airtable",
            "business",
            "expenses",
            "based",
            "on",
            "the",
            "receipt",
            "details",
            "which",
            "are:",
            str(receipt_path),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    human_summary = payload["human_summary"]
    assert exit_code == 0
    assert payload["status"] == "done"
    assert payload["route"] == "chief_of_staff"
    assert payload["manual_request_plan"]["intent"] == "business_system_write"
    assert payload["manual_request_plan"]["target_type"] == "business_system_context"
    assert "Read-only only" not in human_summary
    assert "finance_tax_tracker" in human_summary
    assert "Business Expenses" in human_summary
    assert "Example Print Inc." in human_summary
    assert "Q3" in human_summary
    assert "76.80" in human_summary
    assert "airtable_create_expense_from_receipt" in human_summary
    assert payload["next_action"]["requires_approval"] is True
    assert payload["artifact_refs"][0]["metadata"]["send_enabled"] is False
    assert payload["artifact_refs"][0]["metadata"]["slack_post_allowed"] is False


def test_cli_ask_chief_of_staff_outputs_deterministic_plan(capsys) -> None:
    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "see my email and send an update to the #onboarding channel",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "chief_of_staff"
    assert payload["output"]["recommended_route"]["workflow_type"] == "gmail-summary"
    assert payload["output"]["recommended_route"]["target_channel"] == "onboarding"
    assert payload["output"]["slack_post_allowed"] is False


def test_cli_ask_chief_of_staff_dry_run_uses_orchestrator_manual_plan(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}

    class FakeRoute:
        workflow_type = "chief_of_staff"
        command_text = "@KNI chief of staff review"
        target_channel = ""

    class FakeChiefResult:
        summary = "Chief of Staff used parent Orchestrator plan."
        recommended_route = FakeRoute()
        slack_post_allowed = False

        def model_dump(self, **_kwargs):
            return {
                "summary": self.summary,
                "recommended_route": {
                    "workflow_type": self.recommended_route.workflow_type,
                    "command_text": self.recommended_route.command_text,
                    "target_channel": self.recommended_route.target_channel,
                },
                "slack_post_allowed": self.slack_post_allowed,
            }

    def fake_plan_chief_of_staff_request(*_args, **kwargs):
        captured["manual_request_plan"] = kwargs.get("manual_request_plan")
        return FakeChiefResult()

    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "plan_chief_of_staff_request", fake_plan_chief_of_staff_request)

    exit_code = main(
        [
            "ask",
            "--json",
            "--agent",
            "chief_of_staff",
            "review",
            "the",
            "state",
            "of",
            "KNI",
            "2026",
            "and",
            "why",
            "the",
            "prior",
            "answer",
            "was",
            "unrelated",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    manual_plan = captured["manual_request_plan"]

    assert exit_code == 0
    assert manual_plan is not None
    assert manual_plan.target_agent == "chief_of_staff"
    assert payload["orchestrator_preflight"]["manual_request_plan"]["target_agent"] == (
        "chief_of_staff"
    )
    assert payload["output"]["summary"] == "Chief of Staff used parent Orchestrator plan."


def test_cli_ask_kni_chief_of_staff_uses_chief_work_item(tmp_path: Path, capsys) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask-chief.db'}"

    exit_code = main(
        [
            "ask",
            "--database-url",
            database_url,
            "@KNI",
            "chief",
            "of",
            "staff",
            "summarize",
            "open",
            "Slack",
            "follow-ups",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "WorkItem:" in output
    assert "Route: chief_of_staff" in output
    assert "Artifacts: chief_of_staff_plan:" in output


def test_cli_ask_kni_chief_natural_research_outreach_uses_graph_workflow(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask-chief-research-outreach.db'}"
    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)

    exit_code = main(
        [
            "ask",
            "--json",
            "--no-live-sdk",
            "--database-url",
            database_url,
            "--max-manager-steps",
            "4",
            "@KNI",
            "chief",
            "of",
            "staff",
            "NeuroFlow",
            "has",
            "been",
            "coming",
            "up",
            "as",
            "a",
            "behavioral-health",
            "AI",
            "company",
            "with",
            "payer",
            "partnership",
            "and",
            "outcomes-evidence",
            "signals.",
            "Do",
            "research,",
            "assess",
            "whether",
            "this",
            "is",
            "a",
            "real",
            "KNI",
            "advisory/research",
            "opportunity,",
            "identify",
            "what",
            "source-backed",
            "evidence",
            "is",
            "still",
            "missing,",
            "and",
            "decide",
            "whether",
            "it",
            "should",
            "stop",
            "at",
            "an",
            "approval",
            "checkpoint",
            "before",
            "any",
            "outreach.",
            "If",
            "the",
            "evidence",
            "supports",
            "pursuing",
            "it,",
            "include",
            "a",
            "draft-only",
            "Slack-thread",
            "sample",
            "outreach",
            "for",
            "review.",
            "Do",
            "not",
            "send",
            "email,",
            "create",
            "Gmail",
            "drafts,",
            "post",
            "outside",
            "this",
            "thread,",
            "schedule,",
            "publish,",
            "or",
            "write",
            "external",
            "systems.",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    artifact_types = {
        artifact["artifact_type"] for artifact in payload["work_item"]["artifact_refs"]
    }
    blocker_codes = {blocker["code"] for blocker in payload["blockers"]}
    events = SQLiteStore(database_url).list_work_item_events(payload["work_item"]["id"])
    graph_event = next(event for event in events if event.event_type == "langgraph_orchestration")

    assert payload["route"] == "outreach_composer"
    assert payload["status"] == "blocked"
    assert {"company_profile", "opportunity"} <= artifact_types
    assert "outreach_draft" not in artifact_types
    assert "outreach_draft_needs_model_reasoning" in blocker_codes
    assert graph_event.metadata["checkpoint_required"] is False
    assert "run_business_research" in graph_event.metadata["node_path"]
    assert "run_opportunity_scout" in graph_event.metadata["node_path"]
    assert "run_outreach_composer" in graph_event.metadata["node_path"]


def test_cli_ask_chief_of_staff_reference_capture_persists_memory(
    tmp_path: Path,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'keystone.db'}"
    url = "https://braininitiative.nih.gov/news-events/blog/register-now-nih-brain-neuroai-workshop"

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "--database-url",
            database_url,
            "keep this for future reference:",
            "NIH AI conference with virtual attendees:",
            url,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "chief_of_staff"
    assert payload["output"]["recommended_route"]["workflow_type"] == "reference-capture"
    assert payload["output"]["summary"].startswith("Saved reference for future use:")

    memories = SQLiteStore(database_url).retrieve_memory(
        "NIH AI conference",
        memory_types=["operator_reference"],
    )
    assert len(memories) == 1
    assert memories[0].content["url"] == url


def test_cli_ask_agent_override_auto_live_sdk_in_live_mode(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        assert live_manual_plan is True
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "ResearchBrief",
                    "send_enabled": False,
                    "output": {"summary": "live brief"},
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(["ask", "--agent", "business_research_analyst", "research", "Lindus"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Agent: Business Research Analyst" in output
    assert "Output type: ResearchBrief" in output
    assert "Orchestrator review:" in output
    assert calls
    assert "scripts/run_company_research.py" in calls[0]
    assert "--live-sdk" in calls[0]
    assert "--live-search-plan" in calls[0]


def test_cli_live_finance_receipt_write_skips_live_manual_planner(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        captured["live_manual_plan"] = live_manual_plan
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "ChiefOfStaffResult",
                    "send_enabled": False,
                    "human_summary": "blocked before model/tool call",
                    "output": {
                        "summary": "blocked before model/tool call",
                        "send_enabled": False,
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--live-sdk",
            "--json",
            "@KNI",
            "chief",
            "of",
            "staff",
            "add",
            "a",
            "business",
            "expense",
            "to",
            "the",
            "airtable",
            "business",
            "expenses",
            "based",
            "on",
            "the",
            "receipt",
            "details",
            "which",
            "are:",
            "/tmp/example-business-cards-receipt.pdf",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert captured["live_manual_plan"] is False
    assert "scripts/run_chief_of_staff.py" in captured["command"]
    assert "--live-sdk" in captured["command"]
    assert payload["manual_request_plan"]["intent"] == "business_system_write"
    assert payload["manual_request_plan"]["target_type"] == "business_system_context"


def test_cli_bounded_smoke_suppresses_live_manual_planner_and_live_search(
    monkeypatch,
    capsys,
) -> None:
    prompt = (
        "Research smoke: research NeuroFlow for a short internal read-only company note. "
        "Stay on Business Research only; do not scout opportunities or draft outreach. "
        "Live SDK is approved only for this bounded read-only smoke if the backend would "
        "normally use it; live web search is not approved. Use local/dry-run retrieval "
        "where possible. Do not send email, create drafts, post elsewhere, publish, "
        "schedule, or write external systems."
    )
    captured: dict[str, object] = {}

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        captured["live_manual_plan"] = live_manual_plan
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_advance_work_item_manager_loop(request, **_kwargs):
        captured["request"] = request
        item = WorkItem(
            kind=WorkItemKind.COMPANY_RESEARCH,
            title="Research: NeuroFlow",
            request_text=request.request_text,
            current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            status=WorkItemStatus.DONE,
        )
        return WorkflowRunResult(
            work_item=item,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            status=WorkItemStatus.DONE,
            advanced=True,
            human_summary="Business Research completed.",
            manual_request_plan=request.manual_request_plan,
            orchestrator_preflight=request.orchestrator_preflight,
        )

    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(
        "keystone_agents.langgraph_workflow.advance_work_item_manager_loop_with_optional_langgraph",
        fake_advance_work_item_manager_loop,
    )

    exit_code = main(["ask", "--live-search", "--live-sdk", "--json", prompt])

    payload = json.loads(capsys.readouterr().out)
    request = captured["request"]
    assert exit_code == 0
    assert captured["live_manual_plan"] is False
    assert request.live_search is False
    assert request.live_sdk is True
    assert request.manual_request_plan["target_agent"] == "business_research_analyst"
    assert request.manual_request_plan["requires_live_search"] is False
    assert payload["route"] == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value


def test_cli_ask_agent_override_promotes_child_human_summary(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    summary = (
        "*Answer:*\nCorti has a clean source-backed fit summary.\n\n"
        "*Detailed Summary:*\n* Product/workflow: clinical AI.\n\n"
        "*Useful references:*\n* Corti: https://www.corti.ai"
    )

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        assert live_manual_plan is True
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run(_command, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "CompanyResearchFocusedBrief",
                    "send_enabled": False,
                    "human_summary": summary,
                    "output": {"company_name": "Corti", "send_enabled": False},
                    "model": {"provider": "openai", "name": "gpt-5.4-mini"},
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "business_research_analyst",
            "--json",
            "--database-url",
            f"sqlite:///{tmp_path / 'runs.db'}",
            "research",
            "Corti",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["human_summary"] == summary
    assert payload["slack_display_text"] == summary
    assert payload["display_text"] == summary
    assert payload["summary"] == summary
    assert payload["script_payload"]["human_summary"] == summary
    assert payload["output_type"] == "CompanyResearchFocusedBrief"


def test_cli_live_payload_text_mode_prefers_human_summary_over_message(
    capsys,
) -> None:
    summary = (
        "*Answer:*\nSuki has a clean answer-first fit check.\n\n"
        "*Detailed Summary:*\nDiagnostics should not render before this text."
    )

    assert (
        cli._print_ask_live_payload(
            {
                "agent_name": "Business Research Analyst",
                "output_type": "CompanyResearchFocusedBrief",
                "send_enabled": False,
                "message": "Business Agents Company Research Brief Ready",
                "human_summary": summary,
            },
            json_output=False,
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Business Agents Company Research Brief Ready" not in output
    assert summary in output
    if "Orchestrator review:" in output:
        assert output.index("*Answer:*") < output.index("Orchestrator review:")


def test_cli_live_outreach_inline_context_uses_work_item_runner(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    database_url = f"sqlite:///{tmp_path / 'outreach-inline.db'}"

    def fake_run_ask_work_item(input_text, **kwargs):
        captured["input_text"] = input_text
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_run_ask_work_item", fake_run_ask_work_item)

    exit_code = cli._run_ask_outreach_composer_live(
        "outreach composer agent: diagnostic case diag_outreach flexible labels. "
        "Prepare a draft-only email paragraph. Target contact: Alex Rivera at "
        "Example Health. Approved evidence: Example Health asked whether Keystone "
        "could review its remote patient monitoring AI validation workflow. "
        "Do not send email or create a Gmail draft.",
        json_output=True,
        manual_plan=None,
        orchestrator_preflight=None,
        sdk_session_spec=None,
        cost_tracking_requested=True,
        database_url=database_url,
    )

    assert exit_code == 0
    assert captured["database_url"] == database_url
    assert captured["live_sdk"] is True
    assert captured["live_search"] is False
    assert captured["cost_tracking_requested"] is True
    assert "Approved evidence" in str(captured["input_text"])


def test_cli_live_opportunity_scout_no_external_context_uses_work_item_runner(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    database_url = f"sqlite:///{tmp_path / 'opportunity-inline.db'}"

    def fake_run_ask_work_item(input_text, **kwargs):
        captured["input_text"] = input_text
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_run_ask_work_item", fake_run_ask_work_item)

    exit_code = cli._run_ask_opportunity_scout_live(
        "opportunity scout agent: diagnostic case diag_opp. Use only this sanitized "
        "inline context and do not research externally: Cedar Grove Pediatrics is "
        "considering whether Keystone could review a measurement dashboard before "
        "an internal pilot. Scout two practical opportunity directions.",
        json_output=True,
        manual_plan=None,
        orchestrator_preflight=None,
        sdk_session_spec=None,
        cost_tracking_requested=True,
        database_url=database_url,
    )

    assert exit_code == 0
    assert captured["database_url"] == database_url
    assert captured["live_search"] is False
    assert captured["live_sdk"] is True
    assert captured["cost_tracking_requested"] is True
    assert "Cedar Grove Pediatrics" in str(captured["input_text"])


def test_cli_live_business_research_no_external_context_uses_work_item_runner(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    database_url = f"sqlite:///{tmp_path / 'business-research-inline.db'}"

    def fake_run_ask_work_item(input_text, **kwargs):
        captured["input_text"] = input_text
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_run_ask_work_item", fake_run_ask_work_item)

    exit_code = cli._run_ask_company_research_live(
        "business research analyst agent: diagnostic case diag_bra. Use only this "
        "sanitized inline context and do not research externally: Northstar Sleep Lab "
        "is considering whether Keystone could review an internal sleep-study "
        "operations dashboard before a November leadership review. Return a concise "
        "internal research handoff.",
        json_output=True,
        manual_plan=None,
        orchestrator_preflight=None,
        sdk_session_spec=None,
        cost_tracking_requested=True,
        database_url=database_url,
    )

    assert exit_code == 0
    assert captured["database_url"] == database_url
    assert captured["live_search"] is False
    assert captured["live_sdk"] is True
    assert captured["cost_tracking_requested"] is True
    assert "Northstar Sleep Lab" in str(captured["input_text"])


def test_cli_ask_context_agent_override_live_sdk_runs_typed_agent(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    database_url = f"sqlite:///{tmp_path / 'context-agent.db'}"

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        assert requested_agent == "airtable_context_agent"
        assert live_manual_plan is True
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run_typed_sdk_sync(agent, prompt, output_type, **kwargs):
        calls.append(
            {
                "agent_name": agent.name,
                "prompt": prompt,
                "output_type": output_type.__name__,
                "live": kwargs.get("live"),
                "live_reads_env": os.environ.get(cli.AIRTABLE_LIVE_READS_ENV),
                "operator_approval": os.environ.get(
                    "KEYSTONE_AIRTABLE_OPERATOR_APPROVAL_REFERENCE"
                ),
                "max_turns": kwargs.get("max_turns"),
            }
        )
        return (
            SimpleNamespace(
                final_output=None,
                usage=None,
                context_wrapper=SimpleNamespace(
                    usage=SimpleNamespace(
                        requests=2,
                        input_tokens=1000,
                        output_tokens=100,
                        total_tokens=1100,
                        input_tokens_details=SimpleNamespace(cached_tokens=200),
                        output_tokens_details=SimpleNamespace(reasoning_tokens=20),
                    )
                ),
                new_items=[],
            ),
            cli.AirtableContextResult(
                mode="llm",
                summary="Schema available for Finance & Tax Tracker.",
                base_alias="finance_tax_tracker",
                relevant_tables=["Business Income"],
                relevant_fields=["Date", "Amount", "Client"],
                recommended_actions=["Read at most one Business Income record."],
            ),
        )

    monkeypatch.delenv(cli.AIRTABLE_LIVE_READS_ENV, raising=False)
    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_typed_sdk_sync", fake_run_typed_sdk_sync)

    exit_code = main(
        [
            "ask",
            "--agent",
            "airtable_context_agent",
            "--live-sdk",
            "--database-url",
            database_url,
            "--json",
            "Read-only context test for the 2026 Finance & Tax Tracker.",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "live_sdk"
    assert payload["selected_agent"] == "airtable_context_agent"
    assert payload["output_type"] == "AirtableContextResult"
    assert payload["output"]["relevant_tables"] == ["Business Income"]
    assert payload["send_enabled"] is False
    assert payload["model_execution"] == {
        "provider": "openai",
        "model": "gpt-5.4-mini",
        "run_mode": "live_sdk",
        "usage_available": True,
        "cost_available": True,
        "base_url_configured": False,
        "gateway_mode": False,
        "sdk_turn_policy": {
            "agent_name": "airtable_context_agent",
            "max_turns": 6,
            "source": "fixed_default",
        },
    }
    assert payload["usage"] == {
        "available": True,
        "requests": 2,
        "input_tokens": 1000,
        "output_tokens": 100,
        "total_tokens": 1100,
        "cached_input_tokens": 200,
        "reasoning_output_tokens": 20,
        "cache_hit_rate": 0.2,
            "prompt_cache_key_present": False,
            "prompt_cache_key_hash": "",
    }
    assert payload["cost"]["estimated_usd"] > 0
    assert isinstance(payload["agent_run_id"], int)
    assert calls == [
        {
            "agent_name": "airtable_context_agent",
            "prompt": "Read-only context test for the 2026 Finance & Tax Tracker.",
            "output_type": "AirtableContextResult",
            "live": True,
            "live_reads_env": "true",
            "operator_approval": None,
            "max_turns": 6,
        }
    ]
    assert os.environ.get(cli.AIRTABLE_LIVE_READS_ENV) is None
    assert os.environ.get("KEYSTONE_AIRTABLE_OPERATOR_APPROVAL_REFERENCE") is None
    records = SQLiteStore(database_url).fetch_all("agent_runs")
    assert len(records) == 1
    assert records[0]["agent_name"] == "airtable_context_agent"
    assert records[0]["dry_run"] == 0
    assert records[0]["model"] == "sdk-live:gpt-5.4-mini"


def test_marked_airtable_lifecycle_receives_process_local_operator_approval(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}
    request = (
        "Using Airtable context, create one marked KBA test expense in the Business "
        "Expenses table, verify it, update the same record description, verify it "
        "again, and remove only that test record."
    )

    def fake_run_typed_sdk_sync(_agent, _prompt, _output_type, **_kwargs):
        captured["approval"] = os.environ.get(
            "KEYSTONE_AIRTABLE_OPERATOR_APPROVAL_REFERENCE"
        )
        return (
            SimpleNamespace(final_output=None, usage=None, new_items=[]),
            cli.AirtableContextResult(
                mode="llm",
                summary="Prepared the bounded marked-record lifecycle.",
                base_alias="finance_tax_tracker",
                relevant_tables=["Business Expenses"],
            ),
        )

    monkeypatch.delenv(
        "KEYSTONE_AIRTABLE_OPERATOR_APPROVAL_REFERENCE",
        raising=False,
    )
    monkeypatch.setattr(cli, "run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(cli.SQLiteStore, "save_agent_run", lambda *_args, **_kwargs: 1)

    exit_code = cli._run_ask_context_agent_live(
        "airtable_context_agent",
        request,
        json_output=True,
    )

    assert exit_code == 0
    json.loads(capsys.readouterr().out)
    assert str(captured["approval"]).startswith("airtable-direct:")
    assert os.environ.get("KEYSTONE_AIRTABLE_OPERATOR_APPROVAL_REFERENCE") is None


def test_marked_zotero_lifecycle_receives_process_local_operator_approval(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}
    request = (
        "Create one marked standalone Zotero test note, verify it, revise the same "
        "note to be clearer, verify it again, and remove only that test note."
    )
    manual_plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    def fake_run_typed_sdk_sync(_agent, prompt, _output_type, **_kwargs):
        captured["approval"] = os.environ.get(
            "KEYSTONE_ZOTERO_OPERATOR_APPROVAL_REFERENCE"
        )
        captured["prompt"] = prompt
        return (
            SimpleNamespace(final_output=None, usage=None, new_items=[]),
            ZoteroContextResult(summary="Prepared the bounded marked-note lifecycle."),
        )

    monkeypatch.delenv(
        "KEYSTONE_ZOTERO_OPERATOR_APPROVAL_REFERENCE",
        raising=False,
    )
    monkeypatch.setattr(cli, "run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(cli.SQLiteStore, "save_agent_run", lambda *_args, **_kwargs: 1)

    exit_code = cli._run_ask_context_agent_live(
        "zotero_context_agent",
        request,
        json_output=True,
        manual_plan=manual_plan,
    )

    assert exit_code == 0
    json.loads(capsys.readouterr().out)
    assert str(captured["approval"]).startswith("zotero-direct:")
    assert str(captured["prompt"]).startswith(request)
    assert "Call the matching typed tool with live=true" in str(captured["prompt"])
    assert os.environ.get("KEYSTONE_ZOTERO_OPERATOR_APPROVAL_REFERENCE") is None


def test_context_agent_tool_receipts_are_bounded_and_report_verified_writes() -> None:
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_output_item",
                output=json.dumps(
                    {
                        "status": "success",
                        "operation": "update",
                        "table": "Business Expenses",
                        "record_id": "recSynthetic",
                        "approval_reference": "approval:test:update",
                        "record": {"private_field": "must not enter receipt"},
                        "verification": {
                            "status": "verified",
                            "passed": True,
                            "record_id_match": True,
                            "matched_fields": ["Description"],
                            "mismatched_fields": [],
                        },
                        "send_enabled": False,
                    }
                ),
            )
        ]
    )

    receipts = cli._context_agent_tool_receipts(raw_result)

    assert receipts == [
        {
            "status": "success",
            "operation": "update",
            "table": "Business Expenses",
            "record_id": "recSynthetic",
            "approval_reference": "approval:test:update",
            "send_enabled": False,
            "verification": {
                "status": "verified",
                "passed": True,
                "record_id_match": True,
                "matched_fields": ["Description"],
                "mismatched_fields": [],
            },
        }
    ]


def test_context_agent_blocked_read_is_not_reported_as_blocked_write() -> None:
    receipts = [
        {"status": "blocked", "table": "Business Expenses", "send_enabled": False},
        {
            "status": "success",
            "operation": "create",
            "table": "Business Expenses",
            "approval_reference": "approval:test:create",
            "verification": {"status": "verified", "passed": True},
            "send_enabled": False,
        },
    ]

    assert cli._context_agent_blocked_write_attempts(
        {"agent_name": "airtable_context_agent"},
        tool_receipts=receipts,
    ) == []
    assert cli._context_agent_external_write_performed(receipts) is True
    assert "private_field" not in json.dumps(receipts)


def test_context_agent_lifecycle_receipt_keeps_bounded_verification() -> None:
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_output_item",
                output=json.dumps(
                    {
                        "status": "success",
                        "operation": "test_record_lifecycle",
                        "table": "Business Expenses",
                        "record_id": "recInternalOnly",
                        "approval_reference": "airtable-direct:requesthash",
                        "required_marker": "KBA_TEST_RECORD",
                        "verification": {
                            "passed": True,
                            "create_read_back": True,
                            "same_record_update_read_back": True,
                            "record_absent_after_cleanup": True,
                        },
                        "create": {"record": {"private": "discard"}},
                        "send_enabled": False,
                    }
                ),
            )
        ]
    )

    receipts = cli._context_agent_tool_receipts(raw_result)

    assert receipts[0]["operation"] == "test_record_lifecycle"
    assert receipts[0]["verification"] == {
        "passed": True,
        "create_read_back": True,
        "same_record_update_read_back": True,
        "record_absent_after_cleanup": True,
    }
    assert "create" not in receipts[0]
    assert cli._context_agent_external_write_performed(receipts) is True


def test_airtable_lifecycle_public_payload_removes_provider_identity() -> None:
    provider_id = "recProviderInternal123"
    output = {
        "base_id": "appProviderInternal123",
        "candidate_record_ids": [provider_id],
        "recommended_record_identity": f"Airtable record {provider_id} was removed.",
        "record_summaries": [
            {
                "key": provider_id,
                "value": "Lifecycle passed.",
                "note": f"Updated {provider_id} before cleanup.",
            }
        ],
        "executed_write_results": [
            {"key": "create", "value": "success", "note": f"Created {provider_id}."}
        ],
        "write_plan": {
            "field_mapping": [
                {"key": "required_marker", "value": "KBA_TEST_RECORD"},
                {"key": "record_id", "value": provider_id},
            ]
        },
    }
    receipts = [
        {
            "status": "success",
            "operation": "test_record_lifecycle",
            "record_id": provider_id,
            "verification": {"passed": True},
        }
    ]

    public_output, public_receipts = cli._context_agent_public_payload(
        "airtable_context_agent",
        output,
        receipts,
    )

    rendered = json.dumps({"output": public_output, "receipts": public_receipts})
    assert provider_id not in rendered
    assert "appProviderInternal123" not in rendered
    assert public_output["candidate_record_ids"] == []
    assert public_output["base_id"] == ""
    assert public_output["write_plan"]["field_mapping"] == [
        {"key": "required_marker", "value": "KBA_TEST_RECORD"}
    ]
    assert "record_id" not in public_receipts[0]


def test_zotero_lifecycle_public_payload_removes_provider_identity() -> None:
    provider_id = "NOTEINTERNAL"
    output = {
        "zotero_item_keys": [provider_id],
        "relevant_evidence": [f"Updated {provider_id} and verified it."],
        "executed_note_results": [
            {"key": provider_id, "value": "success", "note": "Cleanup passed."}
        ],
    }
    receipts = [
        {
            "status": "success",
            "operation": "test_note_lifecycle",
            "item_key": provider_id,
            "verification": {"passed": True},
        }
    ]

    public_output, public_receipts = cli._context_agent_public_payload(
        "zotero_context_agent",
        output,
        receipts,
    )

    rendered = json.dumps({"output": public_output, "receipts": public_receipts})
    assert provider_id not in rendered
    assert public_output["zotero_item_keys"] == []
    assert "the marked test note" in rendered
    assert "item_key" not in public_receipts[0]


def test_context_agent_tool_receipts_report_zotero_delete_and_absence() -> None:
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_output_item",
                output=json.dumps(
                    {
                        "status": "success",
                        "operation": "delete_test_note",
                        "item_key": "NOTEKBA1",
                        "approval_reference": "approval:test:delete",
                        "before": {"note_sha256": "private-content-hash"},
                        "verification": {
                            "status": "verified",
                            "passed": True,
                            "item_absent_after": True,
                        },
                        "send_enabled": False,
                    }
                ),
            )
        ]
    )

    receipts = cli._context_agent_tool_receipts(raw_result)

    assert receipts == [
        {
            "status": "success",
            "operation": "delete_test_note",
            "item_key": "NOTEKBA1",
            "approval_reference": "approval:test:delete",
            "send_enabled": False,
            "verification": {
                "status": "verified",
                "passed": True,
                "item_absent_after": True,
            },
        }
    ]
    assert cli._context_agent_external_write_performed(receipts) is True
    assert "private-content-hash" not in json.dumps(receipts)


def test_context_agent_tool_receipts_classify_workspace_sheet_lifecycle() -> None:
    outputs = [
        {
            "status": "success",
            "operation": "create_sheet",
            "spreadsheet_id": "sheetKBA1",
            "title": "KBA_TEST_SHEET sdk-live",
            "folder_path": "KNIOps",
            "approval_reference": "approval:create",
            "verification": {
                "status": "verified",
                "passed": True,
                "spreadsheet_id_match": True,
                "title_match": True,
                "mime_type_match": True,
                "trashed": False,
                "trashed_match": True,
            },
            "send_enabled": False,
        },
        {
            "status": "success",
            "operation": "append_rows",
            "spreadsheet_id": "sheetKBA1",
            "sheet_name": "Validation",
            "row_count": 1,
            "updated_range": "Validation!A2:C2",
            "approval_reference": "approval:append",
            "verification": {
                "status": "verified",
                "passed": True,
                "row_count_match": True,
                "values_match": True,
                "verified_row_count": 1,
            },
            "rows": [["private", "row", "content"]],
            "send_enabled": False,
        },
        {
            "status": "success",
            "operation": "update_row",
            "spreadsheet_id": "sheetKBA1",
            "sheet_name": "Validation",
            "row_number": 2,
            "key_column": "record_key",
            "key_value": "KBA_TEST_ROW_1",
            "approval_reference": "approval:update",
            "verification": {
                "status": "verified",
                "passed": True,
                "row_found": True,
                "row_number": 2,
                "matched_fields": ["note", "status"],
                "mismatched_fields": [],
            },
            "send_enabled": False,
        },
        {
            "status": "success",
            "operation": "trash_sheet",
            "spreadsheet_id": "sheetKBA1",
            "trashed": True,
            "approval_reference": "approval:trash",
            "verification": {
                "status": "verified",
                "passed": True,
                "spreadsheet_id_match": True,
                "title_match": True,
                "mime_type_match": True,
                "trashed": True,
                "trashed_match": True,
            },
            "send_enabled": False,
        },
    ]
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(type="tool_call_output_item", output=json.dumps(output))
            for output in outputs
        ]
    )

    receipts = cli._context_agent_tool_receipts(raw_result)

    assert [receipt["operation"] for receipt in receipts] == [
        "create_sheet",
        "append_rows",
        "update_row",
        "trash_sheet",
    ]
    assert all(receipt["spreadsheet_id"] == "sheetKBA1" for receipt in receipts)
    assert receipts[1]["row_count"] == 1
    assert receipts[1]["verification"]["values_match"] is True
    assert receipts[2]["key_value"] == "KBA_TEST_ROW_1"
    assert receipts[2]["verification"]["matched_fields"] == ["note", "status"]
    assert receipts[3]["trashed"] is True
    assert receipts[3]["verification"]["trashed_match"] is True
    assert cli._context_agent_external_write_performed(receipts) is True
    assert "private" not in json.dumps(receipts)


def test_context_agent_verified_write_reconciles_direct_write_plan() -> None:
    output_payload: dict[str, object] = {
        "write_plan": {
            "approval_required": True,
            "approval_reference_needed": True,
            "live_write_allowed_for_specialist": False,
        }
    }
    receipts = [
        {
            "status": "success",
            "operation": "create_sheet",
            "verification": {"status": "verified", "passed": True},
        }
    ]

    cli._reconcile_context_agent_executed_write_plan(output_payload, receipts)

    write_plan = output_payload["write_plan"]
    assert isinstance(write_plan, dict)
    assert write_plan["approval_required"] is True
    assert write_plan["approval_reference_needed"] is False
    assert write_plan["live_write_allowed_for_specialist"] is True


def test_context_agent_slide_copy_receipts_preserve_provenance_and_classify_write() -> None:
    outputs = [
        {
            "status": "success",
            "operation": "extract_slide_copy",
            "slide_number": 2,
            "output_format": "png",
            "artifact_path": "artifacts/presentation-derived/KBA_TEST_SLIDE-2.png",
            "artifact_size": 1234,
            "artifact_sha256": "a" * 64,
            "parent_content_sha256": "b" * 64,
            "parent_modified": False,
            "derived_copy_created": True,
            "approval_reference": "approved:slide-copy",
            "verification": {
                "status": "verified",
                "passed": True,
                "artifact_exists": True,
                "file_signature_valid": True,
                "parent_hash_match": True,
                "parent_mtime_match": True,
            },
            "send_enabled": False,
        }
    ]

    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(type="tool_call_output_item", output=json.dumps(outputs[0]))
        ]
    )
    receipts = cli._context_agent_tool_receipts(raw_result)

    assert receipts[0]["slide_number"] == 2
    assert receipts[0]["artifact_path"].endswith("KBA_TEST_SLIDE-2.png")
    assert receipts[0]["verification"]["parent_hash_match"] is True
    assert cli._context_agent_external_write_performed(receipts) is True


def test_context_agent_unverified_write_does_not_relax_write_plan() -> None:
    output_payload: dict[str, object] = {
        "write_plan": {
            "approval_reference_needed": True,
            "live_write_allowed_for_specialist": False,
        }
    }
    receipts = [
        {
            "status": "success",
            "operation": "create_sheet",
            "verification": {"status": "verification_failed", "passed": False},
        }
    ]

    cli._reconcile_context_agent_executed_write_plan(output_payload, receipts)

    write_plan = output_payload["write_plan"]
    assert isinstance(write_plan, dict)
    assert write_plan["approval_reference_needed"] is True
    assert write_plan["live_write_allowed_for_specialist"] is False


def test_context_agent_workspace_read_receipts_keep_identity_without_content() -> None:
    outputs = [
        {
            "status": "success",
            "operation": "search_files",
            "query": "proposal",
            "mime_type": "application/vnd.google-apps.document",
            "folder_path": "KNIOps",
            "item_count": 1,
            "items": [{"id": "private-file-id", "name": "private file name"}],
            "send_enabled": False,
        },
        {
            "status": "success",
            "operation": "read_doc",
            "document_id": "docKBA1",
            "title": "Selected internal document",
            "char_count": 412,
            "truncated": False,
            "text": "private document body must not enter the receipt",
            "send_enabled": False,
        },
    ]
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(type="tool_call_output_item", output=json.dumps(output))
            for output in outputs
        ]
    )

    receipts = cli._context_agent_tool_receipts(raw_result)

    assert receipts == [
        {
            "status": "success",
            "operation": "search_files",
            "folder_path": "KNIOps",
            "query": "proposal",
            "mime_type": "application/vnd.google-apps.document",
            "item_count": 1,
            "send_enabled": False,
        },
        {
            "status": "success",
            "operation": "read_doc",
            "document_id": "docKBA1",
            "title": "Selected internal document",
            "char_count": 412,
            "truncated": False,
            "send_enabled": False,
        },
    ]
    assert "private-file-id" not in json.dumps(receipts)
    assert "private document body" not in json.dumps(receipts)


@pytest.mark.parametrize(
    ("agent_text", "expected_route", "expected_agent_name", "expected_output_type"),
    [
        ("rss context agent", "rss_context_agent", "rss_context_agent", "RssContextResult"),
        (
            "preprints context agent",
            "preprints_context_agent",
            "preprints_context_agent",
            "PreprintsContextResult",
        ),
    ],
)
def test_cli_ask_feed_context_mentions_use_context_dry_run(
    capsys,
    tmp_path: Path,
    agent_text: str,
    expected_route: str,
    expected_agent_name: str,
    expected_output_type: str,
) -> None:
    exit_code = main(
        [
            "ask",
            "--no-live-sdk",
            "--database-url",
            f"sqlite:///{tmp_path / 'feed-context.db'}",
            "--json",
            "--input",
            (
                f"@KNI {agent_text}: diagnostic case feed context. "
                "Use available feed context, not browser automation or live web search. "
                "Return a concise Answer and Detailed Summary."
            ),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry_run"
    assert payload["status"] == "done"
    assert payload["route"] == expected_route
    assert payload["selected_agent"] == expected_route
    assert payload["output_type"] == expected_output_type
    assert payload["output"]["agent_name"] == expected_agent_name
    assert payload["human_summary"].startswith("*Answer:*\n")
    assert "\n\n*Detailed Summary:*\n" in payload["human_summary"]
    assert "work_item" not in payload


@pytest.mark.parametrize(
    ("agent_text", "expected_route", "expected_agent_name", "expected_output_type"),
    [
        ("rss context agent", "rss_context_agent", "rss_context_agent", "RssContextResult"),
        (
            "business agents rss context agent",
            "rss_context_agent",
            "rss_context_agent",
            "RssContextResult",
        ),
        (
            "preprints context agent",
            "preprints_context_agent",
            "preprints_context_agent",
            "PreprintsContextResult",
        ),
        (
            "business agents preprints context agent",
            "preprints_context_agent",
            "preprints_context_agent",
            "PreprintsContextResult",
        ),
    ],
)
def test_cli_ask_bare_feed_context_aliases_use_context_dry_run(
    capsys,
    tmp_path: Path,
    agent_text: str,
    expected_route: str,
    expected_agent_name: str,
    expected_output_type: str,
) -> None:
    exit_code = main(
        [
            "ask",
            "--no-live-sdk",
            "--database-url",
            f"sqlite:///{tmp_path / 'bare-feed-context.db'}",
            "--json",
            "--input",
            (
                f"{agent_text}: diagnostic case feed context. "
                "Use available feed context, not browser automation or live web search. "
                "Return a concise Answer and Detailed Summary."
            ),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry_run"
    assert payload["status"] == "done"
    assert payload["route"] == expected_route
    assert payload["selected_agent"] == expected_route
    assert payload["output_type"] == expected_output_type
    assert payload["output"]["agent_name"] == expected_agent_name
    assert payload["human_summary"].startswith("*Answer:*\n")
    assert "\n\n*Detailed Summary:*\n" in payload["human_summary"]


def test_cli_ask_feed_context_text_mode_prints_human_summary(capsys, tmp_path: Path) -> None:
    exit_code = main(
        [
            "ask",
            "--no-live-sdk",
            "--database-url",
            f"sqlite:///{tmp_path / 'feed-context-text.db'}",
            "--input",
            (
                "@KNI preprints context agent: diagnostic case feed context. "
                "Use local dry-run preprints context, not external research. "
                "Look for adolescent depression digital phenotyping or wearable-sensor monitoring."
            ),
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Agent: Preprints Context Agent" in output
    assert "*Answer:*" in output
    assert "*Detailed Summary:*" in output
    assert "Dry-run selected the specialist" in output


def test_feed_context_topic_terms_ignore_instruction_words() -> None:
    terms = cli._feed_context_topic_terms(
        "diagnostic case diag_preprints. Use only local/dry-run preprints context. "
        "Do not research externally. Look for recent preprint context relevant to "
        "adolescent depression digital phenotyping or wearable-sensor monitoring. "
        "Do not create, update, export, write files, send, schedule, publish, create "
        "CRM records, or post elsewhere."
    )

    assert "externally" not in terms
    assert "recent" not in terms
    assert "dry-run" not in terms
    assert "export" not in terms
    assert "research" not in terms
    assert "relevant" not in terms
    assert "concise" not in terms
    assert "human-useful" not in terms
    assert "handoff" not in terms
    assert "whether" not in terms
    assert "adolescent depression" in terms
    assert "digital phenotyping" in terms
    assert "wearable-sensor monitoring" in terms
    assert terms[:3] == [
        "adolescent depression",
        "digital phenotyping",
        "wearable-sensor monitoring",
    ]


def test_feed_context_topic_terms_prioritize_rss_domain_phrases() -> None:
    terms = cli._feed_context_topic_terms(
        "diagnostic case diag_rss. Use only local/dry-run announcements or RSS context. "
        "Do not research externally. Look for recent announcement context relevant to "
        "clinical AI validation, remote monitoring implementation, and operations "
        "dashboard governance. Return a concise human-useful context handoff."
    )

    assert terms[:3] == [
        "clinical AI validation",
        "remote monitoring implementation",
        "operations dashboard governance",
    ]
    assert "announcements" not in terms
    assert "implementation" not in terms
    assert "remote monitoring" not in terms
    assert "externally" not in terms


def test_cli_google_workspace_context_live_sdk_enables_live_read_default(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    database_url = f"sqlite:///{tmp_path / 'google-context-agent.db'}"

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        assert requested_agent == "google_workspace_context_agent"
        assert live_manual_plan is True
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run_typed_sdk_sync(agent, prompt, output_type, **kwargs):
        captured["agent_name"] = agent.name
        captured["live_reads_env"] = os.environ.get(cli.GOOGLE_WORKSPACE_LIVE_READS_ENV)
        return (
            SimpleNamespace(final_output=None, usage=None),
            cli.GoogleWorkspaceContextResult(
                mode="llm",
                summary="KNIOps Drive context was read with live read defaults.",
                relevant_folders=["KNIOps"],
                relevant_docs=["Operations Doc"],
                relevant_sheets=["KNIOps Structured Data"],
                recommended_target="KNIOps",
                recommended_actions=["Hand context to Chief of Staff."],
            ),
        )

    monkeypatch.delenv(cli.GOOGLE_WORKSPACE_LIVE_READS_ENV, raising=False)
    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_typed_sdk_sync", fake_run_typed_sdk_sync)

    exit_code = main(
        [
            "ask",
            "--agent",
            "google_workspace_context_agent",
            "--live-sdk",
            "--database-url",
            database_url,
            "--json",
            "Read-only Google Workspace context test for KNIOps.",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert captured == {
        "agent_name": "google_workspace_context_agent",
        "live_reads_env": "true",
    }
    assert os.environ.get(cli.GOOGLE_WORKSPACE_LIVE_READS_ENV) is None
    assert payload["selected_agent"] == "google_workspace_context_agent"
    assert payload["output_type"] == "GoogleWorkspaceContextResult"


def test_context_agent_human_summary_separates_answer_from_details() -> None:
    summary = cli._context_agent_human_summary(
        {
            "summary": "KNIOps Drive is accessible in read-only mode.",
            "approval_needs": ["Live writes require explicit approval reference"],
            "blockers": ["No Google Sheets were returned in the current folder listing"],
        }
    )

    assert summary == (
        "*Answer:*\n"
        "KNIOps Drive is accessible in read-only mode.\n\n"
        "*Detailed Summary:*\n"
        "- Approval/write boundary: Live writes require explicit approval reference.\n"
        "- Needs attention: No Google Sheets were returned in the current folder listing."
    )


def test_google_workspace_context_dry_run_tracks_onboarding_topic_without_eval_boilerplate() -> None:
    output = cli._context_agent_dry_run_output(
        "google_workspace_context_agent",
        (
            "@KNI google workspace context agent: Use only Google Workspace/Drive context "
            "if available, read-only. I am looking for whether KNIOps has internal "
            "onboarding or operations SOP docs for a new collaborator handoff. "
            "Do not create, edit, share, export, or write."
        ),
        None,
    )

    assert output is not None
    payload = output.model_dump(mode="json")
    summary = cli._context_agent_human_summary(payload)

    assert "internal onboarding" in payload["summary"]
    assert "operations sop" in payload["summary"].lower()
    assert "Slack eval review narrative" not in payload["summary"]
    assert "Eval tracker" not in payload["summary"]
    assert payload["recommended_target"] == "KNIOps Operations onboarding/SOP context"
    assert "KNIOps/Operations" in payload["relevant_folders"]
    assert "onboarding or operations SOP Doc" in payload["relevant_docs"]
    assert "*Detailed Summary:*" in summary
    assert "- Candidate target: KNIOps Operations onboarding/SOP context" in summary
    assert "- Folder hints:" in summary
    assert "- Doc hints:" in summary
    assert "*Useful references:*" in summary
    assert "Useful references require live Workspace reads" in summary


def test_airtable_context_dry_run_tracks_finance_tax_topic_without_eval_boilerplate() -> None:
    output = cli._context_agent_dry_run_output(
        "airtable_context_agent",
        (
            "@KNI airtable context agent: Use Airtable context tools only, read-only. "
            "I am looking for whether the 2026 Finance & Tax Tracker Tax Payments "
            "table has Estimated Tax Period 2 payment records and the Q2 Rolling Taxes "
            "Summary. Do not create, update, delete, attach files, export, or write."
        ),
        None,
    )

    assert output is not None
    payload = output.model_dump(mode="json")
    summary = cli._context_agent_human_summary(payload)

    assert "2026 finance & tax tracker" in payload["summary"].lower()
    assert "tax payments" in payload["summary"].lower()
    assert "estimated tax period 2" in payload["summary"].lower()
    assert "eval_tracker" not in payload["summary"]
    assert "Promptfoo" not in payload["summary"]
    assert payload["base_alias"] == "finance_tax_tracker"
    assert payload["relevant_tables"] == ["Tax Payments"]
    assert "Estimated Tax Periods" in payload["relevant_fields"]
    assert "Q2 Rolling Taxes Summary" in payload["recommended_record_identity"]
    assert "*Detailed Summary:*" in summary
    assert "- Airtable target hints: finance_tax_tracker / Tax Payments" in summary
    assert "- Field hints:" in summary
    assert "- Record filter guidance:" in summary
    assert "*Useful references:*" in summary
    assert "Useful references require live Airtable schema/record reads" in summary


def test_context_agent_human_summary_keeps_multiple_detail_items_readable() -> None:
    summary = cli._context_agent_human_summary(
        {
            "summary": "Airtable records were read successfully.",
            "blockers": [
                "Sample records were arbitrary because no filter was provided",
                "One field label differs from the schema label",
            ],
        }
    )

    assert summary == (
        "*Answer:*\n"
        "Airtable records were read successfully.\n\n"
        "*Detailed Summary:*\n"
        "- Needs attention:\n"
        "  - Sample records were arbitrary because no filter was provided\n"
        "  - One field label differs from the schema label"
    )


def test_context_agent_human_summary_includes_airtable_record_summaries() -> None:
    summary = cli._context_agent_human_summary(
        {
            "summary": "Three matching Tax Payments records were visible.",
            "record_summaries": [
                {
                    "key": "IRS Estimated Taxes: Q2 2026 (pending)",
                    "value": "Tax Type: Federal; Amount: $4,705.00; Payment Date: 6/1/2026",
                    "note": "Period 2",
                },
                {
                    "key": "PA Estimated Taxes: Period 2 2026",
                    "value": "Tax Type: State; Amount: $920.00; Payment Date: 6/1/2026",
                    "note": "Period 2",
                },
            ],
        }
    )

    assert summary == (
        "*Answer:*\n"
        "Three matching Tax Payments records were visible.\n\n"
        "*Detailed Summary:*\n"
        "- Records visible:\n"
        "  - IRS Estimated Taxes: Q2 2026 (pending): Tax Type: Federal; "
        "Amount: $4,705.00; Payment Date: 6/1/2026 (Period 2)\n"
        "  - PA Estimated Taxes: Period 2 2026: Tax Type: State; "
        "Amount: $920.00; Payment Date: 6/1/2026 (Period 2)"
    )


def test_context_agent_human_summary_keeps_airtable_provider_ids_internal() -> None:
    summary = cli._context_agent_human_summary(
        {
            "summary": "Created, updated, and removed one marked test record.",
            "base_alias": "finance_tax_tracker",
            "relevant_tables": ["Business Expenses"],
            "recommended_record_identity": "recProviderInternal123",
            "record_summaries": [
                {
                    "key": "recProviderInternal123",
                    "value": "Business Expenses test record updated and removed.",
                    "note": "Provider read-back and absence verification passed.",
                }
            ],
            "sources": [
                {
                    "title": "Airtable verified test deletion",
                    "note": "Confirmed the marked record was absent after deletion.",
                    "location": "Business Expenses / recProviderInternal123",
                },
                {
                    "title": "Airtable base schema",
                    "note": "Verified the table before writing.",
                    "location": "finance_tax_tracker / appProviderInternal123",
                },
            ],
        }
    )

    assert "recProviderInternal123" not in summary
    assert "appProviderInternal123" not in summary
    assert "Record filter guidance" not in summary
    assert "Business Expenses test record updated and removed" in summary
    assert "Airtable verified test deletion" in summary


def test_context_agent_human_summary_includes_zotero_reference_summaries() -> None:
    summary = cli._context_agent_human_summary(
        {
            "summary": "The KNI foundational collection is available.",
            "sources": [
                {
                    "title": "The growing field of digital psychiatry",
                    "note": "Directly relevant to digital psychiatry background.",
                    "location": "https://doi.org/10.1002/wps.20883",
                },
                {
                    "title": "Toward the future of psychiatric diagnosis",
                    "note": "Directly relevant to psychiatric diagnosis background.",
                    "location": "https://doi.org/10.1186/1741-7015-11-126",
                },
            ],
            "blockers": ["No full-text extraction for some book records in the local cache"],
        }
    )

    assert summary == (
        "*Answer:*\n"
        "The KNI foundational collection is available.\n\n"
        "*Detailed Summary:*\n"
        "- Needs attention: No full-text extraction for some book records in the local cache.\n\n"
        "*Useful references:*\n"
        "  - The growing field of digital psychiatry - Directly relevant to digital "
        "psychiatry background. (https://doi.org/10.1002/wps.20883)\n"
        "  - Toward the future of psychiatric diagnosis - Directly relevant to "
        "psychiatric diagnosis background. (https://doi.org/10.1186/1741-7015-11-126)"
    )


def test_zotero_context_dry_run_tracks_requested_topic_without_generic_boilerplate() -> None:
    output = cli._context_agent_dry_run_output(
        "zotero_context_agent",
        (
            "@KNI zotero context agent: Use only local Zotero/cache context if available. "
            "I am looking for foundational depression or psychiatric diagnosis review "
            "material for an internal background scan. Do not import or write."
        ),
        None,
    )

    assert output is not None
    payload = output.model_dump(mode="json")
    summary = cli._context_agent_human_summary(payload)

    assert "foundational depression" in payload["summary"]
    assert "psychiatric diagnosis" in payload["summary"]
    assert "import" not in payload["summary"]
    assert "collections" not in payload["summary"]
    assert "google" not in payload["summary"].lower()
    assert "looking" not in payload["summary"].lower()
    assert "behavioral-health AI validation" not in payload["summary"]
    assert "foundational depression" in payload["collection_hints"]
    assert "psychiatric diagnosis" in payload["collection_hints"]
    assert "*Detailed Summary:*" in summary
    assert "- Collection/context hints:" in summary
    assert "- Evidence guidance:" in summary
    assert "*Useful references:*" in summary
    assert "Useful references require local item reads" in summary


def test_context_agent_human_summary_includes_feed_item_summaries() -> None:
    summary = cli._context_agent_human_summary(
        {
            "summary": "Two announcement feed items matched the request.",
            "articles": [
                {
                    "title": "AI model validation guide",
                    "url": "https://example.test/ai-validation",
                    "published_at": "2026-06-18",
                    "summary": "Practical validation guidance for clinical AI monitoring.",
                },
                {
                    "title": "Remote monitoring pilot update",
                    "source": "Internal RSS",
                    "summary": "Pilot operations update relevant to dashboard review.",
                },
            ],
            "recurring_themes": ["clinical AI validation", "remote monitoring operations"],
            "evidence_gaps": ["No full article extraction was available for one item"],
        }
    )

    assert summary == (
        "*Answer:*\n"
        "Two announcement feed items matched the request.\n\n"
        "*Detailed Summary:*\n"
        "- Items reviewed:\n"
        "  - AI model validation guide: Practical validation guidance for clinical AI "
        "monitoring. (2026-06-18)\n"
        "  - Remote monitoring pilot update: Pilot operations update relevant to dashboard "
        "review. (Internal RSS)\n"
        "- Themes:\n"
        "  - clinical AI validation\n"
        "  - remote monitoring operations\n"
        "- Needs attention: No full article extraction was available for one item.\n\n"
        "*Useful references:*\n"
        "  - AI model validation guide (https://example.test/ai-validation)"
    )


def test_cli_ask_gmail_triage_long_prompt_is_not_treated_as_fixture_path(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    database_url = f"sqlite:///{tmp_path / 'gmail-ask.db'}"
    long_prompt = (
        "gmail triage agent: diagnostic case diag_gmail_live_receipt_20260618_002 "
        "Use only this inline, non-sensitive email context: Subject: Partnership "
        "follow-up for remote patient monitoring validation From: Alex Rivera, "
        "Partnerships Lead, Example Health Body: Thanks for the earlier discussion. "
        "We are evaluating whether Keystone could help review our remote patient "
        "monitoring AI validation workflow before a pilot proposal in July. Could "
        "you summarize whether this needs a reply and draft-only next-step suggestion? "
        "No PHI is included. Return a human-useful Gmail triage answer: priority, "
        "why it matters, whether a reply is needed, suggested next action, and any "
        "caveats. Do not send email, create a Gmail draft, label messages, schedule, "
        "write files, create CRM records, publish, or post elsewhere."
    )

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        assert requested_agent == "gmail_triage"
        assert live_manual_plan is True
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "EmailTriageResult",
                    "send_enabled": False,
                    "model": {"provider": "openai", "name": "gpt-5.4-mini"},
                    "usage": {"requests": 1, "total_tokens": 42},
                    "cost": {"estimated_usd": 0.001},
                    "output": {
                        "subject": "Partnership follow-up for remote patient monitoring validation",
                        "priority": "high",
                        "summary": "A reply is needed.",
                        "needs_reply": True,
                        "recommended_action": "Prepare a draft-only reply.",
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "gmail_triage",
            "--live-sdk",
            "--database-url",
            database_url,
            "--json",
            long_prompt,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "gmail_triage"
    assert payload["output_type"] == "EmailTriageResult"
    assert payload["model_execution"]["model"] == "gpt-5.4-mini"
    assert payload["agent_run_id"]
    assert calls
    assert calls[0][1] == "scripts/run_gmail_triage.py"
    assert "--fixture" in calls[0]
    assert "--live-gmail" not in calls[0]
    assert "--no-live-gmail" in calls[0]
    assert "--no-allow-inbox" in calls[0]
    assert "--request" in calls[0]
    assert calls[0][calls[0].index("--request") + 1] == long_prompt
    records = SQLiteStore(database_url).fetch_all("agent_runs")
    assert len(records) == 1
    assert records[0]["agent_name"] == "gmail_triage"
    assert records[0]["dry_run"] == 0


def test_cli_ask_live_explicit_mention_honors_manual_plan_reroute(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        assert live_manual_plan is True
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "OpportunityScoutResult",
                    "send_enabled": False,
                    "output": {
                        "summary": "three-company comparison",
                        "ranked_opportunities": [],
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--json",
            "@KNI",
            "business",
            "research",
            "analyst",
            "Compare",
            "three",
            "software-first",
            "companies",
            "with",
            "measurement-based",
            "care",
            "or",
            "digital",
            "psychiatry",
            "tools",
            "for",
            "behavioral",
            "health",
            "clinics.",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "opportunity_scout"
    assert payload["manual_request_plan"]["target_agent"] == "opportunity_scout"
    assert calls
    command = calls[0]
    assert "scripts/run_opportunity_scout.py" in command
    assert "--topic" in command
    topic = command[command.index("--topic") + 1]
    assert topic == (
        "software-first companies with measurement-based care or digital psychiatry tools "
        "for behavioral health clinics"
    )
    assert command[command.index("--max-results") + 1] == "3"


def test_cli_ask_cost_tracking_directive_is_recorded_without_reaching_child(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []
    preflight_inputs: list[str] = []

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        preflight_inputs.append(str(request_text))
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "ResearchBrief",
                    "send_enabled": False,
                    "output": {"summary": "live brief"},
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "business_research_analyst",
            "--json",
            "research",
            "Lindus.",
            "Also",
            "keep",
            "track",
            "of",
            "this",
            "run",
            "costs.",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cost_tracking_requested"] is True
    assert preflight_inputs == ["research Lindus"]
    assert calls
    assert "Also keep track" not in " ".join(calls[0])


def test_cli_ask_live_payload_surfaces_missing_information(
    monkeypatch,
    capsys,
) -> None:
    def fake_run(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "ResearchBrief",
                    "send_enabled": False,
                    "output": {
                        "summary": "partial",
                        "unknowns": ["I did not have source-backed leadership evidence."],
                        "limitations": ["Only one source was available."],
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(["ask", "--agent", "business_research_analyst", "research", "Lindus"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Missing information:" in output
    assert "source-backed leadership evidence" in output
    assert "Only one source was available" in output


def test_cli_ask_kni_explicit_agent_auto_live_sdk_in_live_mode(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        assert requested_agent == "opportunity_scout"
        assert live_manual_plan is True
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "OpportunityScoutResult",
                    "send_enabled": False,
                    "output": {"records": []},
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(["ask", "@KNI", "opportunity", "scout", "find", "AI", "partners"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Agent: Opportunity Scout Agent" in output
    assert "Output type: OpportunityScoutResult" in output
    assert "Orchestrator review:" in output
    assert "WorkItem:" not in output
    assert calls
    assert "scripts/run_opportunity_scout.py" in calls[0]
    assert "--live-search" in calls[0]
    assert "--live-sdk" in calls[0]


def test_cli_ask_explicit_chief_of_staff_runs_orchestrator_preflight_advise_only(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []
    child_envs: list[dict[str, str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        child_envs.append(dict(kwargs.get("env") or {}))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "ChiefOfStaffResult",
                    "send_enabled": False,
                    "output": {
                        "summary": "Architecture review for agent routing.",
                        "recommended_actions": ["Keep raw request visible to specialists."],
                        "audit_notes": ["No external write attempted."],
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--json",
            "@KNI",
            "chief",
            "of",
            "staff",
            "review",
            "the",
            "agent",
            "architecture",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "chief_of_staff"
    assert payload["orchestrator_preflight"]["advisory_only"] is True
    assert payload["orchestrator_preflight"]["manual_request_plan"]["target_agent"] == (
        "chief_of_staff"
    )
    assert payload["orchestrator_preflight"]["route_result"]["route"] == "chief_of_staff"
    assert calls
    assert "scripts/run_chief_of_staff.py" in calls[0]
    assert "--live-search" in calls[0]
    assert "--live-search-plan" in calls[0]
    assert child_envs
    assert ORCHESTRATOR_PREFLIGHT_ENV in child_envs[0]
    assert MANUAL_REQUEST_PLAN_ENV in child_envs[0]
    assert ORCHESTRATOR_ROUTE_RESULT_ENV in child_envs[0]


def test_cli_ask_explicit_agent_send_request_blocked_by_orchestrator_preflight(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "send outreach email to this lead",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "blocked"
    assert payload["status"] == "blocked"
    assert payload["send_enabled"] is False
    assert payload["orchestrator_preflight"]["blocked_by_orchestrator"] is True
    assert payload["orchestrator_preflight"]["execution_allowed"] is False
    assert payload["block_kind"] == "send"
    assert payload["orchestrator_preflight"]["route_result"]["refused"] is True
    assert calls == []


def test_cli_ask_preflight_blocked_omits_raw_workflow_state(
    monkeypatch,
    capsys,
) -> None:
    def fake_preflight_with_slack_state(*args, **kwargs):
        preflight = _fake_orchestrator_preflight(*args, **kwargs)
        preflight.route_result = preflight.route_result.model_copy(
            update={
                "workflow_state_summary": {
                    "recent_slack_thread": [{"summary": "private Slack refusal context"}],
                    "prior_agent_runs": [{"summary": "prior operator correction"}],
                }
            }
        )
        return preflight

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight_with_slack_state)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "send outreach email to this lead",
        ]
    )

    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert exit_code == 0
    assert payload["status"] == "blocked"
    assert "workflow_state_summary" not in payload_text
    assert "private Slack refusal context" not in payload_text
    assert "prior operator correction" not in payload_text


def test_cli_ask_gmail_reply_without_thread_context_is_blocked(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "gmail_triage",
            "--live-sdk",
            "--json",
            "Reply politely and confirm next week works.",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "gmail_triage"
    assert payload["status"] == "blocked"
    assert payload["block_kind"] == "missing_gmail_context"
    assert payload["requires_gmail_context"] is True
    assert payload["send_enabled"] is False
    assert payload["agent_execution_plan"]["operation"] == "draft_reply"
    assert "usable email context" in payload["message"]
    assert "pasted sanitized email" in payload["message"]
    assert calls == []


def test_cli_exact_operator_gmail_draft_write_propagates_scoped_approval(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    prompt = (
        "Find the latest email from Example Health and create a Gmail draft reply. "
        "Do not send it."
    )
    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_GMAIL_DRAFT_ACCOUNT", "operator@example.com")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(["ask", "--agent", "gmail_triage", "--live-sdk", "--json", prompt])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "gmail_triage"
    assert len(calls) == 1
    command = calls[0]
    assert "--live-gmail" in command
    assert "--live-sdk" in command
    assert "--create-draft" in command
    assert "--no-dry-run" in command
    assert command[command.index("--expected-account") + 1] == "operator@example.com"
    approval_reference = command[command.index("--approval-reference") + 1]
    assert approval_reference.startswith("operator-command:gmail-draft:")
    assert prompt not in approval_reference
    assert command[command.index("--gmail-query") + 1].endswith('"example health"')
    assert payload["agent_execution_plan"]["create_gmail_drafts"] is True
    assert payload["send_enabled"] is False


def test_cli_natural_gmail_draft_update_resolves_without_operator_id(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    prompt = (
        "Update the existing Gmail draft with subject 'Project follow-up' for "
        "reviewer@example.com to be shorter and warmer. Do not send."
    )
    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_GMAIL_DRAFT_ACCOUNT", "operator@example.com")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(["ask", "--agent", "gmail_triage", "--live-sdk", "--json", prompt])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    command = calls[0]
    assert "--update-draft" in command
    assert "--create-draft" not in command
    assert command[command.index("--draft-subject-hint") + 1] == "Project follow-up"
    assert command[command.index("--draft-recipient-hint") + 1] == (
        "reviewer@example.com"
    )
    assert "--approval-reference" in command
    assert "--expected-account" in command
    assert payload["agent_execution_plan"]["operation"] == "update_draft"
    assert payload["agent_execution_plan"]["draft_subject_hint"] == "Project follow-up"
    assert payload["send_enabled"] is False


def test_cli_ask_gmail_triage_live_promotes_inline_email_workflow_to_work_item(
    monkeypatch,
    capsys,
) -> None:
    work_item_calls: list[dict[str, object]] = []

    def fake_work_item(input_text, **kwargs):
        work_item_calls.append({"input_text": input_text, **kwargs})
        return 0

    prompt = (
        "gmail triage this sanitized inbound email from Mindful Care, research "
        "Mindful Care, and prepare a draft-only Slack-thread sample outreach for "
        "review. Email: From: Jordan Lee, Operations at Mindful Care. Subject: "
        "Follow-up on measurement support. Body: Hi Jordan, our team is reviewing "
        "measurement-based care workflows and may need advisory help on evaluation "
        "design. Could you let me know if this is relevant for Keystone? Do not "
        "send, create Gmail drafts, post outside this thread, schedule, publish, "
        "create files, update Airtable/CRM/Drive/Sheets, or write external systems."
    )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "_run_ask_work_item", fake_work_item)

    exit_code = main(["ask", "--agent", "gmail_triage", "--live-sdk", "--json", prompt])

    assert exit_code == 0
    assert capsys.readouterr().out == ""
    assert len(work_item_calls) == 1
    call = work_item_calls[0]
    assert call["input_text"] == prompt
    assert call["live_search"] is False
    assert call["live_sdk"] is True
    assert call["json_output"] is True
    assert call["max_manager_steps"] == 3
    assert call["manual_plan"] is not None
    assert call["orchestrator_preflight"] is not None


def test_cli_explicit_orchestrator_source_bundle_uses_work_item_graph(
    monkeypatch,
    capsys,
) -> None:
    work_item_calls: list[dict[str, object]] = []
    fixture_path = Path(__file__).parent / "fixtures/graph_research_to_draft_source_bundle.json"

    def fake_work_item(input_text, **kwargs):
        work_item_calls.append({"input_text": input_text, **kwargs})
        return 0

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "_run_ask_work_item", fake_work_item)
    monkeypatch.setattr(
        cli,
        "_run_ask_orchestrator",
        lambda *_args, **_kwargs: pytest.fail("direct Orchestrator path must not run"),
    )

    exit_code = main(
        [
            "ask",
            "--agent",
            "orchestrator",
            "--live-sdk",
            "--context-file",
            str(fixture_path),
            "--json",
            "Research the supplied packet and prepare a draft-only reply.",
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == ""
    assert len(work_item_calls) == 1
    call = work_item_calls[0]
    assert call["context_file_path"] == str(fixture_path)
    assert call["live_sdk"] is True
    assert call["live_search"] is False
    assert call["max_manager_steps"] == 3


def test_cli_source_bundle_request_budget_blocks_before_preflight_model_call(
    monkeypatch,
    capsys,
) -> None:
    fixture_path = Path(__file__).parent / "fixtures/graph_research_to_draft_source_bundle.json"
    preflight_calls: list[str] = []

    def unexpected_preflight(request_text, **_kwargs):
        preflight_calls.append(request_text)
        pytest.fail("request budget must block before preflight")

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", unexpected_preflight)

    exit_code = main(
        [
            "ask",
            "--agent",
            "orchestrator",
            "--live-sdk",
            "--context-file",
            str(fixture_path),
            "--max-openai-requests",
            "1",
            "--json",
            "Research the supplied packet and prepare a draft-only reply.",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert preflight_calls == []
    assert payload["block_kind"] == "openai_request_budget_exceeded"
    assert payload["estimated_requests"]["max"] == 2
    assert payload["estimated_requests"]["stages"] == [
        "manual_request_planner",
        "outreach_composer_synthesis",
    ]
    assert payload["openai_requests_made"] == 0


def test_cli_source_bundle_no_live_manual_plan_fits_one_request_ceiling(
    monkeypatch,
    capsys,
) -> None:
    fixture_path = Path(__file__).parent / "fixtures/graph_research_to_draft_source_bundle.json"
    work_item_calls: list[dict[str, object]] = []
    preflight_live_plan_values: list[bool] = []

    def fake_preflight(request_text, *, live_manual_plan, **kwargs):
        preflight_live_plan_values.append(live_manual_plan)
        return _fake_orchestrator_preflight(
            request_text,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_work_item(input_text, **kwargs):
        work_item_calls.append({"input_text": input_text, **kwargs})
        return 0

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight)
    monkeypatch.setattr(cli, "_run_ask_work_item", fake_work_item)

    exit_code = main(
        [
            "ask",
            "--agent",
            "orchestrator",
            "--live-sdk",
            "--no-live-manual-plan",
            "--context-file",
            str(fixture_path),
            "--max-openai-requests",
            "1",
            "--json",
            "Research the supplied packet and prepare a draft-only reply.",
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == ""
    assert preflight_live_plan_values == [False]
    assert len(work_item_calls) == 1


def test_cli_connector_backed_gmail_graph_budget_blocks_before_preflight(
    monkeypatch,
    capsys,
) -> None:
    preflight_calls: list[str] = []
    request = (
        "Read the latest Gmail thread from alex@example.test, research Example Health "
        "using only the selected thread context, and prepare a draft reply for review "
        "without sending, creating a Gmail draft, searching the web, posting, or "
        "writing externally."
    )

    def unexpected_preflight(request_text, **_kwargs):
        preflight_calls.append(request_text)
        pytest.fail("connector graph budget must block before preflight or Gmail")

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", unexpected_preflight)

    exit_code = main(
        [
            "ask",
            "--live-sdk",
            "--no-live-manual-plan",
            "--max-openai-requests",
            "0",
            "--max-manager-steps",
            "3",
            "--json",
            request,
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert preflight_calls == []
    assert payload["block_kind"] == "openai_request_budget_exceeded"
    assert payload["estimated_requests"]["min"] == 4
    assert payload["estimated_requests"]["stages"] == [
        "gmail_provider_read",
        "business_research_sdk",
        "outreach_composer_sdk",
        "final_response_synthesis",
    ]
    assert payload["estimated_requests"]["max"] == 7
    assert payload["openai_requests_made"] == 0


def test_bounded_connector_graph_fits_eight_request_ceiling_and_disables_web_search() -> None:
    request = (
        "Read the latest Gmail thread from the configured exact test sender, research "
        "the sender organization using only the selected thread context, and return "
        "a suggested reply with supporting evidence and the approval status."
    )
    args = SimpleNamespace(
        context_file="",
        agent=None,
        max_manager_steps=3,
        live_search=True,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
    )

    assert estimate["max"] == 8
    assert estimate["stages"] == [
        "manual_request_planner",
        "gmail_provider_read",
        "business_research_sdk",
        "outreach_composer_sdk",
        "final_response_synthesis",
    ]


def test_bounded_connector_graph_accepts_state_and_collaboration_wording() -> None:
    request = (
        "Review the latest Gmail thread from the configured exact test sender, including "
        "all messages and the original inquiry. Identify the current conversation state, "
        "recommend the most useful KNI-specific collaboration next step using only that "
        "thread and approved KNI context, and include a reply only if replying now would "
        "move the relationship forward."
    )

    assert cli._is_bounded_gmail_recommendation_graph(request, manager_steps=3) is False
    assert cli._is_bounded_gmail_research_reply_graph(request, manager_steps=3) is True
    assert cli._request_forbids_live_research(request) is True


def test_controlled_gmail_recommendation_fits_two_request_ceiling() -> None:
    request = (
        "Review the latest Gmail thread from the configured exact test sender, including "
        "the original inquiry and all messages. Using only that complete thread and "
        "approved KNI context, identify the current conversation state and recommend the "
        "most useful KNI-specific next step. Include reply copy only if replying now would "
        "move the relationship forward, and return the result here for review."
    )
    args = SimpleNamespace(
        context_file="",
        agent=None,
        max_manager_steps=3,
        live_search=False,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
    )

    assert cli._is_bounded_gmail_recommendation_graph(request, manager_steps=3) is True
    assert estimate["max"] == 2
    assert estimate["stages"] == ["manual_request_planner", "outreach_composer_sdk"]


def test_bounded_gmail_research_summary_fits_eight_request_ceiling() -> None:
    request = (
        "Read the latest Gmail thread from the configured exact sender, including all "
        "messages. Identify the organization and product, then research them using "
        "current public sources. Determine the underlying data source, distinguish "
        "supported facts from inference, summarize the limitations, and include visible "
        "source links. Do not draft or send a reply, create a Gmail draft, post, schedule, "
        "share, or write externally."
    )
    args = SimpleNamespace(
        context_file="",
        agent=None,
        max_manager_steps=3,
        live_search=True,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
    )

    assert cli._is_bounded_gmail_research_summary_graph(request, manager_steps=3) is True
    assert estimate["max"] == 8
    assert estimate["stages"] == [
        "manual_request_planner",
        "gmail_provider_read",
        "business_research_sdk",
        "final_response_synthesis",
    ]


def test_gmail_research_without_explicit_summary_and_no_write_boundary_stays_generic() -> None:
    request = "Read the latest Gmail thread, research the company, and tell me what to do."
    args = SimpleNamespace(
        context_file="",
        agent=None,
        max_manager_steps=3,
        live_search=True,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
    )

    assert cli._is_bounded_gmail_research_summary_graph(request, manager_steps=3) is False
    assert estimate["max"] == 20


def test_cli_explicit_chief_budget_uses_delegated_context_owner_turn_limit(
    monkeypatch,
    capsys,
) -> None:
    preflight_calls: list[str] = []
    request = (
        "Using Airtable context, create one marked KBA test expense in the Business "
        "Expenses table, verify it, update the same record description, verify it "
        "again, and remove only that test record."
    )

    def unexpected_preflight(request_text, **_kwargs):
        preflight_calls.append(request_text)
        pytest.fail("delegated-route request budget must block before preflight")

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_CHIEF_OF_STAFF_SDK_MAX_TURNS", "4")
    monkeypatch.delenv("KEYSTONE_AIRTABLE_CONTEXT_AGENT_SDK_MAX_TURNS", raising=False)
    monkeypatch.setattr(cli, "run_orchestrator_preflight", unexpected_preflight)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--live-sdk",
            "--no-live-manual-plan",
            "--max-openai-requests",
            "4",
            "--json",
            request,
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert preflight_calls == []
    assert payload["estimated_requests"]["max"] == 6
    assert payload["estimated_requests"]["stages"] == ["airtable_context_agent_sdk"]
    assert payload["openai_requests_made"] == 0


def test_cli_ask_gmail_triage_live_accepts_simple_inline_sanitized_email_fixture(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []
    fixture_texts: list[str] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        fixture_path = Path(command[command.index("--fixture") + 1])
        fixture_texts.append(fixture_path.read_text(encoding="utf-8"))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    prompt = (
        "gmail triage this sanitized inbound email and prepare a draft reply for review. "
        "Email: From: Jordan Lee, Operations at Mindful Care. Subject: Follow-up on "
        "measurement support. Body: Hi Jordan, our team is reviewing measurement-based "
        "care workflows and may need advisory help on evaluation design. Could you let "
        "me know if this is relevant for Keystone? Do not send or create Gmail drafts."
    )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(["ask", "--agent", "gmail_triage", "--live-sdk", "--json", prompt])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "gmail_triage"
    assert calls
    command = calls[0]
    assert "--fixture" in command
    assert "--no-live-gmail" in command
    assert "--no-allow-inbox" in command
    assert "--sender-name" in command
    assert command[command.index("--sender-name") + 1] == (
        "Jordan Lee, Operations at Mindful Care"
    )
    assert fixture_texts == [
        (
            "Subject: Follow-up on measurement support\n\n"
            "Hi Jordan, our team is reviewing measurement-based care workflows and "
            "may need advisory help on evaluation design. Could you let me know if "
            "this is relevant for Keystone?"
        )
    ]


def test_cli_ask_live_child_timeout_returns_structured_payload(
    monkeypatch,
    capsys,
) -> None:
    def timeout_run(command, **kwargs):
        raise cli.subprocess.TimeoutExpired(command, kwargs.get("timeout", 1))

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_CHILD_AGENT_TIMEOUT_SECONDS", "2")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", timeout_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "review the agent architecture",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["status"] == "timeout"
    assert payload["timeout_seconds"] == 2.0
    assert payload["send_enabled"] is False
    assert payload["output"]["error_type"] == "timeout"
    assert payload["output"]["failure"]["schema"] == "keystone.operator_failure.v1"
    assert payload["output"]["failure"]["kind"] == "provider_timeout"


def test_cli_ask_live_child_failure_returns_redacted_structured_payload(
    monkeypatch,
    capsys,
) -> None:
    def failed_run(_command, **_kwargs):
        fake_stdout_token = "sk-" + ("y" * 20)
        fake_stderr_token = "sk-" + ("x" * 24)
        long_trace_tail = "ValueError: final diagnostic line from SDK provider"
        return SimpleNamespace(
            returncode=7,
            stdout=f"partial stdout token={fake_stdout_token}",
            stderr=(
                f"failed token={fake_stderr_token}\n"
                + "\n".join(f"stack frame {index}" for index in range(500))
                + f"\n{long_trace_tail}"
            ),
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", failed_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "review the agent architecture",
        ]
    )

    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert exit_code == 7
    assert payload["status"] == "failed"
    assert payload["child_returncode"] == 7
    assert payload["send_enabled"] is False
    assert payload["output"]["error_type"] == "child_process_failed"
    assert payload["output"]["returncode"] == 7
    assert payload["output"]["failure"]["schema"] == "keystone.operator_failure.v1"
    assert payload["output"]["failure"]["kind"] == "unknown_error"
    assert "sk-" + ("x" * 24) not in payload_text
    assert "sk-" + ("y" * 20) not in payload_text
    assert "[REDACTED]" in payload_text
    assert "stack frame 250" not in payload["output"]["stderr_excerpt"]
    assert "ValueError: final diagnostic line from SDK provider" in payload["output"]["stderr_excerpt"]
    assert "ValueError: final diagnostic line from SDK provider" in payload["output"]["error_tail"]
    assert "sk-" + ("x" * 24) not in payload["output"]["error_tail"]


def test_cli_ask_live_child_failure_prefers_operator_failure_payload(
    monkeypatch,
    capsys,
) -> None:
    def failed_run(_command, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps(
                {
                    "status": "failed",
                    "output": {
                        "failure": {
                            "schema": "keystone.operator_failure.v1",
                            "kind": "missing_credentials",
                            "summary": "A required live-provider credential is unavailable.",
                            "reason": "OPENAI_API_KEY=<missing-test-openai-key>",
                            "next_step": "Configure or disable the live provider, then rerun.",
                            "retryable": False,
                            "safe_to_continue": True,
                        }
                    },
                }
            ),
            stderr="Traceback should not become the summary",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", failed_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "review the agent architecture",
        ]
    )

    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert exit_code == 1
    assert payload["output"]["failure"]["kind"] == "missing_credentials"
    assert payload["output"]["summary"] == "A required live-provider credential is unavailable."
    assert "sk-test-secret" not in payload_text


def test_cli_ask_work_item_failure_returns_clear_json_and_stderr(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    def failed_work_item(*_args, **_kwargs):
        raise ToolGuardrailViolation("blocked by safety guardrail")

    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(
        "keystone_agents.langgraph_workflow.advance_work_item_manager_loop_with_optional_langgraph",
        failed_work_item,
    )

    exit_code = main(
        [
            "ask",
            "--json",
            "--database-url",
            f"sqlite:///{tmp_path / 'workitems.sqlite'}",
            "@KNI opportunity scout find source-backed grants",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 1
    assert payload["status"] == "failed"
    assert payload["output"]["error_type"] == "ToolGuardrailViolation"
    assert "safety guardrail" in payload["output"]["summary"]
    assert payload["output"]["failure"]["schema"] == "keystone.operator_failure.v1"
    assert payload["output"]["failure"]["kind"] == "guardrail_block"
    assert payload["output"]["failure"]["safe_to_continue"] is True
    assert "Business Agents run failed" in captured.err
    assert "Traceback" not in captured.err


def test_cli_ask_live_child_malformed_json_returns_redacted_structured_payload(
    monkeypatch,
    capsys,
) -> None:
    def malformed_run(_command, **_kwargs):
        fake_stdout_token = "sk-" + ("x" * 24)
        fake_stderr_token = "sk-" + ("y" * 20)
        return SimpleNamespace(
            returncode=0,
            stdout=f"not json token={fake_stdout_token}",
            stderr=f"warning token={fake_stderr_token}",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", malformed_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "review the agent architecture",
        ]
    )

    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert exit_code == 1
    assert payload["status"] == "failed"
    assert payload["child_returncode"] == 0
    assert payload["send_enabled"] is False
    assert payload["output"]["error_type"] == "child_process_malformed_json"
    assert payload["output"]["failure"]["schema"] == "keystone.operator_failure.v1"
    assert payload["output"]["failure"]["kind"] == "schema_or_parse_error"
    assert "parse_error" in payload["output"]
    assert "sk-" + ("x" * 24) not in payload_text
    assert "sk-" + ("y" * 20) not in payload_text
    assert "[REDACTED]" in payload_text


def test_cli_ask_no_live_sdk_opt_out_keeps_work_item_in_live_mode(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask-live-opt-out.db'}"
    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")

    exit_code = main(
        [
            "ask",
            "--no-live-sdk",
            "--database-url",
            database_url,
            "@KNI",
            "business",
            "research",
            "analyst",
            "research",
            "Lindus",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "WorkItem:" in output
    assert "Route: business_research_analyst" in output


def test_operator_no_live_sdk_wording_overrides_live_cli_flag() -> None:
    args = SimpleNamespace(live_sdk=True)

    assert (
        cli._ask_live_sdk_enabled(
            args,
            input_text="Run deterministically with no live SDK or model calls.",
        )
        is False
    )
    assert cli._ask_live_sdk_enabled(args, input_text="Return the OpenAI request count.") is True


def test_cli_agents_list_prints_registry_cards(capsys) -> None:
    exit_code = main(["agents", "list"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "gmail_triage" in output
    assert "OpportunityScoutResult" in output
    assert "Live flags" in output


def test_cli_agents_list_json(capsys) -> None:
    exit_code = main(["agents", "list", "--json"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"route_name": "orchestrator"' in output
    assert '"tool_policy"' in output
    assert '"allowed_tool_names"' in output


def test_cli_agents_tools_prints_sanitized_runtime_availability(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli,
        "agent_cards",
        lambda: [
            {
                "route_name": "chief_of_staff",
                "agent_name": "Chief",
                "runtime_tool_availability": {
                    "file_search": {
                        "status": "available",
                        "available": True,
                        "vector_store_id_count": 1,
                        "vector_store_source": "agent",
                        "vector_store_env_name": (
                            "KEYSTONE_CHIEF_OF_STAFF_FILE_SEARCH_VECTOR_STORE_IDS"
                        ),
                    },
                    "local_kni_documents": {
                        "status": "ready",
                        "available": True,
                        "indexed_count": 1055,
                        "local_only": True,
                        "model_context_allowed": True,
                        "send_enabled": False,
                    },
                    "mcp": {
                        "status": "sdk_available_not_configured",
                        "available": False,
                        "sdk_available": True,
                        "reason": "Agents SDK HostedMCPTool is installed.",
                    },
                    "source_layer_policy": {
                        "status": "declared",
                        "available": True,
                        "layers": [
                            {
                                "layer": "local_kni_documents",
                                "runtime_status": "ready",
                            },
                            {
                                "layer": "hosted_file_search",
                                "runtime_status": "available",
                            },
                            {
                                "layer": "public_web_search",
                                "runtime_status": "attached_live_available",
                            },
                        ],
                    },
                },
            }
        ],
    )

    exit_code = main(["agents", "tools", "--agent", "chief_of_staff"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "chief_of_staff" in output
    assert "file_search" in output
    assert "local_kni_documents" in output
    assert "stores=1" in output
    assert "indexed=1055" in output
    assert "source_layer_policy" in output
    assert "local_kni_documents:ready" in output
    assert "hosted_file_search:available" in output
    assert "sdk_available_not_configured" in output
    assert "vs_" not in output


def test_cli_agents_tools_json_reports_runtime_availability(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli,
        "agent_cards",
        lambda: [
            {
                "route_name": "business_research_analyst",
                "agent_name": "Research",
                "runtime_tool_availability": {
                    "file_search": {
                        "status": "not_configured",
                        "available": False,
                        "vector_store_id_count": 0,
                    }
                },
            }
        ],
    )

    exit_code = main(["agents", "tools", "--agent", "business_research_analyst", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["route_name"] == "business_research_analyst"
    assert payload[0]["runtime_tool_availability"]["file_search"]["status"] == (
        "not_configured"
    )


def test_cli_agents_file_search_config_prints_sanitized_summary(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli,
        "local_file_search_config_summary",
        lambda: {
            "path": ".local/file-search-vector-stores.json",
            "exists": True,
            "configured": True,
            "status": "ready",
            "error": None,
            "global": {
                "vector_store_id_count": 1,
                "max_num_results": 5,
                "include_search_results": False,
            },
            "agents": [
                {
                    "agent_name": "chief_of_staff",
                    "known_agent": True,
                    "vector_store_id_count": 1,
                    "max_num_results": None,
                    "include_search_results": True,
                },
                {
                    "agent_name": "unknown_helper",
                    "known_agent": False,
                    "vector_store_id_count": 1,
                    "max_num_results": None,
                    "include_search_results": False,
                },
            ],
            "unknown_agents": ["unknown_helper"],
        },
    )

    exit_code = main(["agents", "file-search-config"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Local FileSearch config: ready" in output
    assert "chief_of_staff" in output
    assert "unknown_helper" in output
    assert "Unknown agents: unknown_helper" in output
    assert "Stores" in output
    assert "vs_" not in output


def test_cli_agents_file_search_config_json_reports_invalid(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli,
        "local_file_search_config_summary",
        lambda: {
            "path": ".local/file-search-vector-stores.json",
            "exists": True,
            "configured": True,
            "status": "invalid_config",
            "error": "agents.chief_of_staff.vector_store_ids must include at least one id",
            "global": None,
            "agents": [],
            "unknown_agents": [],
        },
    )

    exit_code = main(["agents", "file-search-config", "--json"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "invalid_config"
    assert payload["error"]
