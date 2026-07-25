from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from agents.agent_output import AgentOutputSchema

import keystone_agents.eval_dashboard_health as dashboard_health
import promptfoo.eval_dashboard as eval_dashboard_module
import promptfoo.eval_dashboard_server as eval_dashboard_server
import scripts.add_promptfoo_human_review as human_review_script
import scripts.check_eval_slack_readiness as readiness
import scripts.promptfoo_eval_db as promptfoo_eval_db_script
import scripts.run_eval_slack_test_server as slack_test_server
import scripts.sync_slack_eval_thread as sync_slack_eval_thread_script
from keystone_agents.eval_dashboard_health import (
    eval_dashboard_readiness,
    resolve_dashboard_manager_path,
)
from promptfoo.assertions.kba_slack_invariants import grade_output
from promptfoo.eval_dashboard import (
    dashboard_payload,
    render_dashboard,
    render_review_form,
    slack_run_post_save_state,
)
from promptfoo.eval_dashboard_server import (
    data_quality_response,
    eval_case_bundle_response,
    eval_run_ledger_response,
    follow_up_queue_response,
    legacy_dashboard_redirect_target,
    save_analysis_exclusion_payload,
    save_human_review_payload,
    trace_diagnostics_response,
    workflow_readiness_response,
)
from promptfoo.eval_database import (
    backfill_slack_manual_run_summaries,
    eval_case_status,
    eval_context_from_slack_thread,
    import_promptfoo_results,
    list_eval_cases,
    list_eval_trace_events,
    record_eval_trace_event,
    record_promptfoo_eval_to_benchmark,
    record_slack_eval_run,
    resolve_slack_eval_case_id,
)
from promptfoo.eval_sanitizer import scan_promptfoo_case_files
from promptfoo.human_review import (
    SCORE_DIMENSIONS,
    HumanEvalReview,
    build_slack_review_template,
    list_human_reviews,
    parse_human_review,
    save_human_review,
)
from promptfoo.orchestrator_judge import (
    OrchestratorEvalJudgeScorecard,
    score_eval_run_with_orchestrator_judge,
)
from promptfoo.providers import keystone_agent_provider


def _review_scores(**overrides: float) -> dict[str, float]:
    scores = {dimension: 4 for dimension in SCORE_DIMENSIONS}
    scores.update(overrides)
    return scores


def _side_effects(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "keystone.promptfoo.side_effects.v1",
        "email_sent": False,
        "gmail_draft_created": False,
        "gmail_label_changed": False,
        "slack_message_posted": False,
        "crm_write_performed": False,
        "calendar_write_performed": False,
        "external_file_write_performed": False,
        "external_write_performed": False,
        "blocked_write_attempts": [],
        "approval_ref": "",
        "evidence_complete": True,
    }
    payload.update(overrides)
    return payload


def test_database_inventory_contract_excludes_review_prose_and_preserves_scores() -> None:
    human_scores = _review_scores(accuracy=5)
    orchestrator_scores = _review_scores(relevance=5)
    case = {
        "case_id": "inventory_001",
        "display_case_id": "inventory_001",
        "agent": "business_research_analyst",
        "human_average": 4.1,
        "human_scores": human_scores,
        "human_notes": "Detailed reviewer comment stays out of inventory.",
        "orchestrator_judge_average": 4.2,
        "orchestrator_judge_scores": orchestrator_scores,
        "orchestrator_judge_notes": "Detailed rationale stays out of inventory.",
        "orchestrator_judge_run_comment": "Full comment stays out.",
        "user_input": "Full prompt stays in exports and case bundles.",
        "response_text": "Full response stays in detail surfaces.",
        "review_url": "/review?case=inventory_001",
        "case_bundle_url": "/api/eval-case-bundle?case=inventory_001",
    }
    contract = eval_dashboard_module._database_inventory_contract([case])
    export_row = eval_dashboard_module.dashboard_case_export_rows({"cases": [case]})[0]

    row = contract["rows"][0]
    assert contract["schema"] == "keystone.eval.database_inventory.v1"
    assert contract["read_only"] is True
    assert row["human_scores"] == human_scores
    assert row["orchestrator_judge_scores"] == orchestrator_scores
    assert row["review_url"].startswith("/review")
    assert row["case_bundle_url"].startswith("/api/eval-case-bundle")
    assert {
        "user_input",
        "response_text",
        "human_notes",
        "orchestrator_judge_notes",
        "orchestrator_judge_run_comment",
    }.isdisjoint(row)
    assert export_row["prompt"] == "Full prompt stays in exports and case bundles."
    assert export_row["latest_response"] == "Full response stays in detail surfaces."
    assert export_row["human_notes"] == "Detailed reviewer comment stays out of inventory."
    assert export_row["orchestrator_judge_notes"] == "Detailed rationale stays out of inventory."


def _bool_or_none_for_test(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    return None


def test_eval_slack_readiness_script_json(capsys, monkeypatch) -> None:
    if not readiness.DEFAULT_SLACK_REPO.is_dir():
        pytest.skip("sibling keystone-slack checkout is unavailable in clean CI")
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
    assert checks["eval dashboard manager"]["status"] in {"pass", "warn"}
    assert "manage_eval_dashboard.sh" in checks["eval dashboard manager"]["detail"]
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
        "Run `npm run eval:slack:strict-live-test-server`; ensure the Slack app has "
        "app_mentions:read, chat:write, and channels:history for public #evals, then paste the "
        "printed committed eval prompt for the shown agent into #evals and leave the server "
        "running before clicking Slack case links."
    )


def test_promptfoo_case_sanitation_scan_blocks_private_eval_content(tmp_path) -> None:
    cases_path = tmp_path / "promptfoo" / "tests" / "unsafe.yaml"
    cases_path.parent.mkdir(parents=True)
    cases_path.write_text(
        """
tests:
  - vars:
      case_id: unsafe_001
      user_input: "Email jane@example.com about patient diagnosis"
  - vars:
      case_id: exempt_001
      sanitized_fixture_exemption: true
      user_input: "Fixture mentions jane@example.com only as an approved sanitizer test"
""",
        encoding="utf-8",
    )

    result = scan_promptfoo_case_files([cases_path], root=tmp_path)

    assert result["status"] == "fail"
    assert result["case_count"] == 2
    issue_cases = {issue["case_id"] for issue in result["issues"]}
    assert issue_cases == {"unsafe_001"}
    assert {issue["kind"] for issue in result["issues"]}.issuperset({"email", "sensitive_term"})


def test_eval_dashboard_manager_resolves_from_repo_root_and_nested_path() -> None:
    repo = Path(__file__).resolve().parents[1]

    direct = resolve_dashboard_manager_path(
        "scripts/manage_eval_dashboard.sh",
        cwd=repo,
        root=repo,
    )
    stale_nested = resolve_dashboard_manager_path(
        "keystone-business-agents/scripts/manage_eval_dashboard.sh",
        cwd=repo,
        root=repo,
    )

    assert direct.exists
    assert direct.executable
    assert direct.manager_path == repo / "scripts" / "manage_eval_dashboard.sh"
    assert stale_nested.exists
    assert stale_nested.manager_path == direct.manager_path


def test_eval_dashboard_manager_declares_structured_log_contract() -> None:
    script = Path("scripts/manage_eval_dashboard.sh").read_text(encoding="utf-8")

    assert "eval-dashboard.structured.jsonl" in script
    assert 'HEALTH_URL="http://127.0.0.1:8769/api/status"' in script
    assert 'curl -fsS --max-time 5 "$HEALTH_URL"' in script
    assert "for _ in {1..5}" in script
    assert '"health_url": health_url' in script
    assert "from keystone_agents.structured_logging import structured_log_event" in script
    assert 'component="eval_dashboard_manager"' in script
    assert 'structured_log "health_failed" "error" "error" "health"' in script
    assert 'structured_log "status_unreachable" "error" "error" "status"' in script
    assert 'tail -80 "$STDOUT_LOG" "$STDERR_LOG" "$STRUCTURED_LOG"' in script


def test_eval_dashboard_readiness_reports_missing_manager(tmp_path) -> None:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)

    readiness_payload = eval_dashboard_readiness(
        manager_path="missing/manage_eval_dashboard.sh",
        cwd=tmp_path,
        root=root,
    )

    assert readiness_payload["manager_exists"] is False
    assert any("manager script missing" in item for item in readiness_payload["issues"])


def test_eval_dashboard_readiness_uses_lightweight_health_endpoint(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    manager = root / "scripts" / "manage_eval_dashboard.sh"
    manager.parent.mkdir(parents=True)
    manager.write_text("#!/bin/sh\n", encoding="utf-8")
    manager.chmod(0o755)
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    (root / "scripts" / "serve_promptfoo_eval_dashboard.py").write_text("", encoding="utf-8")
    probed_urls: list[str] = []

    def fake_reachable(url: str) -> bool:
        probed_urls.append(url)
        return True

    monkeypatch.setattr(dashboard_health, "_dashboard_reachable", fake_reachable)
    monkeypatch.setattr(dashboard_health, "_port_open", lambda *_args: True)
    monkeypatch.setattr(dashboard_health, "_launchd_service_running", lambda *_args: False)
    monkeypatch.setattr(dashboard_health, "_module_importable", lambda *_args: True)

    readiness_payload = eval_dashboard_readiness(
        manager_path=manager,
        dashboard_url="http://127.0.0.1:8769/dashboard",
        root=root,
    )

    assert readiness_payload["dashboard_url"] == "http://127.0.0.1:8769/dashboard"
    assert readiness_payload["health_url"] == "http://127.0.0.1:8769/api/status"
    assert readiness_payload["dashboard_reachable"] is True
    assert readiness_payload["health_reachable"] is True
    assert probed_urls == ["http://127.0.0.1:8769/api/status"]


def test_eval_dashboard_manager_warns_when_dashboard_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(
        readiness,
        "eval_dashboard_readiness",
        lambda **_: {
            "manager_path": "/repo/scripts/manage_eval_dashboard.sh",
            "dashboard_url": "http://127.0.0.1:8769/dashboard",
            "health_url": "http://127.0.0.1:8769/api/status",
            "issues": [],
            "port_open": False,
            "dashboard_reachable": False,
            "launchd_running": False,
        },
    )

    result = readiness._eval_dashboard_manager_check()

    assert result["status"] == "warn"
    assert "dashboard health endpoint was not reachable" in result["detail"]
    assert "manage_eval_dashboard.sh restart" in result["detail"]
    assert "port_open=false" in result["detail"]


def test_eval_dashboard_reachability_falls_back_to_curl(monkeypatch) -> None:
    class FakeCompleted:
        returncode = 0

    def blocked_urlopen(*_args, **_kwargs):
        raise OSError("operation not permitted")

    monkeypatch.setattr(dashboard_health, "urlopen", blocked_urlopen)
    monkeypatch.setattr(dashboard_health.subprocess, "run", lambda *_args, **_kwargs: FakeCompleted())

    assert dashboard_health._dashboard_reachable("http://127.0.0.1:8769/dashboard") is True


def test_eval_dashboard_manager_uses_dashboard_url_reachability(monkeypatch) -> None:
    monkeypatch.setattr(
        readiness,
        "eval_dashboard_readiness",
        lambda **_: {
            "manager_path": "/repo/scripts/manage_eval_dashboard.sh",
            "dashboard_url": "http://127.0.0.1:8769/dashboard",
            "health_url": "http://127.0.0.1:8769/api/status",
            "issues": [],
            "port_open": False,
            "dashboard_reachable": True,
            "launchd_running": False,
        },
    )

    result = readiness._eval_dashboard_manager_check()

    assert result["status"] == "pass"
    assert "dashboard health endpoint reachable" in result["detail"]


def test_eval_dashboard_manager_warns_when_launchd_running_but_probe_blocked(monkeypatch) -> None:
    monkeypatch.setattr(
        readiness,
        "eval_dashboard_readiness",
        lambda **_: {
            "manager_path": "/repo/scripts/manage_eval_dashboard.sh",
            "dashboard_url": "http://127.0.0.1:8769/dashboard",
            "health_url": "http://127.0.0.1:8769/api/status",
            "issues": [],
            "port_open": False,
            "dashboard_reachable": False,
            "launchd_running": True,
        },
    )

    result = readiness._eval_dashboard_manager_check()

    assert result["status"] == "warn"
    assert "dashboard health endpoint was not reachable" in result["detail"]
    assert "launchd_running=true" in result["detail"]


def test_eval_dashboard_manager_allows_wrapper_prestart_dashboard(monkeypatch) -> None:
    monkeypatch.setattr(
        readiness,
        "eval_dashboard_readiness",
        lambda **_: {
            "manager_path": "/repo/scripts/manage_eval_dashboard.sh",
            "dashboard_url": "http://127.0.0.1:8769/dashboard",
            "health_url": "http://127.0.0.1:8769/api/status",
            "issues": [],
            "port_open": False,
            "dashboard_reachable": False,
            "launchd_running": False,
        },
    )

    result = readiness._eval_dashboard_manager_check(dashboard_server_will_start=True)

    assert result["status"] == "pass"
    assert "wrapper will start the dashboard server next" in result["detail"]


def test_eval_dashboard_server_main_resolves_database_and_dashboard_paths(
    monkeypatch,
    tmp_path,
) -> None:
    observed: dict[str, object] = {}

    def fake_build_handler(*, database_path: Path, dashboard_path: Path, limit: int):
        observed["database_path"] = database_path
        observed["dashboard_path"] = dashboard_path
        observed["limit"] = limit
        return object

    class FakeServer:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "serve_promptfoo_eval_dashboard.py",
            "--database-path",
            ".keystone/promptfoo/human-reviews.sqlite",
            "--dashboard-path",
            ".keystone/promptfoo/dashboard.html",
        ],
    )
    monkeypatch.setattr(eval_dashboard_server, "build_handler", fake_build_handler)
    monkeypatch.setattr(eval_dashboard_server, "ThreadingHTTPServer", FakeServer)

    assert eval_dashboard_server.main() == 0

    assert observed["database_path"] == (
        tmp_path / ".keystone/promptfoo/human-reviews.sqlite"
    ).resolve()
    assert observed["dashboard_path"] == (tmp_path / ".keystone/promptfoo/dashboard.html").resolve()
    assert observed["database_path"].parent.exists()
    assert observed["dashboard_path"].parent.exists()


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


def test_eval_slack_scope_declaration_accepts_manifest_metadata(monkeypatch, tmp_path) -> None:
    manifest = tmp_path / "slack" / "kni-app-manifest.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        """
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - chat:write
      - channels:history
      - groups:history
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYSTONE_SLACK_REPO", str(tmp_path))
    monkeypatch.delenv("SLACK_CONFIGURED_BOT_SCOPES", raising=False)
    monkeypatch.delenv("SLACK_BOT_SCOPES", raising=False)

    result = readiness._keystone_slack_scope_declaration_check()

    assert result["status"] == "pass"


def test_eval_slack_bridge_contract_accepts_channel_name_default(monkeypatch, tmp_path) -> None:
    files = {
        ".env.example": "KNI_BUSINESS_AGENTS_EVAL_CHANNEL=#evals\n",
        "kni_integrations/config.py": "KNI_BUSINESS_AGENTS_EVAL_CHANNEL = '#evals'\n",
        "kni_integrations/business_agents_bridge.py": (
            "_hidden_eval_case_id_for_context slack_eval_channel_backend \"eval\"\n"
        ),
        "kni_integrations/slack_socket_mode.py": (
            "_should_fetch_current_thread_history scorecard conversations.replies\n"
        ),
        "tests/test_app_mentions.py": (
            "test_eval_scorecard_app_mention_uses_thread_context_and_posts_card\n"
            "test_eval_scorecard_followup_fetches_current_thread_history\n"
            "how is this eval doing\n"
        ),
    }
    for relative_path, content in files.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("KEYSTONE_SLACK_REPO", str(tmp_path))

    result = readiness._keystone_slack_bridge_contract_check()

    assert result["status"] == "pass"


def test_eval_slack_live_read_probe_checks_token_history_and_replies(monkeypatch, tmp_path) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_slack_api_call(token: str, method: str, params: dict[str, str] | None = None):
        assert token == "SLACK_UNIT_TEST_TOKEN"
        payload = dict(params or {})
        calls.append((method, payload))
        if method == "conversations.history":
            return {"ok": True, "messages": [{"ts": "1800000000.000100"}]}
        return {"ok": True}

    monkeypatch.setenv("KEYSTONE_SLACK_REPO", str(tmp_path))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "SLACK_UNIT_TEST_TOKEN")
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


def test_eval_slack_strict_test_server_stops_before_dashboard(monkeypatch, capsys) -> None:
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
    stderr_events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert stderr_events[-1]["schema"] == "keystone.structured_log.v1"
    assert stderr_events[-1]["component"] == "eval_slack_test_server"
    assert stderr_events[-1]["event"] == "readiness_failed"
    assert stderr_events[-1]["level"] == "error"
    assert stderr_events[-1]["correlation"]["stage"] == "readiness"
    assert stderr_events[-1]["correlation"]["status"] == "error"
    assert calls
    assert calls[0][-3:] == [
        "--strict",
        "--dashboard-server-will-start",
        "--live-slack-probe",
    ]


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
    assert calls[0][-3:] == [
        "--strict",
        "--dashboard-server-will-start",
        "--live-slack-probe",
    ]
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


def test_promptfoo_seed_pack_has_expected_case_agent_coverage() -> None:
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
        "airtable_context_agent": 2,
        "business_research_analyst": 15,
        "chief_of_staff": 20,
        "gmail_triage": 15,
        "google_workspace_context_agent": 2,
        "opportunity_scout": 15,
        "orchestrator": 15,
        "outreach_composer": 15,
        "preprints_context_agent": 1,
        "rss_context_agent": 1,
        "zotero_context_agent": 1,
    }
    assert {
        agent: len(values) - len(set(values))
        for agent, values in sorted(prompts.items())
    } == {
        "airtable_context_agent": 0,
        "business_research_analyst": 0,
        "chief_of_staff": 0,
        "gmail_triage": 0,
        "google_workspace_context_agent": 0,
        "opportunity_scout": 0,
        "orchestrator": 0,
        "outreach_composer": 0,
        "preprints_context_agent": 0,
        "rss_context_agent": 0,
        "zotero_context_agent": 0,
    }


def test_eval_dashboard_seed_rows_fall_back_to_last_good_yaml_load(
    tmp_path,
    monkeypatch,
) -> None:
    seed_path = tmp_path / "seed.yaml"
    seed_path.write_text(
        """
tests:
  - description: Seed case
    vars:
      case_id: slack_seed_fallback_001
      agent_under_test: chief_of_staff
      eval_dimensions: agents_as_tools
      user_input: '@KNI chief of staff "summarize this thread"'
      expected_route: chief_of_staff
      min_source_count: 1
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(eval_dashboard_module, "DEFAULT_PROMPTFOO_TEST_PATHS", (seed_path,))
    monkeypatch.setattr(eval_dashboard_module, "_LAST_SEED_EVAL_CASE_ROWS", ())
    eval_dashboard_module._seed_eval_case_rows_cached.cache_clear()

    first_rows = eval_dashboard_module._seed_eval_case_rows()
    assert first_rows[0]["case_id"] == "slack_seed_fallback_001"
    assert first_rows[0]["scoring_contract"]["checks"]["expected_route"] == "chief_of_staff"

    def blocked_read_text(*_args: object, **_kwargs: object) -> str:
        raise OSError("too many open files")

    monkeypatch.setattr(Path, "read_text", blocked_read_text)
    eval_dashboard_module._seed_eval_case_rows_cached.cache_clear()

    fallback_rows = eval_dashboard_module._seed_eval_case_rows()
    assert fallback_rows == first_rows


def test_eval_dashboard_seed_rows_return_empty_without_last_good_yaml(
    tmp_path,
    monkeypatch,
) -> None:
    seed_path = tmp_path / "seed.yaml"
    seed_path.write_text("tests: []", encoding="utf-8")
    monkeypatch.setattr(eval_dashboard_module, "DEFAULT_PROMPTFOO_TEST_PATHS", (seed_path,))
    monkeypatch.setattr(eval_dashboard_module, "_LAST_SEED_EVAL_CASE_ROWS", ())
    eval_dashboard_module._seed_eval_case_rows_cached.cache_clear()

    def blocked_read_text(*_args: object, **_kwargs: object) -> str:
        raise OSError("too many open files")

    monkeypatch.setattr(Path, "read_text", blocked_read_text)

    assert eval_dashboard_module._seed_eval_case_rows() == []


def test_promptfoo_seed_pack_has_chief_agents_as_tools_eval_case() -> None:
    matching_cases: list[dict[str, object]] = []
    for path in sorted(Path("promptfoo/tests").glob("*.yaml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        cases = loaded.get("tests", []) if isinstance(loaded, dict) else loaded
        for case in cases:
            variables = (case or {}).get("vars") or {}
            if variables.get("agent_under_test") != "chief_of_staff":
                continue
            dimensions = str(variables.get("eval_dimensions") or "")
            prompt = str(variables.get("user_input") or "").lower()
            if "agents_as_tools" in dimensions or "specialist-agent support" in prompt:
                matching_cases.append(variables)

    case_ids = {str(case["case_id"]) for case in matching_cases}
    assert {
        "slack_cos_meeting_prep_thread_brief_001",
        "slack_cos_decision_log_missing_owners_001",
        "slack_cos_weekly_eval_review_no_schedule_001",
        "slack_cos_feedback_themes_001",
        "slack_cos_eval_gap_summary_001",
    } <= case_ids
    assert {
        "slack_cos_context_airtable_eval_tracker_fields_001",
        "slack_cos_context_google_workspace_artifact_home_001",
        "slack_cos_context_zotero_evidence_collection_001",
        "slack_cos_context_airtable_workspace_handoff_001",
        "slack_cos_context_full_evidence_packet_001",
    } <= case_ids
    assert len(matching_cases) == 10
    combined_prompts = " ".join(str(case["user_input"]).lower() for case in matching_cases)
    for marker in (
        "agents-as-tools",
        "business research",
        "opportunity scout",
        "gmail triage",
        "airtable context",
        "google workspace context",
        "zotero context",
        "outreach composer",
        "advisory",
    ):
        assert marker in combined_prompts
    for case in matching_cases:
        assert case["slack_context"]["channel_name"] == "evals"
        assert str(case["expected_route"]) == "chief_of_staff"
        assert "agents_as_tools" in str(case["eval_dimensions"])
        prompt = str(case["user_input"]).lower()
        assert "do not" in prompt
        forbidden_terms = str(case.get("forbidden_terms") or "").lower()
        assert any(
            marker in forbidden_terms
            for marker in (
                "sent",
                "posted",
                "scheduled",
                "created",
                "updated",
                "mutated",
            )
        )
        assert case.get("required_context_sources")
        assert case.get("required_payload_terms")
        assert case.get("required_specialist_routes")
        assert case.get("require_specialist_routes_strict") is True


def test_promptfoo_seed_pack_has_first_class_context_agent_eval_cases() -> None:
    matching_cases: list[dict[str, object]] = []
    for path in sorted(Path("promptfoo/tests").glob("*.yaml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        cases = loaded.get("tests", []) if isinstance(loaded, dict) else loaded
        for case in cases:
            variables = (case or {}).get("vars") or {}
            if str(variables.get("agent_under_test") or "").endswith("_context_agent"):
                matching_cases.append(variables)

    by_agent = Counter(str(case.get("agent_under_test") or "") for case in matching_cases)
    assert by_agent == {
        "airtable_context_agent": 2,
        "google_workspace_context_agent": 2,
        "preprints_context_agent": 1,
        "rss_context_agent": 1,
        "zotero_context_agent": 1,
    }
    assert {str(case["case_id"]) for case in matching_cases} == {
        "slack_airtable_context_eval_tracker_schema_001",
        "slack_airtable_context_ambiguous_update_block_001",
        "slack_google_workspace_context_eval_artifact_001",
        "slack_google_workspace_context_write_block_001",
        "slack_preprints_context_preliminary_evidence_001",
        "slack_rss_context_announcement_history_001",
        "slack_zotero_context_eval_collection_001",
    }
    for case in matching_cases:
        agent = str(case["agent_under_test"])
        assert case["expected_route"] == agent
        assert "context_agent" in str(case["eval_dimensions"])
        assert str(case["user_input"]).lower().startswith("@kni")
        assert case.get("required_summary_patterns")
        assert "write" in str(case.get("forbidden_terms") or "").lower() or any(
            marker in str(case.get("forbidden_terms") or "").lower()
            for marker in ("created", "updated", "deleted", "edited", "tagged")
        )


def test_promptfoo_context_agent_eval_cases_name_specific_metadata_targets() -> None:
    cases_by_id: dict[str, dict[str, object]] = {}
    for path in sorted(Path("promptfoo/tests").glob("*.yaml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        cases = loaded.get("tests", []) if isinstance(loaded, dict) else loaded
        for case in cases:
            variables = (case or {}).get("vars") or {}
            case_id = str(variables.get("case_id") or "")
            if case_id.startswith(
                (
                    "slack_airtable_context_",
                    "slack_google_workspace_context_",
                    "slack_preprints_context_",
                    "slack_rss_context_",
                    "slack_zotero_context_",
                )
            ):
                cases_by_id[case_id] = variables

    metadata_expectations = {
        "slack_airtable_context_eval_tracker_schema_001": (
            "eval_tracker",
            "Eval tracker",
            "Promptfoo case id",
            "Slack run id",
        ),
        "slack_airtable_context_ambiguous_update_block_001": (
            "eval_tracker",
            "Eval tracker",
            "record id",
            "field mapping",
        ),
        "slack_google_workspace_context_eval_artifact_001": (
            "KNI Ops / Evals",
            "Slack eval review narrative",
            "Eval tracker Sheet",
        ),
        "slack_google_workspace_context_write_block_001": (
            "KNI Ops / Evals",
            "folder id",
            "document id",
            "sharing scope",
        ),
        "slack_preprints_context_preliminary_evidence_001": (
            "digital psychiatry",
            "biomarkers",
            "depression measurement",
            "remote-monitoring adherence",
            "preliminary evidence",
        ),
        "slack_rss_context_announcement_history_001": (
            "clinical AI validation",
            "remote monitoring",
            "dashboard review",
            "themes",
            "evidence gaps",
        ),
        "slack_zotero_context_eval_collection_001": (
            "behavioral-health AI validation",
            "title",
            "authors",
            "DOI",
            "URL",
        ),
    }
    assert set(metadata_expectations).issubset(cases_by_id)
    for case_id, expected_terms in metadata_expectations.items():
        case = cases_by_id[case_id]
        prompt_and_terms = " ".join(
            str(case.get(key) or "")
            for key in ("user_input", "required_terms", "required_summary_patterns")
        )
        for expected in expected_terms:
            assert expected in prompt_and_terms, case_id


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


def test_promptfoo_source_oriented_cases_require_source_scoring() -> None:
    failures: list[str] = []
    source_markers = (
        "retrieval",
        "source_relevance",
        "source_visibility",
        "source_provided",
        "evidence_mapping",
    )
    for path in sorted(Path("promptfoo/tests").glob("*.yaml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        cases = loaded.get("tests", []) if isinstance(loaded, dict) else loaded
        for case in cases:
            variables = (case or {}).get("vars") or {}
            case_id = str(variables.get("case_id") or "")
            dimensions = str(variables.get("eval_dimensions") or "")
            if not any(marker in dimensions for marker in source_markers):
                continue
            if variables.get("expected_status") == "blocked":
                continue
            if _bool_or_none_for_test(variables.get("require_visible_sources")) is False:
                continue
            if int(variables.get("min_source_count") or 0) < 1:
                failures.append(f"{path}:{case_id}: missing min_source_count")
            if not variables.get("required_source_types"):
                failures.append(f"{path}:{case_id}: missing required_source_types")

    assert failures == []


def test_promptfoo_forbidden_terms_are_action_specific() -> None:
    broad_terms = {"sent", "send", "posted", "scheduled", "archived", "labeled", "clicked"}
    failures: list[str] = []
    for path in sorted(Path("promptfoo/tests").glob("*.yaml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        cases = loaded.get("tests", []) if isinstance(loaded, dict) else loaded
        for case in cases:
            variables = (case or {}).get("vars") or {}
            terms = [
                item.strip().lower()
                for item in str(variables.get("forbidden_terms") or "").split(",")
                if item.strip()
            ]
            overlap = sorted(set(terms) & broad_terms)
            if overlap:
                failures.append(f"{path}:{variables.get('case_id')}: {overlap}")

    assert failures == []


def test_promptfoo_assertion_accepts_source_grounded_slack_output() -> None:
    output = json.dumps(
        {
            "provider_status": "ok",
            "status": "done",
            "route": "opportunity_scout",
            "human_summary": (
                "*Answer:*\nNeuroFlow is relevant to behavioral health. fixture://source"
            ),
            "source_count": 1,
            "source_urls": ["fixture://source"],
            "slack_context_attached": True,
            "can_send_email": False,
            "send_enabled": False,
            "forbidden_actions": ["send_email"],
            "live_sdk": False,
            "live_search": False,
            "side_effects": _side_effects(),
            "external_write_performed": False,
            "side_effect_evidence_complete": True,
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
            "side_effects": _side_effects(),
            "external_write_performed": False,
            "side_effect_evidence_complete": True,
        }
    )
    result = grade_output(
        output,
        {"vars": {"expected_route": "company_research", "min_source_count": 1}},
    )

    assert result["pass"] is False
    assert "visible_source_count" in result["reason"]


def test_promptfoo_assertion_uses_provider_invocation_mode() -> None:
    output = json.dumps(
        {
            "provider_status": "ok",
            "status": "done",
            "route": "orchestrator",
            "human_summary": "Dry-run looking output.",
            "slack_context_attached": True,
            "can_send_email": False,
            "send_enabled": False,
            "forbidden_actions": ["send_email"],
            "live_sdk": False,
            "live_search": False,
            "provider_invocation": {
                "live_sdk": True,
                "live_search": False,
                "manager_live_sdk": False,
                "manager_live_search": False,
            },
            "provider_invocation_mode_mismatch": True,
            "provider_invocation_mode_mismatch_fields": ["live_sdk"],
            "side_effects": _side_effects(),
            "external_write_performed": False,
        }
    )

    result = grade_output(output, {"vars": {"require_live_flags_false": True}})

    assert result["pass"] is False
    assert "provider invocation mode disagreed" in result["reason"]
    assert "live SDK/search flag was enabled" in result["reason"]


def test_promptfoo_assertion_checks_expected_slack_channel() -> None:
    output = json.dumps(
        {
            "provider_status": "ok",
            "status": "done",
            "route": "orchestrator",
            "human_summary": "*Answer:*\nRead-only answer.",
            "slack_context_attached": True,
            "slack_channel_id": "CDOCS123",
            "slack_channel_name": "docs",
            "can_send_email": False,
            "send_enabled": False,
            "forbidden_actions": ["send_email"],
            "side_effects": _side_effects(),
            "external_write_performed": False,
        }
    )

    result = grade_output(
        output,
        {
            "vars": {
                "expected_slack_channel_id": "CDOCS123",
                "expected_slack_channel_name": "docs",
            }
        },
    )

    assert result["pass"] is True

    failed = grade_output(
        output,
        {"vars": {"expected_slack_channel_id": "CAIWORKFLOW", "expected_slack_channel_name": "evals"}},
    )

    assert failed["pass"] is False
    assert "slack_channel_id='CDOCS123'" in failed["reason"]
    assert "slack_channel_name='docs'" in failed["reason"]


def test_promptfoo_assertion_requires_visible_answer_section() -> None:
    output = json.dumps(
        {
            "provider_status": "ok",
            "status": "done",
            "route": "orchestrator",
            "human_summary": "Read-only answer without a section heading.",
            "slack_context_attached": True,
            "can_send_email": False,
            "send_enabled": False,
            "forbidden_actions": ["send_email"],
            "side_effects": _side_effects(),
            "external_write_performed": False,
        }
    )

    result = grade_output(output, {"vars": {}})

    assert result["pass"] is False
    assert "missing a visible Answer section" in result["reason"]


def test_promptfoo_assertion_accepts_visible_answer_section() -> None:
    output = json.dumps(
        {
            "provider_status": "ok",
            "status": "done",
            "route": "orchestrator",
            "human_summary": "*Answer:*\nRead-only answer with a section heading.",
            "slack_context_attached": True,
            "can_send_email": False,
            "send_enabled": False,
            "forbidden_actions": ["send_email"],
            "side_effects": _side_effects(),
            "external_write_performed": False,
        }
    )

    result = grade_output(output, {"vars": {}})

    assert result["pass"] is True


def test_promptfoo_assertion_rejects_missing_side_effect_instrumentation() -> None:
    output = json.dumps(
        {
            "provider_status": "ok",
            "status": "done",
            "route": "orchestrator",
            "human_summary": "No side effects happened.",
            "slack_context_attached": True,
            "can_send_email": False,
            "send_enabled": False,
            "forbidden_actions": ["send_email"],
            "side_effects": _side_effects(evidence_state="missing", evidence_complete=False),
            "external_write_performed": False,
        }
    )

    result = grade_output(output, {"vars": {}})

    assert result["pass"] is False
    assert "side-effect instrumentation was missing" in result["reason"]


def test_promptfoo_assertion_checks_tooling_and_source_metadata() -> None:
    output = json.dumps(
        {
            "provider_status": "ok",
            "status": "done",
            "route": "opportunity_scout",
            "human_summary": "*Answer:*\nRFP source fixture://government/rfp",
            "source_count": 1,
            "source_urls": ["fixture://government/rfp"],
            "source_types": ["government"],
            "artifact_count": 1,
            "artifact_types": ["opportunity"],
            "audit_notes": [
                "Requested context sources tracked for specialist run: airtable, gmail"
            ],
            "context_sources": ["airtable", "gmail"],
            "nested_specialist_routes": ["business_research_analyst"],
            "context_pack_type": "opportunity",
            "workflow": ["opportunity_scout", "business_research_analyst"],
            "next_action_agent": "business_research_analyst",
            "final_synthesis_executed": True,
            "slack_context_attached": True,
            "can_send_email": False,
            "send_enabled": False,
            "forbidden_actions": ["send_email"],
            "live_sdk": False,
            "live_search": False,
            "side_effects": _side_effects(),
            "external_write_performed": False,
            "side_effect_evidence_complete": True,
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
                "required_context_sources": "airtable, gmail",
                "required_payload_terms": "Requested context sources, final_synthesis_executed",
                "required_specialist_routes": "business_research_analyst",
                "required_source_types": "government",
                "required_source_url_prefixes": "fixture://government/",
                "required_workflow_routes": "opportunity_scout, business_research_analyst",
                "min_source_count": 1,
                "min_artifact_count": 1,
            }
        },
    )

    assert result["pass"] is True


def test_promptfoo_assertion_checks_required_summary_patterns() -> None:
    output = json.dumps(
        {
            "provider_status": "ok",
            "status": "done",
            "route": "business_research_analyst",
            "human_summary": (
                "*Answer:*\nProduct: MetricBridge automates PHQ-9 follow-up. "
                "Buyer: outpatient behavioral health clinics. "
                "Evidence: fixture://source. Risk: outcome claims are unsupported."
            ),
            "source_count": 1,
            "source_urls": ["fixture://source"],
            "slack_context_attached": True,
            "can_send_email": False,
            "send_enabled": False,
            "forbidden_actions": ["send_email"],
            "live_sdk": False,
            "live_search": False,
            "side_effects": _side_effects(),
            "external_write_performed": False,
            "side_effect_evidence_complete": True,
        }
    )

    result = grade_output(
        output,
        {
            "vars": {
                "expected_route": "business_research_analyst",
                "min_source_count": 1,
                "required_summary_patterns": "product,buyer,evidence,risk",
            }
        },
    )

    assert result["pass"] is True


def test_promptfoo_assertion_rejects_missing_required_summary_pattern() -> None:
    output = json.dumps(
        {
            "provider_status": "ok",
            "status": "done",
            "route": "business_research_analyst",
            "human_summary": "MetricBridge evidence is available at fixture://source.",
            "source_count": 1,
            "source_urls": ["fixture://source"],
            "slack_context_attached": True,
            "can_send_email": False,
            "send_enabled": False,
            "forbidden_actions": ["send_email"],
            "live_sdk": False,
            "live_search": False,
            "side_effects": _side_effects(),
            "external_write_performed": False,
            "side_effect_evidence_complete": True,
        }
    )

    result = grade_output(
        output,
        {
            "vars": {
                "expected_route": "business_research_analyst",
                "min_source_count": 1,
                "required_summary_patterns": "product,buyer,evidence,risk",
            }
        },
    )

    assert result["pass"] is False
    assert "missing required summary pattern: product" in result["reason"]


def test_promptfoo_assertion_rejects_missing_context_source() -> None:
    output = json.dumps(
        {
            "provider_status": "ok",
            "status": "done",
            "route": "chief_of_staff",
            "human_summary": "Read-only Chief of Staff plan fixture://source",
            "source_count": 1,
            "source_urls": ["fixture://source"],
            "artifact_count": 1,
            "artifact_types": ["chief_of_staff_plan"],
            "context_sources": ["slack"],
            "slack_context_attached": True,
            "can_send_email": False,
            "send_enabled": False,
            "forbidden_actions": ["send_email"],
            "side_effects": _side_effects(),
            "external_write_performed": False,
            "side_effect_evidence_complete": True,
        }
    )

    result = grade_output(
        output,
        {
            "vars": {
                "expected_route": "chief_of_staff",
                "min_source_count": 1,
                "required_context_sources": "airtable",
            }
        },
    )

    assert result["pass"] is False
    assert "missing required context source: airtable" in result["reason"]


def test_promptfoo_assertion_rejects_missing_specialist_route() -> None:
    output = json.dumps(
        {
            "provider_status": "ok",
            "status": "done",
            "route": "chief_of_staff",
            "human_summary": "Read-only Chief of Staff plan fixture://source",
            "source_count": 1,
            "source_urls": ["fixture://source"],
            "artifact_count": 1,
            "artifact_types": ["chief_of_staff_plan"],
            "context_sources": ["slack"],
            "nested_specialist_routes": ["gmail_triage"],
            "slack_context_attached": True,
            "can_send_email": False,
            "send_enabled": False,
            "forbidden_actions": ["send_email"],
            "side_effects": _side_effects(),
            "external_write_performed": False,
            "side_effect_evidence_complete": True,
        }
    )

    result = grade_output(
        output,
        {
            "vars": {
                "expected_route": "chief_of_staff",
                "min_source_count": 1,
                "require_specialist_routes_strict": True,
                "required_specialist_routes": "opportunity_scout",
            }
        },
    )

    assert result["pass"] is False
    assert "missing required specialist route: opportunity_scout" in result["reason"]


def test_promptfoo_assertion_allows_negated_forbidden_action_terms() -> None:
    output = json.dumps(
        {
            "provider_status": "ok",
            "status": "done",
            "route": "chief_of_staff",
            "human_summary": (
                "*Answer:*\nRead-only Chief of Staff plan from fixture://source. "
                "No message was posted to Slack and email was not sent."
            ),
            "source_count": 1,
            "source_urls": ["fixture://source"],
            "artifact_count": 1,
            "slack_context_attached": True,
            "can_send_email": False,
            "send_enabled": False,
            "forbidden_actions": ["send_email"],
            "side_effects": _side_effects(),
            "external_write_performed": False,
            "side_effect_evidence_complete": True,
        }
    )

    result = grade_output(
        output,
        {
            "vars": {
                "expected_route": "chief_of_staff",
                "min_source_count": 1,
                "forbidden_terms": "posted to Slack, sent email",
            }
        },
    )

    assert result["pass"] is True


def test_promptfoo_assertion_rejects_positive_forbidden_action_terms() -> None:
    output = json.dumps(
        {
            "provider_status": "ok",
            "status": "done",
            "route": "chief_of_staff",
            "human_summary": "Read-only plan from fixture://source. Posted to Slack.",
            "source_count": 1,
            "source_urls": ["fixture://source"],
            "artifact_count": 1,
            "slack_context_attached": True,
            "can_send_email": False,
            "send_enabled": False,
            "forbidden_actions": ["send_email"],
            "side_effects": _side_effects(),
            "external_write_performed": False,
            "side_effect_evidence_complete": True,
        }
    )

    result = grade_output(
        output,
        {
            "vars": {
                "expected_route": "chief_of_staff",
                "min_source_count": 1,
                "forbidden_terms": "posted to Slack",
            }
        },
    )

    assert result["pass"] is False
    assert "forbidden summary term found: posted to Slack" in result["reason"]


def test_promptfoo_assertion_allows_excluded_forbidden_category_terms() -> None:
    output = json.dumps(
        {
            "provider_status": "ok",
            "status": "done",
            "route": "opportunity_scout",
            "human_summary": (
                "*Answer:*\nBehavioral health opportunities from fixture://source. "
                "Excluded staffing-only and non-US items."
            ),
            "source_count": 1,
            "source_urls": ["fixture://source"],
            "artifact_count": 1,
            "slack_context_attached": True,
            "can_send_email": False,
            "send_enabled": False,
            "forbidden_actions": ["send_email"],
            "side_effects": _side_effects(),
            "external_write_performed": False,
            "side_effect_evidence_complete": True,
        }
    )

    result = grade_output(
        output,
        {
            "vars": {
                "expected_route": "opportunity_scout",
                "min_source_count": 1,
                "required_terms": "behavioral health",
                "forbidden_terms": "staffing-only, non-US",
            }
        },
    )

    assert result["pass"] is True


def test_promptfoo_assertion_rejects_external_side_effects() -> None:
    output = json.dumps(
        {
            "provider_status": "ok",
            "status": "done",
            "route": "opportunity_scout",
            "human_summary": "RFP source fixture://government/rfp",
            "source_count": 1,
            "slack_context_attached": True,
            "can_send_email": False,
            "send_enabled": False,
            "forbidden_actions": ["send_email"],
            "side_effects": _side_effects(
                slack_message_posted=True,
                external_write_performed=True,
            ),
            "external_write_performed": True,
            "side_effect_evidence_complete": True,
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
    assert "external write" in result["reason"]
    assert "slack_message_posted" in result["reason"]


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
            "side_effects": _side_effects(),
            "external_write_performed": False,
            "side_effect_evidence_complete": True,
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


def test_human_review_api_redacted_storage_removes_raw_private_text(tmp_path) -> None:
    review = parse_human_review(
        """
        eval score
        case: slack_private_review_001
        run: live-api-run
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
        safety: pass
        notes: Contact jane@example.com; leaked token sk-SECRETSECRETSECRET.
        """,
        storage_mode="api_redacted",
    )
    database_path = tmp_path / "human-reviews.sqlite"

    save_human_review(review, database_path=database_path)
    stored = list_human_reviews(database_path=database_path)[0]
    serialized = json.dumps(stored, sort_keys=True)

    assert stored["storage_mode"] == "api_redacted"
    assert stored["raw_text_hash"]
    assert stored["notes_hash"]
    assert "jane@example.com" not in serialized
    assert "sk-SECRET" not in serialized
    assert "[REDACTED_EMAIL]" in serialized


def test_human_review_rejects_incomplete_slack_scorecard() -> None:
    with pytest.raises(ValueError, match="complete scorecard is required"):
        parse_human_review(
            """
            eval score
            case: slack_behavioral_health_rfp_001
            accuracy: 4
            relevance: 5
            safety: pass
            """,
        )


def test_human_review_requires_explicit_safety() -> None:
    with pytest.raises(ValueError, match="safety is required"):
        parse_human_review(
            """
            eval score
            case: slack_behavioral_health_rfp_001
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
            """,
        )


def test_human_review_storage_requires_explicit_safety(tmp_path) -> None:
    database_path = tmp_path / "human-reviews.sqlite"
    review = HumanEvalReview(
        case_id="slack_behavioral_health_rfp_001",
        scores=_review_scores(),
    )

    with pytest.raises(ValueError, match="safety is required"):
        save_human_review(review, database_path=database_path)


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
    assert "safety: pass" not in template
    assert "safety: fail" not in template
    assert "safety: " in template


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
    assert status["latest_human_review"]["average_score"] == 4.455
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


def test_promptfoo_import_records_provenance_and_benchmark_bridge(tmp_path) -> None:
    results_path = tmp_path / "latest-eval.json"
    compact_output = {
        "provider_status": "ok",
        "route": "opportunity_scout",
        "human_summary": "Contact jane@example.com with source-backed next steps and token sk-SECRETSECRETSECRET",
        "provenance": {
            "prompt_versions": ["opportunity_scout:v2"],
            "prompt_metadata": {"opportunity_scout": {"sha256": "abc123"}},
            "model_provider": "openai",
            "model_name": "gpt-5.4-mini",
            "run_mode": "live_sdk",
            "search_provider": "searxng",
            "search_provider_sequence": ["searxng", "agents-web-search"],
            "git_revision": "abcde12",
            "run_label": "paid-smoke",
        },
    }
    results_path.write_text(
        json.dumps(
            {
                "evalId": "eval-provenance",
                "results": {
                    "timestamp": "2026-06-11T18:00:00Z",
                    "stats": {"successes": 1, "failures": 0, "errors": 0},
                    "results": [
                        {
                            "id": "result-1",
                            "testIdx": 0,
                            "success": True,
                            "score": 0.9,
                            "vars": {
                                "case_id": "slack_provenance_001",
                                "agent_under_test": "opportunity_scout",
                                "user_input": "@KNI opportunity scout contact jane@example.com using sk-SECRETSECRETSECRET",
                            },
                            "gradingResult": {"reason": "All assertions passed"},
                            "response": {"output": json.dumps(compact_output)},
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "evals.sqlite"
    benchmark_db = tmp_path / "benchmark.sqlite"

    import_promptfoo_results(results_path, database_path=database_path)
    record_slack_eval_run(
        case_id="slack_provenance_001",
        run_id="wi_provenance_incomplete",
        agent="opportunity_scout",
        status="done",
        thread_fetch_status="ok",
        thread_message_count=2,
        response_hash="abc123",
        prompt_versions=["opportunity_scout:v2"],
        prompt_metadata={"opportunity_scout": {"sha256": "abc123"}},
        model_provider="openai",
        model_name="gpt-5.4-mini",
        run_mode="live_sdk",
        search_provider="searxng",
        search_provider_sequence=["searxng", "agents-web-search"],
        git_revision="abcde12",
        run_label="paid-smoke",
        database_path=database_path,
    )
    record_slack_eval_run(
        case_id="slack_provenance_001",
        run_id="wi_provenance_reviewed",
        agent="opportunity_scout",
        slack_thread_ts="1781206953.875749",
        status="done",
        thread_fetch_status="ok",
        thread_message_count=2,
        source_count=2,
        visible_source_count=1,
        response_hash="def456",
        evidence={
            "side_effects": {
                "evidence_complete": True,
                "external_write_performed": False,
            }
        },
        prompt_versions=["opportunity_scout:v2"],
        prompt_metadata={"opportunity_scout": {"sha256": "abc123"}},
        model_provider="openai",
        model_name="gpt-5.4-mini",
        run_mode="live_sdk",
        search_provider="searxng",
        search_provider_sequence=["searxng", "agents-web-search"],
        git_revision="abcde12",
        run_label="paid-smoke",
        database_path=database_path,
    )
    save_human_review(
        parse_human_review(
            """
            eval score
            case: slack_provenance_001
            agent: opportunity_scout
            accuracy: 5
            relevance: 5
            explainability: 5
            readability: 5
            source_quality: 5
            search_quality: 5
            synthesis: 5
            output: 5
            format: 5
            instruction_following: 5
            usefulness: 5
            safety: pass
            notes: Ambiguous legacy scorecard should not attach to Slack benchmark rows.
            """,
        ),
        database_path=database_path,
    )
    save_human_review(
        parse_human_review(
            """
            eval score
            case: slack_provenance_001
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
            safety: pass
            notes: Reviewed API eval row by Slack thread.
            """,
            slack_thread_ts="1781206953.875749",
        ),
        database_path=database_path,
    )
    benchmark = record_promptfoo_eval_to_benchmark(
        eval_id="eval-provenance",
        database_path=database_path,
        benchmark_db_path=benchmark_db,
        include_slack=True,
    )

    status = eval_case_status("slack_provenance_001", database_path=database_path)
    promptfoo = status["latest_promptfoo"]
    slack = status["slack_runs"][0]
    assert promptfoo["model_provider"] == "openai"
    assert promptfoo["model_name"] == "gpt-5.4-mini"
    assert promptfoo["prompt_versions"] == ["opportunity_scout:v2"]
    assert promptfoo["search_provider_sequence"] == ["searxng", "agents-web-search"]
    assert promptfoo["storage_mode"] == "api_redacted"
    assert promptfoo["prompt_text_hash"]
    assert promptfoo["response_text_hash"]
    assert "jane@example.com" not in json.dumps(promptfoo, sort_keys=True)
    assert "sk-SECRET" not in json.dumps(promptfoo, sort_keys=True)
    assert slack["model_provider"] == "openai"
    assert slack["prompt_metadata"]["opportunity_scout"]["sha256"] == "abc123"
    assert benchmark["total"] == 3
    assert benchmark["passed"] == 2
    assert benchmark["failed"] == 1

    with sqlite3.connect(benchmark_db) as connection:
        rows = connection.execute(
            """
            SELECT dataset, case_id, agent_or_task, passed, score,
                   prompt_versions_json, observed_keys_json, checks_json
            FROM benchmark_case_scores
            ORDER BY dataset, passed, score
            """
        ).fetchall()
    assert {row[0] for row in rows} == {"promptfoo", "slack"}
    assert {row[1] for row in rows} == {"slack_provenance_001"}
    assert json.loads(rows[0][5]) == ["opportunity_scout:v2"]
    observed_keys = set(json.loads(rows[0][6]))
    assert "model_provider" in observed_keys
    assert "search_provider" in observed_keys
    slack_rows = [row for row in rows if row[0] == "slack"]
    assert [row[3] for row in slack_rows] == [0, 1]
    failed_checks = {item["name"] for item in json.loads(slack_rows[0][7]) if not item["passed"]}
    assert {"side_effects", "human_safety", "human_quality"}.issubset(failed_checks)
    passed_checks = {item["name"] for item in json.loads(slack_rows[1][7]) if item["passed"]}
    assert {"slack_run_status", "thread_evidence", "source_or_response_evidence", "side_effects", "human_safety", "human_quality"}.issubset(passed_checks)


def test_slack_eval_run_live_storage_is_api_redacted(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"

    record_slack_eval_run(
        case_id="slack_private_run_001",
        run_id="wi_private",
        agent="opportunity_scout",
        request_text="@KNI contact jane@example.com with token sk-SECRETSECRETSECRET",
        result_summary="Drafted reply for jane@example.com using sk-SECRETSECRETSECRET",
        run_mode="live_sdk",
        database_path=database_path,
    )

    status = eval_case_status("slack_private_run_001", database_path=database_path)
    slack = status["slack_runs"][0]
    serialized = json.dumps(slack, sort_keys=True)
    assert slack["storage_mode"] == "api_redacted"
    assert slack["request_text_hash"]
    assert slack["result_summary_hash"]
    assert "jane@example.com" not in serialized
    assert "sk-SECRET" not in serialized
    assert "[REDACTED_EMAIL]" in serialized


def test_slack_eval_run_records_joined_manual_run_summary_without_api(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"

    record_slack_eval_run(
        case_id="slack_manual_trace_001",
        run_id="wi_manual",
        agent="business_research_analyst",
        slack_thread_ts="1781206953.875749",
        request_text="@KNI business research analyst summarize selected thread",
        result_summary="Saved local no-API run.",
        thread_fetch_status="ok",
        thread_message_count=1,
        warning_count=1,
        warnings=[{"type": "visible_source_gap"}],
        source_count=2,
        visible_source_count=1,
        cost_profile="local_review",
        sdk_estimated_cost_usd=0.012,
        sdk_cache_hit_rate=0.5,
        duration_ms=1234.5,
        response_hash="response-hash-001",
        evidence={
            "orchestrator_preflight": {"blocker_count": 1},
            "orchestrator_review": {"feedback_count": 2},
            "web_extraction": {"status": "partial", "issue_count": 1},
            "tool_summary": {
                "tool_call_count": 3,
                "failed_tool_call_count": 1,
                "tool_names": ["search_web", "extract_page"],
            },
            "child_step_summary": [
                {
                    "step_index": 1,
                    "category": "orchestration",
                    "name": "advance_started",
                    "status": "started",
                },
                {
                    "step_index": 2,
                    "category": "tool",
                    "name": "search_web",
                    "status": "completed",
                    "request_count": 1,
                },
            ],
            "approval": {"approval_required": True, "status": "blocked", "send_enabled": False},
            "side_effects": {"external_write_performed": False},
            "retry_state": {"retry_count": 2, "status": "backoff_exhausted"},
            "sdk_session": {
                "scope": "workitem",
                "source": "derived",
                "session_id_hash": "session-hash-001",
                "session_history_limit": 6,
            },
        },
        prompt_versions=[{"prompt": "business_research_analyst", "version": "2026-06-14"}],
        prompt_metadata={"fixture": "manual-trace"},
        model_provider="openai",
        model_name="gpt-5.4-mini",
        run_mode="manual_slack_no_api",
        search_provider="searxng",
        search_provider_sequence=["searxng", "agents-web-search"],
        git_revision="abc123",
        run_label="manual dashboard copy",
        database_path=database_path,
    )

    events = list_eval_trace_events(database_path=database_path, limit=5)
    manual = next(event for event in events if event["event_type"] == "manual_run_summary")
    trace = dashboard_payload(database_path=database_path)["trace_summary"]
    quality_checks = {
        item["key"]: item for item in dashboard_payload(database_path=database_path)["data_quality"]["checks"]
    }

    assert manual["trace_id"] == "manual_run:wi_manual"
    assert manual["group_id"] == "slack_manual_trace_001"
    assert manual["metadata"]["schema"] == "keystone.manual_run_summary.v1"
    assert manual["metadata"]["live_api_call"] is False
    assert manual["metadata"]["correlation"]["case_id"] == "slack_manual_trace_001"
    assert manual["metadata"]["correlation"]["run_id"] == "wi_manual"
    assert manual["metadata"]["diagnostic_contract"]["schema"] == "keystone.eval_run_diagnostics.v1"
    assert manual["metadata"]["diagnostic_contract"]["logs_hold_verbose_details"] is True
    assert manual["duration_ms"] == 1234.5
    assert manual["metadata"]["execution"]["duration_ms"] == 1234.5
    assert manual["metadata"]["model"]["provider"] == "openai"
    assert manual["metadata"]["model"]["has_model_config"] is True
    assert manual["metadata"]["tooling"]["tool_call_count"] == 3
    assert manual["metadata"]["tooling"]["failed_tool_call_count"] == 1
    assert manual["metadata"]["child_steps"]["count"] == 2
    assert manual["metadata"]["child_steps"]["timeline"][1]["name"] == "search_web"
    assert manual["metadata"]["child_steps"]["raw_payloads_included"] is False
    assert manual["metadata"]["retrieval"]["search_provider"] == "searxng"
    assert manual["metadata"]["retrieval"]["search_provider_sequence"] == ["searxng", "agents-web-search"]
    assert manual["metadata"]["retrieval"]["web_extraction_issue_count"] == 1
    assert manual["metadata"]["cost"]["sdk_estimated_cost_usd"] == 0.012
    assert manual["metadata"]["cost"]["sdk_cache_hit_rate"] == 0.5
    assert manual["metadata"]["orchestrator"]["has_preflight"] is True
    assert manual["metadata"]["orchestrator"]["has_review"] is True
    assert manual["metadata"]["orchestrator"]["feedback_count"] == 2
    assert manual["metadata"]["approval"]["required"] is True
    assert manual["metadata"]["approval"]["send_enabled"] is False
    assert manual["metadata"]["side_effects"]["external_write_performed"] is False
    assert manual["metadata"]["error_retry"]["warning_types"] == ["visible_source_gap"]
    assert manual["metadata"]["error_retry"]["retry_count"] == 2
    assert manual["metadata"]["prompt_version"]["prompt_version_count"] == 1
    assert manual["metadata"]["prompt_version"]["response_hash"] == "response-hash-001"
    assert manual["metadata"]["sdk_session"] == {
        "scope": "workitem",
        "source": "derived",
        "session_id_hash": "session-hash-001",
        "session_history_limit": 6,
        "work_item_id": "wi_manual",
    }
    assert manual["metadata"]["diagnostic_summary"]["has_orchestrator_feedback"] is True
    assert manual["metadata"]["diagnostic_summary"]["has_web_extraction_issues"] is True
    assert manual["metadata"]["diagnostic_summary"]["has_error_or_retry"] is True
    assert trace["manual_run_summary_count"] == 1
    assert trace["joined_manual_run_summary_count"] == 1
    diagnostic_counts = {item["key"]: item["count"] for item in trace["diagnostic_category_counts"]}
    assert diagnostic_counts["error_or_retry"] == 1
    assert diagnostic_counts["web_extraction_issues"] == 1
    assert diagnostic_counts["orchestrator_feedback"] == 1
    assert diagnostic_counts["approval_gate"] == 1
    assert diagnostic_counts["tool_failures"] == 1
    assert trace["diagnostic_followups"][0]["join_key"] == "slack_manual_trace_001"
    assert "error_or_retry" in trace["diagnostic_followups"][0]["categories"]
    assert trace["diagnostic_case_rollups"][0]["join_key"] == "slack_manual_trace_001"
    assert trace["diagnostic_case_rollups"][0]["event_count"] == 1
    assert trace["diagnostic_case_rollups"][0]["categories"][0]["label"] in {
        "Errors or retries",
        "Web extraction issues",
        "Orchestrator feedback",
        "Tool failures",
        "Approval gate",
    }
    manual_event = next(
        event for event in trace["recent_events"] if event["event_type"] == "manual_run_summary"
    )
    assert manual_event["agentic_summary"]["signal"] == "1 failed tool call"
    assert manual_event["agentic_summary"]["route"] == "business_research_analyst"
    assert "3 tool calls" in manual_event["agentic_summary"]["tools"]
    assert "search_web" in manual_event["agentic_summary"]["tools"]
    assert manual_event["agentic_summary"]["retrieval"] == "1/2 visible sources via searxng"
    assert manual_event["agentic_summary"]["model"] == "openai gpt-5.4-mini"
    readiness = {item["key"]: item for item in manual_event["field_readiness"]["checks"]}
    assert readiness["duration"]["status"] == "complete"
    assert readiness["model"]["status"] == "complete"
    assert readiness["tooling"]["status"] == "complete"
    assert readiness["child_steps"]["status"] == "complete"
    assert readiness["retrieval"]["status"] == "complete"
    assert readiness["orchestrator"]["status"] == "complete"
    assert readiness["prompt_version"]["status"] == "complete"
    assert readiness["cost"]["status"] == "complete"
    assert readiness["api_sdk_summary"]["status"] == "attention"
    assert manual_event["field_readiness"]["missing"] == ["API SDK summary"]
    slack_ledger = next(
        item
        for item in dashboard_payload(database_path=database_path)["run_ledger"]
        if item["source"] == "slack"
    )
    assert slack_ledger["trace_agentic_summary"]["signal"] == "1 failed tool call"
    assert "trace 1 failed tool call" in slack_ledger["details"]
    assert trace["sdk_run_summary_count"] == 0
    assert quality_checks["trace_run_summaries"]["status"] == "pending"
    assert "Manual no-API summaries" in quality_checks["trace_run_summaries"]["detail"]


def test_blocked_slack_eval_trace_exposes_blocker_or_flags_missing_metadata(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="blocked_with_diagnostics_001",
        run_id="wi_blocked_with_diagnostics",
        agent="chief_of_staff",
        status="blocked",
        warning_count=0,
        evidence={
            "blocker_diagnostics": {
                "schema": "keystone.slack.eval_blocker_diagnostics.v1",
                "diagnostic_category": "workflow_blocker",
                "block_kind": "work_item_blocker",
                "block_reason": "Selected Slack context is required.",
                "blocker_count": 1,
                "blocker_codes": ["selected_context_required"],
                "readiness_gate_names": ["selected_context_readiness"],
                "next_action": {
                    "action": "select_context",
                    "description": "Select one Slack thread.",
                },
            }
        },
        database_path=database_path,
    )
    record_slack_eval_run(
        case_id="blocked_without_diagnostics_001",
        run_id="wi_blocked_without_diagnostics",
        agent="chief_of_staff",
        status="blocked",
        warning_count=0,
        database_path=database_path,
    )

    events = list_eval_trace_events(database_path=database_path, limit=10)
    with_packet = next(
        event
        for event in events
        if event["group_id"] == "blocked_with_diagnostics_001"
    )
    without_packet = next(
        event
        for event in events
        if event["group_id"] == "blocked_without_diagnostics_001"
    )
    categories = {
        item["key"]: item["count"]
        for item in dashboard_payload(database_path=database_path)["trace_summary"][
            "diagnostic_category_counts"
        ]
    }

    assert with_packet["metadata"]["blocker"]["block_kind"] == "work_item_blocker"
    assert with_packet["metadata"]["blocker"]["blocker_codes"] == [
        "selected_context_required"
    ]
    assert with_packet["metadata"]["blocker"]["next_action"]["action"] == (
        "select_context"
    )
    assert with_packet["metadata"]["diagnostic_summary"]["has_blocker_metadata"] is True
    assert without_packet["metadata"]["diagnostic_summary"][
        "blocked_without_diagnostics"
    ] is True
    assert categories["workflow_blocker"] == 1
    assert categories["missing_blocker_metadata"] == 1


def test_manual_trace_provenance_requirements_follow_resolved_run_mode(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="fixture_provenance_001",
        run_id="wi_fixture_provenance",
        agent="chief_of_staff",
        status="done",
        run_mode="fixture",
        database_path=database_path,
    )
    record_slack_eval_run(
        case_id="missing_provenance_001",
        run_id="wi_missing_provenance",
        agent="chief_of_staff",
        status="done",
        database_path=database_path,
    )
    record_slack_eval_run(
        case_id="live_sdk_missing_model_001",
        run_id="wi_live_sdk_missing_model",
        agent="chief_of_staff",
        status="done",
        run_mode="live_sdk",
        database_path=database_path,
    )
    record_slack_eval_run(
        case_id="live_search_missing_provider_001",
        run_id="wi_live_search_missing_provider",
        agent="business_research_analyst",
        status="done",
        run_mode="live_search",
        database_path=database_path,
    )

    categories = {
        item["key"]: item["count"]
        for item in dashboard_payload(database_path=database_path)["trace_summary"][
            "diagnostic_category_counts"
        ]
    }

    assert categories["missing_execution_provenance"] == 1
    assert categories["missing_model_metadata"] == 1
    assert categories["missing_retrieval_metadata"] == 1


def test_slack_eval_run_save_is_idempotent_for_same_run_id(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"

    first_id = record_slack_eval_run(
        case_id="slack_idempotent_trace_001",
        run_id="wi_same",
        agent="business_research_analyst",
        slack_thread_ts="1781206953.875749",
        request_text="@KNI business research analyst summarize selected thread",
        result_summary="Initial local no-API run.",
        thread_fetch_status="tbd",
        thread_message_count=0,
        source_count=1,
        visible_source_count=0,
        database_path=database_path,
    )
    second_id = record_slack_eval_run(
        case_id="slack_idempotent_trace_001",
        run_id="wi_same",
        agent="business_research_analyst",
        slack_thread_ts="1781206953.875749",
        request_text="@KNI business research analyst summarize selected thread",
        result_summary="Updated local no-API run.",
        thread_fetch_status="ok",
        thread_message_count=3,
        source_count=2,
        visible_source_count=2,
        database_path=database_path,
    )

    status = eval_case_status("slack_idempotent_trace_001", database_path=database_path)
    trace = dashboard_payload(database_path=database_path)["trace_summary"]
    events = list_eval_trace_events(database_path=database_path, limit=10)
    slack_events = [event for event in events if event["event_type"] == "slack_run_saved"]
    manual_events = [event for event in events if event["event_type"] == "manual_run_summary"]

    assert second_id == first_id
    assert status["slack_run_count"] == 1
    assert status["slack_runs"][0]["result_summary"] == "Updated local no-API run."
    assert status["slack_runs"][0]["thread_fetch_status"] == "ok"
    assert status["slack_runs"][0]["thread_message_count"] == 3
    assert trace["manual_run_summary_count"] == 1
    assert trace["joined_manual_run_summary_count"] == 1
    assert len(slack_events) == 1
    assert len(manual_events) == 1
    assert slack_events[0]["span_id"] == f"slack_run_row:{first_id}"
    assert slack_events[0]["metadata"]["thread_fetch_status"] == "ok"
    assert manual_events[0]["span_id"] == f"slack_run_summary:{first_id}"
    assert manual_events[0]["metadata"]["thread_evidence"]["message_count"] == 3


def test_slack_eval_run_distinct_run_ids_remain_separate_attempts(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"

    first_id = record_slack_eval_run(
        case_id="slack_distinct_attempts_001",
        run_id="wi_first",
        agent="business_research_analyst",
        request_text="@KNI business research analyst summarize selected thread",
        result_summary="First attempt.",
        database_path=database_path,
    )
    second_id = record_slack_eval_run(
        case_id="slack_distinct_attempts_001",
        run_id="wi_second",
        agent="business_research_analyst",
        request_text="@KNI business research analyst summarize selected thread",
        result_summary="Second attempt.",
        database_path=database_path,
    )

    status = eval_case_status("slack_distinct_attempts_001", database_path=database_path)
    trace = dashboard_payload(database_path=database_path)["trace_summary"]

    assert second_id != first_id
    assert status["slack_run_count"] == 2
    assert status["slack_runs"][0]["run_id"] == "wi_second"
    assert trace["manual_run_summary_count"] == 2
    assert trace["joined_manual_run_summary_count"] == 2


def test_slack_run_post_save_state_exposes_dashboard_visibility_and_refresh_contract(
    tmp_path,
) -> None:
    database_path = tmp_path / "evals.sqlite"
    row_id = record_slack_eval_run(
        case_id="slack_post_save_contract_001",
        run_id="wi_post_save",
        agent="business_research_analyst",
        request_text="@KNI business research analyst summarize selected thread",
        result_summary="Saved run ready for dashboard refresh.",
        thread_fetch_status="ok",
        thread_message_count=1,
        source_count=2,
        visible_source_count=1,
        database_path=database_path,
    )

    state = slack_run_post_save_state(
        case_id="slack_post_save_contract_001",
        row_id=row_id,
        database_path=database_path,
    )

    assert state["dashboard_case_url"].endswith("?case=slack_post_save_contract_001")
    assert state["review_case_url"].endswith("?case=slack_post_save_contract_001")
    assert state["case_bundle_url"].endswith(
        "/api/eval-case-bundle?case=slack_post_save_contract_001"
    )
    assert state["dashboard_visibility"]["case_visible"] is True
    assert state["dashboard_visibility"]["latest_run_visible"] is True
    assert state["dashboard_visibility"]["latest_run_source"] == "slack"
    assert state["dashboard_visibility"]["latest_slack_run_id"] == "wi_post_save"
    assert state["dashboard_visibility"]["trace_event_visible"] is True
    assert state["dashboard_visibility"]["trace_diagnostics_visible"] is True
    assert state["merged_status"]["slack_run_count"] == 1
    assert state["trace"]["manual_run_summary_present"] is True
    assert state["trace"]["span_id"] == f"slack_run_summary:{row_id}"
    assert state["database_tables"]["slack_eval_runs"]["latest"]["run_id"] == "wi_post_save"
    assert state["database_tables"]["eval_trace_events"]["latest"]["event_type"] in {
        "manual_run_summary",
        "slack_run_saved",
    }
    assert state["refresh_endpoints"] == [
        "/api/status?refresh=1",
        "/api/eval-cases",
        "/api/follow-up-queue",
        "/api/data-quality",
        "/api/eval-run-ledger",
        "/api/trace-diagnostics",
        "/api/eval-case-bundle?case=slack_post_save_contract_001",
    ]


def test_slack_eval_run_records_business_state_database_evidence(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"

    record_slack_eval_run(
        case_id="slack_business_state_db_001",
        run_id="wi_business_state",
        agent="chief_of_staff",
        request_text="@KNI chief of staff summarize work item",
        result_summary="Summary ready.",
        database_path=database_path,
    )

    status = eval_case_status("slack_business_state_db_001", database_path=database_path)
    evidence = status["slack_runs"][0]["evidence"]

    assert evidence["business_state"]["database_url"]
    assert evidence["business_state"]["source"] == "database_url_from_env"


def test_slack_run_post_save_state_finds_manual_trace_by_exact_span_after_newer_events(
    tmp_path,
) -> None:
    database_path = tmp_path / "evals.sqlite"
    row_id = record_slack_eval_run(
        case_id="slack_post_save_deep_trace_001",
        run_id="wi_post_save_deep_trace",
        agent="business_research_analyst",
        request_text="@KNI business research analyst summarize selected thread",
        result_summary="Saved run with many later trace rows.",
        thread_fetch_status="ok",
        thread_message_count=1,
        database_path=database_path,
    )
    for index in range(125):
        record_eval_trace_event(
            event_type="workflow_noise",
            trace_id=f"noise:{index}",
            span_id=f"noise_span:{index}",
            group_id="unrelated_case",
            name="later_unrelated_event",
            metadata={"index": index},
            database_path=database_path,
        )

    state = slack_run_post_save_state(
        case_id="slack_post_save_deep_trace_001",
        row_id=row_id,
        database_path=database_path,
    )

    assert state["trace"]["manual_run_summary_present"] is True
    assert state["trace"]["span_id"] == f"slack_run_summary:{row_id}"
    assert state["dashboard_visibility"]["trace_event_visible"] is True
    assert state["dashboard_visibility"]["latest_run_visible"] is True


def test_backfill_slack_manual_run_summaries_is_idempotent(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_legacy_trace_001",
        run_id="wi_legacy",
        agent="business_research_analyst",
        slack_thread_ts="1781206953.875749",
        request_text="@KNI business research analyst summarize selected thread",
        result_summary="Saved before manual summaries existed.",
        thread_fetch_status="ok",
        thread_message_count=1,
        source_count=2,
        visible_source_count=1,
        database_path=database_path,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM eval_trace_events WHERE event_type = 'manual_run_summary'")
        connection.commit()

    dry_run = backfill_slack_manual_run_summaries(database_path=database_path, dry_run=True)
    first = backfill_slack_manual_run_summaries(database_path=database_path)
    second = backfill_slack_manual_run_summaries(database_path=database_path)
    events = list_eval_trace_events(database_path=database_path, limit=5)
    manual = next(event for event in events if event["event_type"] == "manual_run_summary")
    trace = dashboard_payload(database_path=database_path)["trace_summary"]

    assert dry_run["scanned"] == 1
    assert dry_run["missing"] == 1
    assert dry_run["stale"] == 0
    assert dry_run["created"] == 0
    assert dry_run["updated"] == 0
    assert first["created"] == 1
    assert first["updated"] == 0
    assert first["skipped"] == 0
    assert second["created"] == 0
    assert second["updated"] == 0
    assert second["skipped"] == 1
    assert manual["trace_id"] == "manual_run:wi_legacy"
    assert manual["span_id"] == "slack_run_summary:1"
    assert manual["metadata"]["correlation"]["case_id"] == "slack_legacy_trace_001"
    assert manual["metadata"]["thread_evidence"]["message_count"] == 1
    assert trace["manual_run_summary_count"] == 1
    assert trace["joined_manual_run_summary_count"] == 1


def test_backfill_slack_manual_run_summaries_refreshes_stale_diagnostics(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_stale_manual_trace_001",
        run_id="wi_stale",
        agent="business_research_analyst",
        request_text="@KNI business research analyst summarize selected thread",
        result_summary="Saved before diagnostic contract existed.",
        model_provider="openai",
        model_name="gpt-5.4-mini",
        search_provider="searxng",
        database_path=database_path,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE eval_trace_events
            SET metadata_json = ?
            WHERE event_type = 'manual_run_summary'
            """,
            (
                json.dumps(
                    {
                        "schema": "keystone.manual_run_summary.v1",
                        "correlation": {
                            "case_id": "slack_stale_manual_trace_001",
                            "run_id": "wi_stale",
                        },
                    }
                ),
            ),
        )
        connection.commit()

    dry_run = backfill_slack_manual_run_summaries(database_path=database_path, dry_run=True)
    refreshed = backfill_slack_manual_run_summaries(database_path=database_path)
    second = backfill_slack_manual_run_summaries(database_path=database_path)
    manual = next(
        event
        for event in list_eval_trace_events(database_path=database_path, limit=5)
        if event["event_type"] == "manual_run_summary"
    )
    trace = dashboard_payload(database_path=database_path)["trace_summary"]

    assert dry_run["created"] == 0
    assert dry_run["updated"] == 0
    assert dry_run["stale"] == 1
    assert refreshed["created"] == 0
    assert refreshed["updated"] == 1
    assert refreshed["stale"] == 1
    assert second["created"] == 0
    assert second["updated"] == 0
    assert second["skipped"] == 1
    assert manual["metadata"]["diagnostic_contract"]["schema"] == "keystone.eval_run_diagnostics.v1"
    assert manual["metadata"]["model"]["provider"] == "openai"
    assert manual["metadata"]["retrieval"]["search_provider"] == "searxng"
    assert trace["diagnostic_category_counts"] == [
        {
            "key": "missing_execution_provenance",
            "label": "Missing execution provenance",
            "severity": "fail",
            "count": 1,
            "detail": "Run summary lacks the resolved fixture/live SDK/live search mode.",
        },
        {
            "key": "missing_child_step_metadata",
            "label": "Missing child-step metadata",
            "severity": "warn",
            "count": 1,
            "detail": "Run summary lacks the bounded WorkItem/tool execution timeline.",
        },
    ]


def test_trace_summary_counts_full_database_while_recent_rows_are_bounded(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    for index in range(25):
        record_slack_eval_run(
            case_id="slack_trace_totals_001",
            run_id=f"wi_trace_total_{index:02d}",
            agent="business_research_analyst",
            slack_thread_ts="1781206953.875749",
            request_text="@KNI business research analyst summarize selected thread",
            result_summary=f"Saved local no-API run {index}.",
            thread_fetch_status="ok",
            thread_message_count=1,
            database_path=database_path,
        )

    trace = dashboard_payload(database_path=database_path)["trace_summary"]

    assert trace["event_count"] == 50
    assert trace["event_counts"]["slack_run_saved"] == 25
    assert trace["event_counts"]["manual_run_summary"] == 25
    assert trace["run_summary_count"] == 25
    assert trace["manual_run_summary_count"] == 25
    assert trace["joined_manual_run_summary_count"] == 25
    assert trace["unjoined_manual_run_summary_count"] == 0
    assert trace["recent_event_count"] == 20
    assert len(trace["recent_events"]) == 20


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


def test_eval_database_explicit_eval_case_id_wins_over_fuzzy_match(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_from_this_thread_001",
        run_id="wi_old",
        agent="chief_of_staff",
        request_text="From this thread, produce exactly two sections.",
        result_summary="Older similar response.",
        database_path=database_path,
    )

    case_id = resolve_slack_eval_case_id(
        request_text=(
            "chief of staff: eval case slack_cos_readonly_two_section_response_20260620_001 "
            "From this thread, produce exactly two sections."
        ),
        agent="chief_of_staff",
        slack_channel_id="C0BA17Y9C01",
        slack_channel_name="evals",
        database_path=database_path,
    )

    assert case_id == "slack_cos_readonly_two_section_response_20260620_001"
    status = eval_case_status(case_id, database_path=database_path)
    assert status["case"]["source"] == "slack"
    assert status["case"]["user_input"].startswith("chief of staff: eval case")


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
            notes: Useful course scan.
            """,
            slack_thread_ts="1781206953.875749",
        ),
        database_path=database_path,
    )
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(human_eval_reviews)")}
        assert "total_score" in columns
        assert "score_accuracy" in columns
        assert "score_instruction_following" in columns
        review_row = connection.execute(
            """
            SELECT total_score, score_accuracy, score_relevance, score_instruction_following
            FROM human_eval_reviews
            WHERE case_id = ?
            """,
            ("slack_agents_sdk_course_001",),
        ).fetchone()
    assert review_row == pytest.approx((4.455, 4, 5, 5))
    record_slack_eval_run(
        case_id="slack_agents_sdk_course_001",
        run_id="wi_course",
        agent="opportunity_scout",
        slack_channel_id="C0BA17Y9C01",
        slack_channel_name="evals",
        slack_thread_ts="1781206953.875749",
        result_summary="Course scan found source-backed options.",
        context_policy="selected_message_or_thread_recent_window",
        thread_fetch_status="ok",
        thread_message_count=2,
        cost_profile="slack_opportunity_balanced",
        source_count=3,
        visible_source_count=2,
        sdk_cache_hit_rate=0.5,
        database_path=database_path,
    )
    record_eval_trace_event(
        event_type="span_end",
        trace_id="trace_dashboard",
        span_id="span_agent",
        name="agent",
        group_id="slack_agents_sdk_course_001",
        metadata={"agent_name": "opportunity_scout", "case_id": "slack_agents_sdk_course_001"},
        duration_ms=123.4,
        database_path=database_path,
    )
    record_eval_trace_event(
        event_type="sdk_run_summary",
        trace_id="wi_course",
        name="sdk_run_summary",
        group_id="slack_agents_sdk_course_001",
        metadata={
            "schema": "keystone.sdk_run_summary.v1",
            "agent": "opportunity_scout",
            "correlation": {
                "case_id": "slack_agents_sdk_course_001",
                "run_id": "wi_course",
            },
            "redaction": {
                "raw_prompt_included": False,
                "raw_response_included": False,
            },
        },
        duration_ms=456.7,
        database_path=database_path,
    )
    output_path = render_dashboard(
        database_path=database_path,
        output_path=tmp_path / "dashboard.html",
    )

    html = output_path.read_text(encoding="utf-8")
    assert "Keystone Eval Scoring" in html
    assert "slack_agents_sdk_course_001" in html
    assert "opportunity_scout" in html
    assert "Human Avg / 5" in html
    assert "Machine Pass Rate" in html
    assert "Scoring status" in html
    assert "Orchestrator Review" in html
    assert "Orchestrator Judge" not in html
    assert "Orchestrator judge" not in html
    assert "dashboard judge scoring" not in html
    assert "Scoring notes" in html
    assert "Promptfoo scores are backend assertion checks" in html
    assert "manual human and Orchestrator Review scorecards" in html
    assert "Human Quality Review" in html
    assert "Promptfoo Run Analysis" in html
    assert "Case Changes Across Runs" in html
    assert "Latest check" in html
    assert "Machine checks</th>" in html
    assert "<th>Signal</th>" not in html
    assert "Slack Eval Conversation Flow" in html
    assert "Cost-safe #evals loop. Stage bars use saved database rows" in html
    assert "Dashboard Health" in html
    assert "Data Quality" in html
    assert "Slack Evidence" in html
    assert "Slack evidence" in html
    assert "Trace Explorer" in html
    assert "Trace Health" in html
    assert "Trace Diagnostics" in html
    assert "Current Run Trace" in html
    assert "Trace Event Log" in html
    assert "trace-event-detail" in html
    assert "data-trace-detail-index" in html
    assert "Show full sanitized trace details" in html
    assert "keystone.eval.trace_event_detail.v1" in html
    assert "Bounded sanitized trace packet" in html
    assert "Storage & Instrumentation" in html
    assert html.index("Trace Health") < html.index("Current Run Trace")
    assert html.index("trace-analytics") < html.index("trace-latest-run")
    assert html.index("Trace Diagnostics") < html.index("Current Run Trace")
    assert "trace-storage-stack" in html
    assert "Trace Processor Readiness" in html
    assert "Local DB Freshness" in html
    assert "trace-db-freshness" in html
    assert "Slack runs table" in html
    assert "Promptfoo cases table" in html
    assert "Human reviews table" in html
    assert "trace-analytics" in html
    assert "Join health" in html
    assert "Run source split" in html
    assert "Diagnostic signals" in html
    assert "Top cleanup signal" in html
    assert "Privacy guardrail" in html
    assert "trace-current-timeline" in html
    assert "traceTimelineMarkup" in html
    assert "traceStepLabel" in html
    assert "traceEventSignal" in html
    assert "traceAgenticFacts" in html
    assert "traceSameRun" in html
    assert "<div class=\"label\">Tools</div>" in html
    assert "<div class=\"label\">Retrieval</div>" in html
    assert "<div class=\"label\">Model</div>" in html
    assert "diagnostic_category_counts" in html
    assert "diagnostic_category_trends" in html
    assert "trace-diagnostic-trend-chart" in html
    assert "trace-diagnostic-trends" in html
    assert "renderTraceDiagnosticTrendChart" in html
    assert "Promptfoo Scoring Contract" in html
    assert "renderScoringContract" in html
    assert "scoring_contract: item.scoring_contract" in html
    assert '"scoring_contract":' in html
    assert '"required_specialist_routes"' in html
    assert '"required_source_types"' in html
    assert "Trace diagnostic category trend by day" in html
    assert "trace-chart-legend" in html
    assert "traceDiagnosticBadge" in html
    assert "trace-diagnostic-pill" in html
    assert "bar neutral" in html
    assert "diagnostic_followups" in html
    assert "Run summaries" in html
    assert "API Spend Readiness Gates" in html
    assert "Local data checks that should pass before paid Slack/API eval runs are trusted." in html
    assert "Prompt text" in html
    assert "Slack thread evidence" in html
    assert "What We Store Locally" in html
    assert "Implementation Contract" in html
    assert "raw prompts" in html
    assert "eval_trace_events" in html
    assert "Metadata preview" not in html
    assert "copyTraceReview" in html
    assert "data-copy-trace-event" in html
    assert "ensureCopyFallback" in html
    assert "writeClipboardReviewText" in html
    assert "Clipboard access was blocked" in html
    assert "copyPrompt" in html
    assert "data-copy-prompt" in html
    assert "The text is selected so it can be copied manually." in html
    assert "keystone.eval.trace_event_review.v1" in html
    assert "joined_case" in html
    assert "case_bundle_url: joinKey ? `/api/eval-case-bundle?case=${encodeURIComponent(joinKey)}` : ''" in html
    assert "metadata_truncated" in html
    assert "metadata_excerpt" in html
    assert "field_readiness" in html
    assert "missing_relevant_fields" in html
    assert "Trace includes compact timing, model, tool, retrieval, approval, side-effect, prompt/config, cost/cache, and error/retry diagnostics." in html
    assert "Analysis Latest Run" not in html
    assert "Analysis Data Handoff" not in html
    assert "analysis-latest-run" not in html
    assert "Latest pass rate" not in html
    prompt_rows_script = re.search(
        r"function renderPromptRows\(\) \{(.*?)function renderRows",
        html,
        re.DOTALL,
    )
    assert prompt_rows_script is not None
    assert "runScoreStrip(item)" not in prompt_rows_script.group(1)
    assert "latestRun" not in prompt_rows_script.group(1)
    assert "latestRunAt" not in prompt_rows_script.group(1)
    assert "evidence" not in prompt_rows_script.group(1)
    assert "Slack not run" not in prompt_rows_script.group(1)
    database_rows_script = re.search(
        r"function renderDatabaseRows\(\) \{(.*?)async function verifyLocalRefreshEndpoints",
        html,
        re.DOTALL,
    )
    assert database_rows_script is not None
    assert "databaseInventory.rows" in database_rows_script.group(1)
    assert "item.user_input" not in database_rows_script.group(1)
    assert "item.response_text" not in database_rows_script.group(1)
    assert "item.human_notes" not in database_rows_script.group(1)
    assert "orchestrator_judge_run_comment" not in database_rows_script.group(1)
    assert "Review form" in database_rows_script.group(1)
    assert "Case bundle" in database_rows_script.group(1)
    database_header = re.search(
        r'<section id="view-database".*?<thead>(.*?)</thead>',
        html,
        re.DOTALL,
    )
    assert database_header is not None
    assert "Human accuracy" in database_header.group(1)
    assert "Orchestrator accuracy" in database_header.group(1)
    assert "<th>Prompt</th>" not in database_header.group(1)
    assert "<th>Latest response</th>" not in database_header.group(1)
    assert "<th>Review notes</th>" not in database_header.group(1)
    assert "<th>Details</th>" in database_header.group(1)
    assert "workflow-stage-bar" in html
    assert "Source checks" in html
    assert "Thread preview" in html
    assert "what should appear in #evals after one agent run" in html
    assert "Suggested Slack test run" in html
    assert "workflow-disclosure" in html
    assert "Expected live calls when enabled" in html
    assert "Expected in-thread communication flow" in html
    assert "Slack-linked scoring form" in html
    assert "Promptfoo machine-check status" in html
    assert "do not rerun Promptfoo from Slack thread" in html
    assert "Eval Run Ledger" in html
    assert "Chronological local events across Slack runs" in html
    assert "slack_company_research_001" in html
    assert "copy into #evals" in html
    assert "no extra agent response is needed" in html
    assert "Score from the linked form" in html
    assert "Submit Evaluation" in html
    assert "without extra model calls" in html
    assert "Secondary diagnostics" in html
    assert "Planned chart backlog" in html
    assert "Review completion funnel" in html
    assert "Machine vs review coverage" in html
    assert "Review scorecards" in html
    assert "Cost-safe run volume" in html
    assert "Prompt score distribution" in html
    assert "Agent score comparison" in html
    assert "Dimension readiness heatmap" in html
    assert "Machine trend line" in html
    assert "Review trend line" in html
    assert "One AI agent call per root eval" in html
    assert "Interaction guardrails" in html
    assert "Future live trigger" in html
    assert "Live now" in html
    assert "No API call from dashboard copy" in html
    assert "Use dry-run fixtures/cache first; cap live retrieval to accepted root run" in html
    assert "No model call; form uses saved case, run id, thread, and response context" in html
    assert "Manual submit uses no model call and no Slack post; Orchestrator Review runs only when explicitly enabled" in html
    assert "Latest run time" in html
    assert "Run source" in html
    assert "Run id / thread" in html
    assert "<th>Machine</th>" in html
    assert "<th>Human review</th>" in html
    assert "<th>Orchestrator Review</th>" in html
    assert "<th>Evidence</th>" in html
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
    assert case["latest_run_source"] == "slack"
    assert case["latest_run_id"] == "wi_course"
    assert case["latest_run_at"]
    assert case["display_prompt_number"] == "001"
    assert case["human_scores"]["accuracy"] == 4
    assert case["dashboard_url"].endswith("?case=slack_agents_sdk_course_001")
    assert case["review_url"].endswith("?case=slack_agents_sdk_course_001")
    assert case["case_bundle_url"].endswith("/api/eval-case-bundle?case=slack_agents_sdk_course_001")
    case_checks = {item["label"]: item for item in case["case_review_checklist"]}
    assert case_checks["recorded_response"]["status"] == "complete"
    assert case_checks["slack_thread_evidence"]["status"] == "complete"
    assert case_checks["machine_check"]["status"] == "missing"
    assert case_checks["human_review"]["status"] == "complete"
    assert case["next_follow_up"] == "Import Promptfoo result by case_id before analysis comparison."
    assert "Copy eval review packet" in html
    assert "caseBundleReviewText" in html
    assert "keystone.eval.case_review_bundle.v1" in html
    assert "latest_response_truncated" in html
    assert "copy_policy" in html
    assert "Please review this Keystone eval case" in html
    assert data["analysis"]["run_trends"]
    assert data["analysis"]["agent_score_trends"]["series"]["all"]
    assert "opportunity_scout" in data["analysis"]["agent_score_trends"]["agents"]
    assert "Average Score Across Time" in html
    assert "<div class=\"metric-th-title\">Human accuracy</div>" in html
    assert "<div class=\"metric-th-title\">Orchestrator accuracy</div>" in html
    assert "<div class=\"metric-th-title\">Human relevance</div>" in html
    assert "<div class=\"metric-th-title\">Orchestrator relevance</div>" in html
    assert "<th>Human scores</th>" not in html
    assert "<th>Orchestrator scores</th>" not in html
    assert "scoreMetricStrip" in html
    assert "Machine average" in html
    assert "Orchestrator Review average" in html
    assert "Review target" in html
    assert 'id="analysis-agent-trend-filter"' in html
    assert "human_case_trends" in data["analysis"]
    assert data["workflow_readiness"]["mode"] == "local_preview"
    assert data["workflow_readiness"]["live_api_calls"] is False
    assert data["trace_summary"]["mode"] == "disabled"
    assert data["trace_summary"]["event_count"] == 4
    assert data["trace_summary"]["run_summary_count"] == 2
    assert data["trace_summary"]["sdk_run_summary_count"] == 1
    assert data["trace_summary"]["manual_run_summary_count"] == 1
    assert data["trace_summary"]["joined_run_summary_count"] == 2
    assert data["trace_summary"]["joined_sdk_run_summary_count"] == 1
    assert data["trace_summary"]["joined_manual_run_summary_count"] == 1
    assert data["trace_summary"]["unjoined_run_summary_count"] == 0
    assert data["trace_summary"]["unjoined_sdk_run_summary_count"] == 0
    assert data["trace_summary"]["unjoined_manual_run_summary_count"] == 0
    trace_diagnostics = {
        item["key"]: item["count"] for item in data["trace_summary"]["diagnostic_category_counts"]
    }
    assert trace_diagnostics["missing_diagnostic_contract"] == 1
    assert trace_diagnostics["missing_execution_provenance"] == 1
    assert data["trace_summary"]["diagnostic_followups"]
    assert data["trace_summary"]["diagnostic_case_rollups"]
    assert data["trace_summary"]["effective_sensitive_capture"] is False
    assert data["trace_summary"]["sensitive_data_env"] == "default_false"
    trace_ids = {event["trace_id"] for event in data["trace_summary"]["recent_events"]}
    assert {"trace_dashboard", "slack_run:wi_course", "manual_run:wi_course"}.issubset(trace_ids)
    assert "raw prompts" in data["trace_summary"]["dropped_fields"]
    assert data["data_quality"]["mode"] == "local_preflight"
    assert data["data_quality"]["live_api_calls"] is False
    assert any(check["key"] == "prompt_text" for check in data["data_quality"]["checks"])
    assert any(check["key"] == "slack_thread_evidence" for check in data["data_quality"]["checks"])
    assert any(
        check["key"] == "trace_sensitive_capture" and check["status"] == "complete"
        for check in data["data_quality"]["checks"]
    )
    assert any(
        check["key"] == "trace_run_summaries" and check["status"] == "complete"
        for check in data["data_quality"]["checks"]
    )
    ledger_sources = {item["source"] for item in data["run_ledger"]}
    assert {"promptfoo", "slack", "human", "trace"}.issubset(ledger_sources)
    slack_ledger = next(item for item in data["run_ledger"] if item["source"] == "slack")
    assert slack_ledger["case_id"] == "slack_agents_sdk_course_001"
    assert slack_ledger["run_id"] == "wi_course"
    assert "2 messages" in slack_ledger["details"]
    assert slack_ledger["trace_agentic_summary"]["signal"] == "2/3 visible sources"
    assert slack_ledger["trace_agentic_summary"]["retrieval"] == "2/3 visible sources"
    assert "trace 2/3 visible sources" in slack_ledger["details"]
    assert slack_ledger["dashboard_url"].endswith("?case=slack_agents_sdk_course_001")
    assert slack_ledger["review_url"].endswith("?case=slack_agents_sdk_course_001")
    assert slack_ledger["case_bundle_url"].endswith(
        "/api/eval-case-bundle?case=slack_agents_sdk_course_001"
    )
    slack_checks = {item["label"]: item for item in slack_ledger["review_checklist"]}
    assert slack_checks["response_saved"]["status"] == "complete"
    assert slack_checks["slack_thread_evidence"]["status"] == "complete"
    assert slack_checks["human_review"]["status"] == "complete"
    assert slack_checks["promptfoo_import"]["status"] == "complete"
    assert slack_ledger["next_follow_up"] == (
        "Ready to compare Slack output, machine score, Orchestrator Review, and human review in Analysis."
    )
    promptfoo_ledger = next(item for item in data["run_ledger"] if item["source"] == "promptfoo")
    promptfoo_checks = {item["label"]: item for item in promptfoo_ledger["review_checklist"]}
    assert promptfoo_checks["machine_import"]["status"] == "complete"
    assert promptfoo_checks["machine_average"]["status"] == "complete"
    assert 'data-ledger-index="${index}"' in html
    assert "data-copy-ledger-event" in html
    assert "ledgerDefaultLimit = 12" in html
    assert "data-ledger-toggle" in html
    assert "Showing newest" in html
    assert "copyLedgerReview" in html
    assert "Please review this Keystone eval run ledger entry" in html
    assert "review_checklist" in html
    assert "next_follow_up" in html
    assert data["workflow_readiness"]["counts"]["total_cases"] > 0
    starter_plan = data["workflow_readiness"]["starter_run_plan"]
    assert starter_plan["channel"] == "#evals"
    assert starter_plan["case_id"] == "slack_company_research_001"
    assert starter_plan["display_case_id"].endswith("_012")
    assert starter_plan["paste_text"].startswith("@KNI business research analyst")
    assert starter_plan["local_only_now"] is True
    assert starter_plan["dashboard_url"].endswith("?case=slack_company_research_001")
    assert starter_plan["human_review_url"].endswith("?case=slack_company_research_001")
    assert starter_plan["status_reply"] == "Submit Evaluation"
    assert any(item["speaker"] == "KNI eval context" for item in starter_plan["thread_sequence"])
    assert any(item["step"] == "Promptfoo summary" for item in starter_plan["expected_live_calls_when_enabled"])
    assert len(starter_plan["expected_live_calls_when_enabled"]) >= 4
    interactions = data["workflow_readiness"]["interactions"]
    assert [item["interaction"] for item in interactions] == [
        "Prompt copy",
        "Agent thread reply",
        "Promptfoo machine summary",
        "Retrieval/source evidence",
        "Slack review form open",
        "Submit Evaluation",
        "Analysis inclusion",
    ]
    assert all(item["live_api_call_now"] is False for item in interactions)
    assert interactions[0]["cost_guardrail"] == "No API call from dashboard copy"
    assert interactions[2]["cost_guardrail"] == "Use imported Promptfoo result by case_id; do not rerun Promptfoo from Slack thread"
    assert interactions[3]["cost_guardrail"] == "Use dry-run fixtures/cache first; cap live retrieval to accepted root run"
    assert interactions[4]["cost_guardrail"] == "No model call; form uses saved case, run id, thread, and response context"
    assert interactions[5]["cost_guardrail"] == (
        "Manual submit uses no model call and no Slack post; Orchestrator Review runs only when explicitly enabled, then refreshes Database, Runs & Scoring, and Analysis from saved rows"
    )
    assert data["score_dimensions"]
    assert "review-form" in html
    assert "Review controls are disabled." not in html


def test_eval_dashboard_human_state_tracks_latest_slack_target(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_agents_sdk_course_001",
        run_id="wi_old",
        agent="opportunity_scout",
        slack_thread_ts="1781206953.000001",
        request_text="@KNI opportunity scout -- find three Agents SDK courses",
        result_summary="Old course scan.",
        database_path=database_path,
    )
    save_human_review(
        parse_human_review(
            """
            eval score
            case: slack_agents_sdk_course_001
            run: wi_old
            agent: opportunity_scout
            accuracy: 4
            relevance: 4
            explainability: 4
            readability: 4
            source_quality: 4
            search_quality: 4
            synthesis: 4
            output: 4
            format: 4
            instruction_following: 4
            usefulness: 4
            safety: pass
            notes: Old run reviewed.
            """,
            slack_thread_ts="1781206953.000001",
        ),
        database_path=database_path,
    )
    record_slack_eval_run(
        case_id="slack_agents_sdk_course_001",
        run_id="wi_new",
        agent="opportunity_scout",
        slack_thread_ts="1781206953.000002",
        request_text="@KNI opportunity scout -- find three Agents SDK courses",
        result_summary="New course scan.",
        database_path=database_path,
    )

    payload = dashboard_payload(database_path=database_path)
    case = next(item for item in payload["cases"] if item["case_id"] == "slack_agents_sdk_course_001")
    checks = {item["label"]: item for item in case["case_review_checklist"]}

    assert case["latest_slack_run_id"] == "wi_new"
    assert case["human_average"] is None
    assert case["human_scores"] == {}
    assert checks["human_review"]["status"] == "missing"
    assert payload["summary"]["human_reviewed"] == 0
    assert payload["analysis"]["human_review_trends"] == []
    assert payload["analysis"]["human_case_trends"] == []
    assert all(
        row.get("human_count") == 0
        for row in payload["analysis"]["agent_score_trends"]["series"].get("all", [])
    )
    case_rows = list_eval_cases(database_path=database_path)
    case_row = next(row for row in case_rows if row["case_id"] == "slack_agents_sdk_course_001")
    assert case_row["latest_human_average"] is None
    assert case_row["latest_human_safety"] == ""

    save_human_review(
        parse_human_review(
            """
            eval score
            case: slack_agents_sdk_course_001
            run: wi_new
            agent: opportunity_scout
            accuracy: 5
            relevance: 5
            explainability: 5
            readability: 5
            source_quality: 5
            search_quality: 5
            synthesis: 5
            output: 5
            format: 5
            instruction_following: 5
            usefulness: 5
            safety: pass
            notes: New run reviewed.
            """,
            slack_thread_ts="1781206953.000002",
        ),
        database_path=database_path,
    )

    reviewed_payload = dashboard_payload(database_path=database_path)
    reviewed_case = next(
        item for item in reviewed_payload["cases"] if item["case_id"] == "slack_agents_sdk_course_001"
    )
    reviewed_checks = {item["label"]: item for item in reviewed_case["case_review_checklist"]}

    assert reviewed_case["latest_slack_run_id"] == "wi_new"
    assert reviewed_case["human_average"] == 5.0
    assert reviewed_case["human_notes"] == "New run reviewed."
    assert reviewed_checks["human_review"]["status"] == "complete"
    assert reviewed_payload["summary"]["human_reviewed"] == 1
    assert reviewed_payload["analysis"]["human_review_trends"] == [
        {"date": reviewed_case["human_created_at"][:10], "average_score": 5.0, "count": 1}
    ]
    reviewed_human_case_trend = next(
        row
        for row in reviewed_payload["analysis"]["human_case_trends"]
        if row["case_id"] == "slack_agents_sdk_course_001"
    )
    assert len(reviewed_human_case_trend["days"]) == 1
    reviewed_human_day = reviewed_human_case_trend["days"][0]
    assert reviewed_human_day["date"] == reviewed_case["human_created_at"][:10]
    assert reviewed_human_day["average_score"] == 5.0
    assert reviewed_human_day["count"] == 1
    assert reviewed_human_day["reviews"][0]["target_type"] == "slack"
    assert reviewed_human_day["reviews"][0]["run_id"] == "wi_new"
    assert reviewed_human_day["reviews"][0]["slack_thread_ts"] == "1781206953.000002"
    reviewed_case_rows = list_eval_cases(database_path=database_path)
    reviewed_case_row = next(
        row for row in reviewed_case_rows if row["case_id"] == "slack_agents_sdk_course_001"
    )
    assert reviewed_case_row["latest_human_average"] == 5.0
    assert reviewed_case_row["latest_human_safety"] == "pass"


def test_eval_dashboard_human_analysis_target_uses_status_not_run_id_prefix(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_custom_run_id_001",
        run_id="eval-looking-slack-target",
        agent="opportunity_scout",
        request_text="@KNI opportunity scout -- find relevant eval opportunities",
        result_summary="Slack response with a nonstandard run id.",
        database_path=database_path,
    )
    save_human_review(
        parse_human_review(
            """
            eval score
            case: slack_custom_run_id_001
            run: eval-looking-slack-target
            agent: opportunity_scout
            accuracy: 4
            relevance: 5
            explainability: 4
            readability: 4
            source_quality: 4
            search_quality: 4
            synthesis: 4
            output: 4
            format: 4
            instruction_following: 4
            usefulness: 5
            safety: pass
            notes: Nonstandard Slack run reviewed.
            """,
        ),
        database_path=database_path,
    )

    payload = dashboard_payload(database_path=database_path)
    human_case_trend = next(
        row
        for row in payload["analysis"]["human_case_trends"]
        if row["case_id"] == "slack_custom_run_id_001"
    )
    review_target = human_case_trend["days"][0]["reviews"][0]

    assert review_target["target_type"] == "slack"
    assert review_target["run_id"] == "eval-looking-slack-target"
    assert review_target["target_storage_mode"] == "local_review"


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
    record_slack_eval_run(
        case_id="slack_agents_sdk_course_001",
        run_id="wi_excluded",
        agent="opportunity_scout",
        request_text="@KNI opportunity scout -- find three Agents SDK courses",
        result_summary="Duplicate retry result.",
        thread_fetch_status="ok",
        thread_message_count=1,
        database_path=database_path,
    )
    save_human_review(
        parse_human_review(
            """
            eval score
            case: slack_agents_sdk_course_001
            run: wi_excluded
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
            safety: pass
            notes: Duplicate retry score.
            """,
            slack_thread_ts="",
        ),
        database_path=database_path,
    )

    exclusion_result = save_analysis_exclusion_payload(
        {
            "eval_id": "eval-dashboard",
            "case_id": "slack_agents_sdk_course_001",
            "excluded": True,
            "reason": "duplicate retry",
        },
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
    assert exclusion_result["refresh_targets"] == ["overview", "database", "runs_scoring", "analysis"]
    assert exclusion_result["refresh_endpoints"] == [
        "/api/status?refresh=1",
        "/api/eval-cases",
        "/api/follow-up-queue",
        "/api/data-quality",
        "/api/eval-run-ledger",
        "/api/trace-diagnostics",
        "/api/eval-case-bundle?case=slack_agents_sdk_course_001",
    ]
    assert exclusion_result["database_tables"]["promptfoo_case_results"]["row_count"] == 1
    assert exclusion_result["database_tables"]["human_eval_reviews"]["row_count"] == 1
    assert (
        exclusion_result["database_tables"]["human_eval_reviews"]["latest"]["case_id"]
        == "slack_agents_sdk_course_001"
    )
    assert exclusion_result["post_save_state"]["case"]["analysis_excluded"] is True
    assert exclusion_result["post_save_state"]["case"]["analysis_exclusion_reason"] == "duplicate retry"
    assert exclusion_result["post_save_state"]["analysis"]["run_trends"] == 0
    assert exclusion_result["post_save_state"]["summary"]["human_reviewed"] == 0
    assert case["analysis_excluded"] is True
    assert case["analysis_exclusion_reason"] == "duplicate retry"
    assert data["analysis"]["run_trends"] == []
    assert data["analysis"]["case_trends"] == []
    assert data["analysis"]["human_review_trends"] == []
    assert data["analysis"]["agent_score_trends"]["series"]["all"] == []
    excluded_ledger_rows = [
        row for row in data["run_ledger"] if row["case_id"] == "slack_agents_sdk_course_001"
    ]
    assert excluded_ledger_rows
    assert excluded_ledger_rows[0]["excluded_from_scoring"] is True
    assert excluded_ledger_rows[0]["analysis_exclusion_reason"] == "duplicate retry"
    assert data["summary"]["human_reviewed"] == 0
    assert data["workflow_readiness"]["counts"]["human_scorecards"] == 1
    assert data["workflow_readiness"]["counts"]["analysis_included"] == 0
    assert "Include in analysis" in html
    assert "analysis excluded" in html


def test_slack_eval_duplicate_attempts_keep_audit_rows_with_group_marker(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    first_id = record_slack_eval_run(
        case_id="slack_duplicate_eval_001",
        run_id="wi_first",
        agent="business_research_analyst",
        slack_thread_ts="1781206953.875749",
        request_text="@KNI business research analyst research Lindus Health",
        result_summary="Lindus Health research complete.",
        status="done",
        response_hash="same-response",
        database_path=database_path,
    )
    second_id = record_slack_eval_run(
        case_id="slack_duplicate_eval_001",
        run_id="wi_second",
        agent="business_research_analyst",
        slack_thread_ts="1781206953.875749",
        request_text="@KNI business research analyst research Lindus Health",
        result_summary="Lindus Health research complete.",
        status="done",
        response_hash="same-response",
        database_path=database_path,
    )

    status = eval_case_status("slack_duplicate_eval_001", database_path=database_path)
    rows = status["slack_runs"]

    assert first_id != second_id
    assert len(rows) == 2
    assert rows[0]["attempt_group_id"] == rows[1]["attempt_group_id"]
    assert rows[0]["duplicate_attempt"] == 1
    assert rows[0]["duplicate_of_run_id"] == "wi_first"


def test_source_tagged_dashboard_case_treats_zero_source_metadata_as_attention(tmp_path) -> None:
    results_path = tmp_path / "latest-eval.json"
    results_path.write_text(
        json.dumps(
            {
                "evalId": "eval-source-visibility",
                "results": {
                    "timestamp": "2026-06-11T18:00:00Z",
                    "stats": {"successes": 1, "failures": 0, "errors": 0},
                    "results": [
                        {
                            "id": "result-1",
                            "success": True,
                            "score": 1,
                            "vars": {
                                "case_id": "slack_source_visibility_001",
                                "agent_under_test": "business_research_analyst",
                                "eval_dimensions": "retrieval, source_visibility",
                                "user_input": "@KNI business research analyst research Lindus Health",
                                "min_source_count": 1,
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
    record_slack_eval_run(
        case_id="slack_source_visibility_001",
        run_id="wi_sources_missing",
        agent="business_research_analyst",
        request_text="@KNI business research analyst research Lindus Health",
        result_summary="Research complete but no sources recorded.",
        thread_fetch_status="ok",
        thread_message_count=1,
        source_count=0,
        visible_source_count=0,
        database_path=database_path,
    )

    payload = dashboard_payload(database_path=database_path)
    case = next(item for item in payload["cases"] if item["case_id"] == "slack_source_visibility_001")
    checks = {item["label"]: item for item in case["case_review_checklist"]}
    quality_checks = {item["key"]: item for item in payload["data_quality"]["checks"]}

    assert checks["source_visibility"]["status"] == "attention"
    assert quality_checks["source_visibility"]["status"] == "pending"


def test_human_review_payload_rejects_mismatched_slack_target(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_agents_sdk_course_001",
        run_id="wi_expected",
        agent="opportunity_scout",
        slack_thread_ts="1781206953.875749",
        request_text="@KNI opportunity scout -- find three Agents SDK courses",
        result_summary="Course scan complete.",
        database_path=database_path,
    )

    with pytest.raises(ValueError, match="run_id does not match"):
        save_human_review_payload(
            {
                "case_id": "slack_agents_sdk_course_001",
                "run_id": "wi_wrong",
                "agent": "opportunity_scout",
                "slack_thread_ts": "1781206953.875749",
                "scores": _review_scores(),
                "safety": "pass",
            },
            database_path=database_path,
            require_recorded_response=True,
        )
    with pytest.raises(ValueError, match="Slack thread does not match"):
        save_human_review_payload(
            {
                "case_id": "slack_agents_sdk_course_001",
                "run_id": "wi_expected",
                "agent": "opportunity_scout",
                "slack_thread_ts": "1781200000.000000",
                "scores": _review_scores(),
                "safety": "pass",
            },
            database_path=database_path,
            require_recorded_response=True,
        )


def test_human_review_payload_rejects_untargeted_slack_case(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_agents_sdk_course_001",
        run_id="wi_expected",
        agent="opportunity_scout",
        slack_thread_ts="1781206953.875749",
        request_text="@KNI opportunity scout -- find three Agents SDK courses",
        result_summary="Course scan complete.",
        database_path=database_path,
    )

    with pytest.raises(ValueError, match="Slack-linked case requires"):
        save_human_review_payload(
            {
                "case_id": "slack_agents_sdk_course_001",
                "scores": _review_scores(),
                "safety": "pass",
            },
            database_path=database_path,
            require_recorded_response=True,
        )
    with pytest.raises(ValueError, match="Slack-linked case requires"):
        save_human_review_payload(
            {
                "case_id": "slack_agents_sdk_course_001",
                "agent": "opportunity_scout",
                "scores": _review_scores(),
                "safety": "pass",
            },
            database_path=database_path,
            require_recorded_response=True,
        )


def test_human_review_payload_resolves_slack_thread_target(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_agents_sdk_course_001",
        run_id="wi_expected",
        agent="opportunity_scout",
        slack_thread_ts="1781206953.875749",
        request_text="@KNI opportunity scout -- find three Agents SDK courses",
        result_summary="Course scan complete.",
        database_path=database_path,
    )

    result = save_human_review_payload(
        {
            "case_id": "slack_agents_sdk_course_001",
            "slack_thread_ts": "1781206953.875749",
            "scores": _review_scores(usefulness=5),
            "safety": "pass",
        },
        database_path=database_path,
        require_recorded_response=True,
    )
    stored = list_human_reviews(
        database_path=database_path,
        case_id="slack_agents_sdk_course_001",
    )

    assert result["run_id"] == "wi_expected"
    assert result["agent"] == "opportunity_scout"
    assert result["slack_thread_ts"] == "1781206953.875749"
    assert stored[0]["run_id"] == "wi_expected"
    assert stored[0]["agent"] == "opportunity_scout"
    assert stored[0]["slack_thread_ts"] == "1781206953.875749"


def test_orchestrator_judge_scores_saved_evals_slack_run_as_scorecard(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_agents_sdk_course_001",
        run_id="wi_expected",
        agent="opportunity_scout",
        slack_channel_name="evals",
        slack_thread_ts="1781206953.875749",
        request_text="@KNI opportunity scout -- find three Agents SDK courses",
        result_summary="Course scan complete with three options and sources.",
        database_path=database_path,
    )

    result = score_eval_run_with_orchestrator_judge(
        case_id="slack_agents_sdk_course_001",
        slack_thread_ts="1781206953.875749",
        database_path=database_path,
        scorer=lambda _packet: OrchestratorEvalJudgeScorecard(
            scores=_review_scores(accuracy=5, source_quality=3),
            safety="pass",
            notes=(
                "For this run, the response answered the course request but lowered the "
                "score because source detail was thin."
            ),
            dimension_rationales={
                "accuracy": "The saved response matches the prompt at a high level.",
                "source_quality": "Only thin source detail was visible in the run output.",
            },
            recommended_next_action="Use as baseline; improve sources next.",
            confidence=0.8,
        ),
    )
    stored = list_human_reviews(
        database_path=database_path,
        case_id="slack_agents_sdk_course_001",
    )
    payload = dashboard_payload(database_path=database_path)
    case = next(item for item in payload["cases"] if item["case_id"] == "slack_agents_sdk_course_001")
    status = eval_case_status("slack_agents_sdk_course_001", database_path=database_path)

    assert result["review_kind"] == "orchestrator_judge"
    assert result["reviewer"] == "orchestrator_judge"
    assert result["average_score"] == pytest.approx(4.0)
    assert stored[0]["review_kind"] == "orchestrator_judge"
    assert status["human_review_count"] == 0
    assert status["orchestrator_judge_review_count"] == 1
    assert status["scorecard_review_count"] == 1
    assert case["human_average"] is None
    assert case["review_kind"] == ""
    assert case["orchestrator_judge_average"] == pytest.approx(4.0)
    assert "lowered the score" in case["orchestrator_judge_run_comment"]
    assert (
        case["orchestrator_judge_dimension_rationales"]["source_quality"]
        == "Only thin source detail was visible in the run output."
    )
    assert payload["summary"]["human_reviewed"] == 0
    assert payload["summary"]["orchestrator_judge_reviewed"] == 1
    assert payload["analysis"]["agent_score_trends"]["series"]["all"][0][
        "orchestrator_judge_average"
    ] == pytest.approx(4.0)


def test_orchestrator_judge_scorecard_is_agents_strict_schema_compatible() -> None:
    AgentOutputSchema(OrchestratorEvalJudgeScorecard)


def test_orchestrator_judge_rejects_promptfoo_only_case(tmp_path) -> None:
    results_path = tmp_path / "results.json"
    results_path.write_text(
        json.dumps(
            {
                "evalId": "eval-one",
                "results": {
                    "timestamp": "2026-06-20T10:00:00Z",
                    "stats": {"successes": 1, "failures": 0, "errors": 0},
                    "results": [
                        {
                            "vars": {
                                "case_id": "promptfoo_only_001",
                                "agent_under_test": "opportunity_scout",
                                "user_input": "@KNI opportunity scout find courses",
                            },
                            "success": True,
                            "score": 1,
                            "response": {"output": "Promptfoo fixture response."},
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "evals.sqlite"
    import_promptfoo_results(results_path, database_path=database_path)

    with pytest.raises(ValueError, match="saved #evals Slack run"):
        score_eval_run_with_orchestrator_judge(
            case_id="promptfoo_only_001",
            database_path=database_path,
            scorer=lambda _packet: OrchestratorEvalJudgeScorecard(
                scores=_review_scores(),
                safety="pass",
            ),
        )


def test_orchestrator_judge_payload_requires_enable_flag(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_agents_sdk_course_001",
        run_id="wi_expected",
        agent="opportunity_scout",
        slack_channel_name="evals",
        slack_thread_ts="1781206953.875749",
        request_text="@KNI opportunity scout -- find three Agents SDK courses",
        result_summary="Course scan complete.",
        database_path=database_path,
    )
    monkeypatch.delenv("KEYSTONE_EVAL_LLM_JUDGE", raising=False)

    with pytest.raises(ValueError, match="KEYSTONE_EVAL_LLM_JUDGE=true"):
        eval_dashboard_server.save_orchestrator_judge_payload(
            {
                "case_id": "slack_agents_sdk_course_001",
                "run_id": "wi_expected",
                "slack_thread_ts": "1781206953.875749",
            },
            database_path=database_path,
        )


def test_human_review_payload_inherits_api_redacted_slack_target(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_private_review_001",
        run_id="wi_private",
        agent="opportunity_scout",
        slack_thread_ts="1781206953.875749",
        request_text="@KNI contact jane@example.com with token sk-SECRETSECRETSECRET",
        result_summary="Drafted reply for jane@example.com using sk-SECRETSECRETSECRET",
        run_mode="live_sdk",
        database_path=database_path,
    )

    result = save_human_review_payload(
        {
            "case_id": "slack_private_review_001",
            "slack_thread_ts": "1781206953.875749",
            "scores": _review_scores(usefulness=5),
            "safety": "pass",
            "notes": "Reviewer saw jane@example.com and token sk-SECRETSECRETSECRET.",
        },
        database_path=database_path,
        require_recorded_response=True,
    )
    stored = list_human_reviews(
        database_path=database_path,
        case_id="slack_private_review_001",
    )[0]
    serialized = json.dumps(stored, sort_keys=True)

    assert result["storage_mode"] == "api_redacted"
    assert stored["storage_mode"] == "api_redacted"
    assert stored["raw_text_hash"]
    assert stored["notes_hash"]
    assert "jane@example.com" not in serialized
    assert "sk-SECRET" not in serialized
    assert "[REDACTED_EMAIL]" in serialized


def test_add_human_review_script_resolves_recorded_slack_target(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_agents_sdk_course_001",
        run_id="wi_expected",
        agent="opportunity_scout",
        slack_thread_ts="1781206953.875749",
        request_text="@KNI opportunity scout -- find three Agents SDK courses",
        result_summary="Course scan complete.",
        database_path=database_path,
    )
    score_block = """
    eval score
    accuracy: 4
    relevance: 5
    explainability: 4
    readability: 4
    source_quality: 4
    search_quality: 4
    synthesis: 4
    uniqueness: 4
    format: 4
    instruction_following: 5
    usefulness: 5
    safety: pass
    notes: useful enough
    """
    monkeypatch.setattr(
        "sys.argv",
        [
            "add_promptfoo_human_review.py",
            "--database-path",
            str(database_path),
            "--case-id",
            "slack_agents_sdk_course_001",
            "--slack-thread-ts",
            "1781206953.875749",
            "--require-recorded-response",
            "--text",
            score_block,
        ],
    )

    assert human_review_script.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["run_id"] == "wi_expected"
    assert payload["agent"] == "opportunity_scout"
    assert payload["slack_thread_ts"] == "1781206953.875749"


def test_promptfoo_eval_db_record_slack_run_accepts_diagnostics(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    database_path = tmp_path / "evals.sqlite"
    evidence = {
        "orchestrator_preflight": {"blocker_count": 1},
        "orchestrator_review": {"feedback_count": 2},
        "web_extraction": {"status": "partial", "issue_count": 1},
        "tool_summary": {
            "tool_call_count": 2,
            "failed_tool_call_count": 1,
            "tool_names": ["search_web"],
        },
        "approval": {"approval_required": True, "status": "blocked", "send_enabled": False},
        "retry_state": {"retry_count": 1, "status": "retried"},
    }
    monkeypatch.setattr(
        "sys.argv",
        [
            "promptfoo_eval_db.py",
            "--database-path",
            str(database_path),
            "record-slack-run",
            "--case-id",
            "slack_manual_cli_trace_001",
            "--run-id",
            "wi_manual_cli",
            "--work-item-id",
            "wi_manual_cli",
            "--agent",
            "business_research_analyst",
            "--route",
            "business_research_analyst",
            "--status",
            "done",
            "--slack-thread-ts",
            "1781206953.875749",
            "--thread-fetch-status",
            "ok",
            "--thread-message-count",
            "2",
            "--warning",
            "visible_source_gap",
            "--source-count",
            "2",
            "--visible-source-count",
            "1",
            "--model-provider",
            "openai",
            "--model-name",
            "gpt-5.4-mini",
            "--run-mode",
            "manual_slack_no_api",
            "--search-provider",
            "searxng",
            "--search-provider-sequence",
            "searxng",
            "--evidence-json",
            json.dumps(evidence),
            "--request-text",
            "@KNI business research analyst summarize selected thread",
            "--result-summary",
            "Saved manual CLI run.",
        ],
    )

    assert promptfoo_eval_db_script.main() == 0
    payload = json.loads(capsys.readouterr().out)
    status = eval_case_status("slack_manual_cli_trace_001", database_path=database_path)
    manual = next(
        event
        for event in list_eval_trace_events(database_path=database_path)
        if event["event_type"] == "manual_run_summary"
    )
    categories = {
        item["key"]: item["count"]
        for item in dashboard_payload(database_path=database_path)["trace_summary"][
            "diagnostic_category_counts"
        ]
    }

    assert payload["id"] > 0
    assert payload["dashboard_case_url"].endswith("?case=slack_manual_cli_trace_001")
    assert payload["review_case_url"].endswith("?case=slack_manual_cli_trace_001")
    assert payload["case_bundle_url"].endswith(
        "/api/eval-case-bundle?case=slack_manual_cli_trace_001"
    )
    assert payload["dashboard_visibility"]["case_visible"] is True
    assert payload["dashboard_visibility"]["display_case_id"]
    assert payload["dashboard_visibility"]["latest_run_visible"] is True
    assert payload["dashboard_visibility"]["latest_run_source"] == "slack"
    assert payload["dashboard_visibility"]["latest_slack_run_id"] == "wi_manual_cli"
    assert payload["dashboard_visibility"]["in_follow_up_queue"] is True
    assert payload["dashboard_visibility"]["trace_event_visible"] is True
    assert payload["dashboard_visibility"]["trace_diagnostics_visible"] is True
    assert payload["merged_status"]["slack_run_count"] == 1
    assert payload["merged_status"]["latest_slack_run_id"] == "wi_manual_cli"
    assert payload["merged_status"]["latest_slack_created_at"]
    assert payload["follow_up"]["still_open"] is True
    assert payload["follow_up"]["follow_up_summary"].startswith("Missing:")
    assert "human_review" in payload["follow_up"]["missing_labels"]
    assert payload["trace"]["manual_run_summary_present"] is True
    assert payload["trace"]["diagnostic_contract"] is True
    assert payload["trace"]["trace_id"] == "manual_run:wi_manual_cli"
    assert payload["trace"]["span_id"] == f"slack_run_summary:{payload['id']}"
    assert payload["trace"]["slack_run_created_at"]
    assert payload["database_tables"]["slack_eval_runs"]["latest"]["run_id"] == "wi_manual_cli"
    assert "/api/status?refresh=1" in payload["refresh_endpoints"]
    assert "/api/data-quality" in payload["refresh_endpoints"]
    assert "/api/trace-diagnostics" in payload["refresh_endpoints"]
    assert status["slack_run_count"] == 1
    assert status["slack_runs"][0]["source_count"] == 2
    assert status["slack_runs"][0]["visible_source_count"] == 1
    assert status["slack_runs"][0]["evidence"]["web_extraction"]["issue_count"] == 1
    assert manual["metadata"]["diagnostic_contract"]["schema"] == "keystone.eval_run_diagnostics.v1"
    assert manual["metadata"]["slack_run_created_at"] == payload["trace"]["slack_run_created_at"]
    assert manual["metadata"]["model"]["provider"] == "openai"
    assert manual["metadata"]["retrieval"]["search_provider"] == "searxng"
    assert manual["metadata"]["retrieval"]["web_extraction_issue_count"] == 1
    assert manual["metadata"]["tooling"]["failed_tool_call_count"] == 1
    assert manual["metadata"]["orchestrator"]["has_preflight"] is True
    assert manual["metadata"]["orchestrator"]["has_review"] is True
    assert manual["metadata"]["diagnostic_summary"]["has_orchestrator_feedback"] is True
    assert categories["orchestrator_feedback"] == 1
    assert categories["web_extraction_issues"] == 1
    assert categories["tool_failures"] == 1


def test_sync_slack_eval_thread_records_completed_thread(tmp_path: Path) -> None:
    database_path = tmp_path / "evals.sqlite"
    thread_path = tmp_path / "thread.json"
    thread_path.write_text(
        json.dumps(
            {
                "ok": True,
                "messages": [
                    {
                        "ts": "1781972599.940979",
                        "text": (
                            "<@U0ASBG2R823|KNI> chief of staff: eval case "
                            "slack_cos_exec_brief_three_sections_001\n\n"
                            "Make a three-section executive brief from this thread: "
                            "decision, evidence, next action."
                        ),
                    },
                    {
                        "ts": "1781972620.111111",
                        "text": (
                            "Business Agents Run Completed\n"
                            "Run: sbar_737037d36ecb44b89d2aab2120b91e29\n"
                            "Status: completed"
                        ),
                    },
                    {
                        "ts": "1781972630.222222",
                        "text": (
                            "Decision: keep the run pending until completion evidence is visible.\n"
                            "Evidence: the Slack run completed and linked one source: "
                            "https://example.com/evidence\n"
                            "Next action: update the scoring form."
                        ),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = sync_slack_eval_thread_script.sync_slack_eval_thread(
        database_path=database_path,
        thread_json_path=str(thread_path),
        channel_id="C0BA17Y9C01",
        channel_name="evals",
    )

    assert payload["case_id"] == "slack_cos_exec_brief_three_sections_001"
    assert payload["run_id"] == "sbar_737037d36ecb44b89d2aab2120b91e29"
    assert payload["status"] == "done"
    status = eval_case_status(payload["case_id"], database_path=database_path)
    assert status["slack_run_count"] == 1
    assert status["slack_runs"][0]["thread_message_count"] == 3
    assert status["slack_runs"][0]["source_count"] == 1
    dashboard = dashboard_payload(database_path=database_path, limit=200)
    case = next(item for item in dashboard["cases"] if item["case_id"] == payload["case_id"])
    assert case["latest_run_source"] == "slack"
    assert case["latest_run_id"] == "sbar_737037d36ecb44b89d2aab2120b91e29"
    assert case["latest_run_at"]
    assert "Decision:" in case["latest_slack_summary"]
    assert "Decision:" in case["scored_response_text"]
    assert payload["post_save_state"]["dashboard_visibility"]["case_visible"] is True


def test_sync_slack_eval_thread_requires_explicit_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="use exactly one"):
        sync_slack_eval_thread_script.sync_slack_eval_thread(
            database_path=tmp_path / "evals.sqlite",
        )


def test_sync_slack_eval_thread_cli_reads_thread_json_from_stdin(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    database_path = tmp_path / "evals.sqlite"
    thread_payload = {
        "ok": True,
        "messages": [
            {
                "ts": "1781973000.100000",
                "text": (
                    "<@U0ASBG2R823|KNI> opportunity scout: eval case "
                    "slack_live_sync_stdin_001\nFind one safe eval target."
                ),
            },
            {
                "ts": "1781973001.100000",
                "text": "Business Agents Run Completed\nRun: sbar_stdin123\nStatus: completed",
            },
            {
                "ts": "1781973002.100000",
                "text": "Result: found one safe target for review.",
            },
        ],
    }
    monkeypatch.setattr(
        "sys.argv",
        [
            "sync_slack_eval_thread.py",
            "--database-path",
            str(database_path),
            "--thread-json",
            "-",
            "--json",
        ],
    )
    monkeypatch.setattr("sys.stdin", SimpleNamespace(read=lambda: json.dumps(thread_payload)))

    assert sync_slack_eval_thread_script.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["case_id"] == "slack_live_sync_stdin_001"
    assert payload["run_id"] == "sbar_stdin123"
    status = eval_case_status("slack_live_sync_stdin_001", database_path=database_path)
    assert status["slack_run_count"] == 1


def test_live_slack_eval_case_keeps_explicit_display_case_id(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_cos_decision_memo_quality_20260620_001",
        run_id="sbar_live_label",
        agent="chief_of_staff",
        request_text=(
            "@KNI chief of staff: eval case slack_cos_decision_memo_quality_20260620_001 "
            "Create a decision memo."
        ),
        result_summary="Saved live Slack response.",
        thread_fetch_status="ok",
        thread_message_count=3,
        database_path=database_path,
    )

    payload = dashboard_payload(database_path=database_path)
    selected = next(
        item
        for item in payload["cases"]
        if item["case_id"] == "slack_cos_decision_memo_quality_20260620_001"
    )
    bundle = eval_case_bundle_response(
        case_id="slack_cos_decision_memo_quality_20260620_001",
        database_path=database_path,
    )["bundle"]

    assert selected["source"] == "slack"
    assert selected["display_prompt_number"]
    assert selected["display_case_id"] == selected["case_id"]
    assert bundle["display_case_id"] == "slack_cos_decision_memo_quality_20260620_001"


def test_promptfoo_eval_db_record_slack_run_accepts_display_case_id(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    database_path = tmp_path / "evals.sqlite"
    seed_payload = dashboard_payload(database_path=database_path)
    seed_case = next(
        item
        for item in seed_payload["cases"]
        if item["case_id"] == "slack_behavioral_health_rfp_001"
    )
    display_case_id = seed_case["display_case_id"]
    assert display_case_id != seed_case["case_id"]
    monkeypatch.setattr(
        "sys.argv",
        [
            "promptfoo_eval_db.py",
            "--database-path",
            str(database_path),
            "record-slack-run",
            "--case-id",
            display_case_id,
            "--run-id",
            "wi_display_cli",
            "--agent",
            "opportunity_scout",
            "--request-text",
            "@KNI opportunity scout find grants or RFPs",
            "--result-summary",
            "Saved display-id CLI run.",
        ],
    )

    assert promptfoo_eval_db_script.main() == 0
    payload = json.loads(capsys.readouterr().out)
    canonical_status = eval_case_status("slack_behavioral_health_rfp_001", database_path=database_path)
    display_status = eval_case_status(display_case_id, database_path=database_path)

    assert payload["input_case_id"] == display_case_id
    assert payload["case_id"] == "slack_behavioral_health_rfp_001"
    assert payload["dashboard_case_url"].endswith("?case=slack_behavioral_health_rfp_001")
    assert canonical_status["slack_run_count"] == 1
    assert canonical_status["slack_runs"][0]["run_id"] == "wi_display_cli"
    assert display_status["slack_run_count"] == 0


def test_promptfoo_eval_db_status_includes_readiness_summary(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_status_ready_001",
        run_id="wi_status_ready",
        agent="business_research_analyst",
        request_text="@KNI business research analyst summarize selected thread",
        result_summary="Status-ready response.",
        model_provider="openai",
        model_name="gpt-5.4-mini",
        search_provider="searxng",
        database_path=database_path,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "promptfoo_eval_db.py",
            "--database-path",
            str(database_path),
            "status",
            "--case-id",
            "slack_status_ready_001",
            "--json",
        ],
    )

    assert promptfoo_eval_db_script.main() == 0
    payload = json.loads(capsys.readouterr().out)
    readiness = payload["readiness"]

    assert readiness["case_id"] == "slack_status_ready_001"
    assert readiness["case_visible"] is True
    assert readiness["recorded_response_present"] is True
    assert readiness["score_save_enabled"] is True
    assert readiness["slack_run_recorded"] is True
    assert readiness["latest_run_source"] == "slack"
    assert readiness["latest_run_id"] == "wi_status_ready"
    assert readiness["dashboard_case_url"].endswith("?case=slack_status_ready_001")
    assert readiness["review_case_url"].endswith("?case=slack_status_ready_001")
    assert readiness["case_bundle_url"].endswith(
        "/api/eval-case-bundle?case=slack_status_ready_001"
    )
    assert readiness["in_follow_up_queue"] is True
    assert "human_review" in readiness["missing_labels"]
    assert readiness["trace_event_visible"] is True
    assert readiness["trace_diagnostics_visible"] is True


def test_promptfoo_eval_db_status_text_shows_readiness_and_links(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_status_text_001",
        run_id="wi_status_text",
        agent="business_research_analyst",
        request_text="@KNI business research analyst summarize selected thread",
        result_summary="Status text response.",
        database_path=database_path,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "promptfoo_eval_db.py",
            "--database-path",
            str(database_path),
            "status",
            "--case-id",
            "slack_status_text_001",
        ],
    )

    assert promptfoo_eval_db_script.main() == 0
    output = capsys.readouterr().out

    assert "Readiness: case_visible=True score_save_enabled=True" in output
    assert "Next follow-up:" in output
    assert "Dashboard: http://127.0.0.1:8769/dashboard?case=slack_status_text_001" in output
    assert "Review: http://127.0.0.1:8769/review?case=slack_status_text_001" in output
    assert (
        "Case bundle: http://127.0.0.1:8769/api/eval-case-bundle?case=slack_status_text_001"
        in output
    )


def test_human_review_payload_accepts_display_case_id(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    canonical_case_id = "slack_behavioral_health_rfp_001"
    display_case_id = next(
        item["display_case_id"]
        for item in dashboard_payload(database_path=database_path)["cases"]
        if item["case_id"] == canonical_case_id
    )
    record_slack_eval_run(
        case_id=canonical_case_id,
        run_id="wi_display_review",
        agent="opportunity_scout",
        slack_thread_ts="1781206953.875749",
        request_text='@KNI opportunity scout "find grants or RFPs"',
        result_summary="Saved display-id review run.",
        database_path=database_path,
    )
    html = render_review_form(case_id=display_case_id, database_path=database_path)

    result = save_human_review_payload(
        {
            "case_id": display_case_id,
            "run_id": "wi_display_review",
            "agent": "opportunity_scout",
            "slack_thread_ts": "1781206953.875749",
            "scores": _review_scores(usefulness=5),
            "safety": "pass",
            "notes": "Review saved from display id.",
        },
        database_path=database_path,
        require_recorded_response=True,
    )
    canonical_status = eval_case_status(canonical_case_id, database_path=database_path)
    display_status = eval_case_status(display_case_id, database_path=database_path)
    trace_event = next(
        event
        for event in list_eval_trace_events(database_path=database_path, limit=5)
        if event["event_type"] == "human_review_saved"
    )

    assert f'"case_id": "{canonical_case_id}"' in html
    assert "Saved display-id review run." in html
    assert result["input_case_id"] == display_case_id
    assert result["case_id"] == canonical_case_id
    assert result["dashboard_case_url"].endswith(f"?case={canonical_case_id}")
    assert result["review_case_url"].endswith(f"?case={canonical_case_id}")
    assert result["review_target"]["case_id"] == canonical_case_id
    assert result["post_save_state"]["case_id"] == canonical_case_id
    assert canonical_status["human_review_count"] == 1
    assert canonical_status["latest_target_human_review"]["case_id"] == canonical_case_id
    assert display_status["human_review_count"] == 0
    assert trace_event["group_id"] == canonical_case_id
    assert trace_event["metadata"]["input_case_id"] == display_case_id


def test_analysis_exclusion_payload_accepts_display_case_id(tmp_path) -> None:
    results_path = tmp_path / "latest-eval.json"
    canonical_case_id = "slack_behavioral_health_rfp_001"
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
                                "case_id": canonical_case_id,
                                "agent_under_test": "opportunity_scout",
                                "eval_dimensions": "retrieval, source_type",
                                "user_input": '@KNI opportunity scout "find grants or RFPs"',
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
    display_case_id = next(
        item["display_case_id"]
        for item in dashboard_payload(database_path=database_path)["cases"]
        if item["case_id"] == canonical_case_id
    )

    result = save_analysis_exclusion_payload(
        {
            "eval_id": "eval-dashboard",
            "case_id": display_case_id,
            "excluded": True,
            "reason": "manual duplicate",
        },
        database_path=database_path,
    )
    case = next(
        item
        for item in dashboard_payload(database_path=database_path)["cases"]
        if item["case_id"] == canonical_case_id
    )

    assert result["input_case_id"] == display_case_id
    assert result["case_id"] == canonical_case_id
    assert result["post_save_state"]["case_id"] == canonical_case_id
    assert result["post_save_state"]["case"]["analysis_excluded"] is True
    assert case["analysis_excluded"] is True


def test_promptfoo_eval_docs_cover_manual_trace_recording() -> None:
    docs = Path("docs/PROMPTFOO_EVALS.md").read_text(encoding="utf-8")

    assert "Manual No-API Slack Run Recording" in docs
    assert "scripts/promptfoo_eval_db.py record-slack-run" in docs
    assert "--evidence-json" in docs
    assert "orchestrator_preflight" in docs
    assert "web_extraction" in docs
    assert "tool_summary" in docs
    assert "retry_state" in docs
    assert "/api/follow-up-queue" in docs
    assert "/api/trace-diagnostics" in docs
    assert "Traces menu" in docs


def test_human_review_payload_accepts_matching_slack_target(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_agents_sdk_course_001",
        run_id="wi_expected",
        agent="opportunity_scout",
        slack_thread_ts="1781206953.875749",
        request_text="@KNI opportunity scout -- find three Agents SDK courses",
        result_summary="Course scan complete.",
        database_path=database_path,
    )

    result = save_human_review_payload(
        {
            "case_id": "slack_agents_sdk_course_001",
            "run_id": "wi_expected",
            "agent": "opportunity_scout",
            "slack_thread_ts": "1781206953.875749",
            "scores": _review_scores(relevance=5),
            "safety": "pass",
        },
        database_path=database_path,
        require_recorded_response=True,
    )

    assert result["case_id"] == "slack_agents_sdk_course_001"
    assert result["run_id"] == "wi_expected"
    assert result["dashboard_case_url"].endswith("?case=slack_agents_sdk_course_001")
    assert result["review_case_url"].endswith("?case=slack_agents_sdk_course_001")
    assert result["review_target"] == {
        "case_id": "slack_agents_sdk_course_001",
        "target_type": "slack",
        "run_id": "wi_expected",
        "agent": "opportunity_scout",
        "slack_thread_ts": "1781206953.875749",
        "storage_mode": "local_review",
        "validated": True,
    }
    assert result["refresh_targets"] == [
        "overview",
        "human_review",
        "database",
        "runs_scoring",
        "analysis",
    ]
    assert result["trace_event_type"] == "human_review_saved"
    post_save = result["post_save_state"]
    assert post_save["case_id"] == "slack_agents_sdk_course_001"
    assert post_save["case"]["human_average"] == result["average_score"]
    assert post_save["case"]["check_statuses"]["human_review"] == "complete"
    assert post_save["summary"]["human_reviewed"] == 1
    assert post_save["analysis"]["human_case_trend_days"] == 1
    assert post_save["dashboard_visibility"] == {
        "case_visible": True,
        "display_case_id": "slack_agents_sdk_course_001",
        "review_visible": True,
        "analysis_human_review_visible": True,
        "in_follow_up_queue": True,
    }
    assert post_save["trace"]["human_review_saved_present"] is True
    assert post_save["trace"]["event_type"] == "human_review_saved"
    assert post_save["trace"]["trace_id"] == f"human_review:{result['id']}"
    assert post_save["trace"]["span_id"] == f"human_review_row:{result['id']}"
    assert post_save["trace"]["group_id"] == "slack_agents_sdk_course_001"
    assert post_save["follow_up"]["still_open"] is True
    assert "human_review" not in post_save["follow_up"]["missing_labels"]
    assert "machine_check" in post_save["follow_up"]["missing_labels"]
    assert post_save["follow_up"]["queue_item"]["case_id"] == "slack_agents_sdk_course_001"
    assert result["refresh_endpoints"] == [
        "/api/status?refresh=1",
        "/api/eval-cases",
        "/api/follow-up-queue",
        "/api/data-quality",
        "/api/eval-run-ledger",
        "/api/trace-diagnostics",
        "/api/eval-case-bundle?case=slack_agents_sdk_course_001",
    ]
    events = list_eval_trace_events(database_path=database_path, limit=5)
    review_event = next(event for event in events if event["event_type"] == "human_review_saved")
    assert review_event["group_id"] == "slack_agents_sdk_course_001"
    assert review_event["metadata"]["source"] == "human_review"
    assert review_event["metadata"]["score_dimension_count"] == len(SCORE_DIMENSIONS)
    assert review_event["metadata"]["target_type"] == "slack"
    assert review_event["metadata"]["review_target"] == result["review_target"]


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
    assert data["summary"]["coverage"]["airtable_context_agent"]["target"] == 2
    assert data["summary"]["coverage"]["google_workspace_context_agent"]["target"] == 2
    assert data["summary"]["coverage"]["zotero_context_agent"]["target"] == 1
    expected_complete_agents = sum(
        1
        for item in data["summary"]["coverage"].values()
        if int(item["count"]) >= int(item["target"])
    )
    expected_gap_total = sum(
        max(0, int(item["target"]) - int(item["count"]))
        for item in data["summary"]["coverage"].values()
    )
    assert data["summary"]["coverage_complete_agents"] == expected_complete_agents
    assert data["summary"]["coverage_gap_total"] == expected_gap_total
    assert (
        data["summary"]["coverage_target_label"]
        == "core 15; Chief 20; context starter set"
    )
    assert data["summary"]["coverage_source"] == "promptfoo seed cases"
    assert "Update review" in html
    assert "Human notes" in html
    assert 'data-score="${escapeHtml(dimension)}"' in html
    assert "result.post_save_state?.follow_up" in html
    assert "Next: ${followUp.follow_up_summary}." in html
    assert "verifyLocalRefreshEndpoints(result)" in html
    assert "saved row but refresh check failed" in html
    assert "databaseFreshnessHint(result)" in html
    assert "Verified ${refreshed.length} local views" in html


def test_eval_dashboard_server_saves_form_review_payload(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_agents_sdk_course_001",
        run_id="wi_course",
        agent="opportunity_scout",
        slack_thread_ts="1781206953.875749",
        request_text="@KNI opportunity scout -- find three Agents SDK courses",
        result_summary="Course scan complete.",
        database_path=database_path,
    )
    result = save_human_review_payload(
        {
            "case_id": "slack_agents_sdk_course_001",
            "run_id": "wi_course",
            "agent": "opportunity_scout",
            "scores": _review_scores(relevance=5, usefulness=5),
            "safety": "pass",
            "notes": "Useful and readable.",
            "slack_thread_ts": "1781206953.875749",
        },
        database_path=database_path,
    )

    assert result["case_id"] == "slack_agents_sdk_course_001"
    assert result["average_score"] == 4.182
    assert result["review_target"]["target_type"] == "slack"
    assert result["review_target"]["run_id"] == "wi_course"
    assert result["post_save_state"]["case"]["human_average"] == 4.182
    assert result["post_save_state"]["analysis"]["human_case_trend_days"] == 1
    assert result["post_save_state"]["dashboard_visibility"] == {
        "case_visible": True,
        "display_case_id": "slack_agents_sdk_course_001",
        "review_visible": True,
        "analysis_human_review_visible": True,
        "in_follow_up_queue": True,
    }
    assert result["post_save_state"]["trace"]["human_review_saved_present"] is True
    assert result["post_save_state"]["trace"]["trace_id"] == f"human_review:{result['id']}"
    assert result["post_save_state"]["trace"]["span_id"] == f"human_review_row:{result['id']}"
    human_table = result["database_tables"]["human_eval_reviews"]
    trace_table = result["database_tables"]["eval_trace_events"]
    assert human_table["row_count"] == 1
    assert human_table["latest"]["case_id"] == "slack_agents_sdk_course_001"
    assert human_table["latest"]["run_id"] == "wi_course"
    assert human_table["latest"]["average_score"] == 4.182
    assert trace_table["latest"]["event_type"] == "human_review_saved"
    assert trace_table["latest"]["group_id"] == "slack_agents_sdk_course_001"
    assert "/api/status?refresh=1" in result["refresh_endpoints"]
    stored = list_human_reviews(
        database_path=database_path,
        case_id="slack_agents_sdk_course_001",
    )
    assert stored[0]["run_id"] == "wi_course"
    assert stored[0]["scores"]["relevance"] == 5
    assert stored[0]["notes"] == "Useful and readable."


def test_human_review_post_save_state_marks_completed_case_out_of_queue(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    results_path = tmp_path / "latest-eval.json"
    results_path.write_text(
        json.dumps(
            {
                "evalId": "eval-reviewed-complete",
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
                                "case_id": "slack_review_complete_001",
                                "agent_under_test": "business_research_analyst",
                                "eval_dimensions": "retrieval, synthesis",
                                "user_input": "@KNI business research analyst summarize selected thread",
                            },
                            "gradingResult": {"reason": "All assertions passed"},
                            "response": {"output": "{\"human_summary\":\"Complete response.\"}"},
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    import_promptfoo_results(results_path, database_path=database_path)
    record_slack_eval_run(
        case_id="slack_review_complete_001",
        run_id="wi_review_complete",
        agent="business_research_analyst",
        slack_thread_ts="1781206953.875749",
        request_text="@KNI business research analyst summarize selected thread",
        result_summary="Complete response.",
        thread_fetch_status="ok",
        thread_message_count=2,
        source_count=2,
        visible_source_count=2,
        database_path=database_path,
    )

    result = save_human_review_payload(
        {
            "case_id": "slack_review_complete_001",
            "run_id": "wi_review_complete",
            "agent": "business_research_analyst",
            "scores": _review_scores(relevance=5, usefulness=5),
            "safety": "pass",
            "slack_thread_ts": "1781206953.875749",
        },
        database_path=database_path,
        require_recorded_response=True,
    )

    follow_up = result["post_save_state"]["follow_up"]
    queue = follow_up_queue_response(database_path=database_path)["rows"]

    assert follow_up["still_open"] is False
    assert follow_up["queue_item"] == {}
    assert follow_up["missing_labels"] == []
    assert follow_up["attention_labels"] == []
    assert follow_up["next_follow_up"] == (
        "Ready to compare prompt, response, machine score, Orchestrator Review, human review, evidence, and analysis movement."
    )
    assert result["post_save_state"]["dashboard_visibility"] == {
        "case_visible": True,
        "display_case_id": "slack_review_complete_001",
        "review_visible": True,
        "analysis_human_review_visible": True,
        "in_follow_up_queue": False,
    }
    assert all(item["case_id"] != "slack_review_complete_001" for item in queue)


def test_eval_review_form_blocks_unrun_case_until_response_exists(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"

    html = render_review_form(
        case_id="slack_agents_sdk_course_001",
        database_path=database_path,
    )

    assert "Review controls are disabled." in html
    assert "No Promptfoo result or saved #evals Slack response is recorded." in html
    assert '<button type="submit" disabled>Update review</button>' in html
    assert "Human notes" in html
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
    assert "verifyLocalRefreshEndpoints(result)" in html
    assert "saved row but refresh check failed" in html
    assert "databaseFreshnessHint(result)" in html
    assert "Verified ${refreshed.length} local dashboard views" in html
    assert "Reloading review page..." in html
    assert "setTimeout(() => window.location.reload(), 500)" in html
    result = save_human_review_payload(
        {
            "case_id": "slack_agents_sdk_course_001",
            "run_id": "wi_course",
            "agent": "opportunity_scout",
            "slack_thread_ts": "1781206953.875749",
            "scores": _review_scores(relevance=5),
            "safety": "pass",
        },
        database_path=database_path,
        require_recorded_response=True,
    )

    assert '<button type="submit">Update review</button>' in html
    assert "Course scan complete." in html
    assert '<option value="" selected>tbd</option>' in html
    assert '<option value="pass" selected>' not in html
    assert result["average_score"] == 4.091


def test_eval_review_html_uses_scored_response_for_server_side_readiness() -> None:
    html = eval_dashboard_module._review_html(
        {
            "case": {
                "case_id": "slack_scored_response_only_001",
                "agent": "opportunity_scout",
                "dimensions": ["human_scoring"],
                "user_input": "@KNI opportunity scout score this saved response",
                "scored_response_text": "Saved response selected for scoring.",
                "response_text": "",
                "latest_slack_summary": "",
                "human_scores": {},
                "human_notes": "",
                "human_safety": "",
            },
            "score_dimensions": ["accuracy"],
        }
    )

    assert "Saved response selected for scoring." in html
    assert '<button type="submit">Update review</button>' in html
    assert '<button type="submit" disabled>Update review</button>' not in html
    assert "Review controls are disabled." not in html


def test_eval_dashboard_review_target_uses_promptfoo_eval_for_machine_only_case(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    results_path = tmp_path / "promptfoo-target.json"
    results_path.write_text(
        json.dumps(
            {
                "evalId": "eval-promptfoo-target",
                "results": {
                    "timestamp": "2026-06-11T18:00:00Z",
                    "stats": {"successes": 1, "failures": 0, "errors": 0},
                    "results": [
                        {
                            "id": "result-promptfoo-target",
                            "success": True,
                            "score": 1,
                            "vars": {
                                "case_id": "slack_agents_sdk_course_001",
                                "agent_under_test": "opportunity_scout",
                                "eval_dimensions": "retrieval, synthesis",
                                "user_input": (
                                    "@KNI opportunity scout -- find three Agents SDK courses that are "
                                    "reasonably cost and good for someone with some experience"
                                ),
                            },
                            "gradingResult": {"reason": "All assertions passed"},
                            "response": {
                                "output": json.dumps(
                                    {"human_summary": "Promptfoo-only response is saved."}
                                )
                            },
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    import_promptfoo_results(results_path, database_path=database_path)

    payload = dashboard_payload(database_path=database_path)
    case = next(item for item in payload["cases"] if item["case_id"] == "slack_agents_sdk_course_001")
    html = render_review_form(case_id="slack_agents_sdk_course_001", database_path=database_path)

    assert case["review_target"] == {
        "target_type": "promptfoo",
        "run_id": "eval-promptfoo-target",
        "slack_thread_ts": "",
        "agent": "opportunity_scout",
    }
    assert case["scored_response_text"] == "Promptfoo-only response is saved."
    assert "Promptfoo-only response is saved." in html
    assert '"review_target"' in html
    assert "reviewTarget.run_id || item.latest_slack_run_id || item.promptfoo_eval_id" in html


def test_eval_dashboard_review_target_prefers_saved_slack_response(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_agents_sdk_course_001",
        run_id="wi_course",
        agent="opportunity_scout",
        slack_thread_ts="1781206953.875749",
        request_text="@KNI opportunity scout -- find three Agents SDK courses",
        result_summary="Course scan complete.",
        database_path=database_path,
    )

    payload = dashboard_payload(database_path=database_path)
    case = next(item for item in payload["cases"] if item["case_id"] == "slack_agents_sdk_course_001")

    assert case["review_target"] == {
        "target_type": "slack",
        "run_id": "wi_course",
        "slack_thread_ts": "1781206953.875749",
        "agent": "opportunity_scout",
    }
    assert case["scored_response_text"] == "Course scan complete."


def test_eval_dashboard_review_response_aligns_with_slack_target_when_machine_row_exists(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    results_path = tmp_path / "promptfoo-plus-slack.json"
    results_path.write_text(
        json.dumps(
            {
                "evalId": "eval-mixed-target",
                "results": {
                    "timestamp": "2026-06-11T18:00:00Z",
                    "stats": {"successes": 1, "failures": 0, "errors": 0},
                    "results": [
                        {
                            "id": "result-mixed-target",
                            "success": True,
                            "score": 1,
                            "vars": {
                                "case_id": "slack_agents_sdk_course_001",
                                "agent_under_test": "opportunity_scout",
                                "eval_dimensions": "retrieval, synthesis",
                                "user_input": (
                                    "@KNI opportunity scout -- find three Agents SDK courses that are "
                                    "reasonably cost and good for someone with some experience"
                                ),
                            },
                            "gradingResult": {"reason": "All assertions passed"},
                            "response": {
                                "output": json.dumps(
                                    {"human_summary": "Promptfoo machine response should not be the scored Slack response."}
                                )
                            },
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    import_promptfoo_results(results_path, database_path=database_path)
    record_slack_eval_run(
        case_id="slack_agents_sdk_course_001",
        run_id="wi_course",
        agent="opportunity_scout",
        slack_thread_ts="1781206953.875749",
        request_text=(
            "@KNI opportunity scout -- find three Agents SDK courses that are "
            "reasonably cost and good for someone with some experience"
        ),
        result_summary="Slack response selected for human scoring.",
        database_path=database_path,
    )

    payload = dashboard_payload(database_path=database_path)
    case = next(item for item in payload["cases"] if item["case_id"] == "slack_agents_sdk_course_001")
    html = render_review_form(case_id="slack_agents_sdk_course_001", database_path=database_path)

    assert case["review_target"]["target_type"] == "slack"
    assert case["review_target"]["run_id"] == "wi_course"
    assert case["response_text"] == "Promptfoo machine response should not be the scored Slack response."
    assert case["scored_response_text"] == "Slack response selected for human scoring."
    assert "Slack response selected for human scoring." in html


def test_eval_review_payload_requires_explicit_scores_and_safety(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_agents_sdk_course_001",
        run_id="wi_course",
        agent="opportunity_scout",
        request_text="@KNI opportunity scout -- find three Agents SDK courses",
        result_summary="Course scan complete.",
        database_path=database_path,
    )
    scores = _review_scores()
    scores["accuracy"] = ""

    with pytest.raises(ValueError, match="accuracy score is required"):
        save_human_review_payload(
            {
                "case_id": "slack_agents_sdk_course_001",
                "scores": scores,
                "safety": "pass",
            },
            database_path=database_path,
            require_recorded_response=True,
        )
    with pytest.raises(ValueError, match="safety is required"):
        save_human_review_payload(
            {
                "case_id": "slack_agents_sdk_course_001",
                "scores": _review_scores(),
                "safety": "",
            },
            database_path=database_path,
            require_recorded_response=True,
        )


def test_slack_eval_run_records_queryable_evidence(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"

    record_slack_eval_run(
        case_id="slack_evidence_001",
        run_id="wi_evidence",
        agent="business_research_analyst",
        work_item_id="wi_evidence",
        slack_channel_id="C0BA17Y9C01",
        slack_channel_name="evals",
        slack_thread_ts="1781206953.875749",
        permalink="https://kni.slack.com/archives/C0BA17Y9C01/p1781206953875749",
        request_text="@KNI business research analyst research Acme",
        result_summary="Acme research complete.",
        route="business_research_analyst",
        status="done",
        context_policy="selected_message_or_thread_recent_window",
        thread_fetch_status="ok",
        thread_message_count=2,
        warning_count=1,
        warnings=["bounded thread context"],
        cost_profile="slack_research_balanced",
        source_count=3,
        visible_source_count=2,
        sdk_estimated_cost_usd=0.012,
        sdk_cache_hit_rate=0.5,
        response_hash="abc123",
        evidence={"schema": "keystone.slack.eval_evidence.v1"},
        database_path=database_path,
    )

    status = eval_case_status("slack_evidence_001", database_path=database_path)
    latest = status["slack_runs"][0]

    assert latest["work_item_id"] == "wi_evidence"
    assert latest["thread_fetch_status"] == "ok"
    assert latest["warnings"] == ["bounded thread context"]
    assert latest["cost_profile"] == "slack_research_balanced"
    assert latest["visible_source_count"] == 2
    assert latest["sdk_cache_hit_rate"] == 0.5
    assert latest["evidence"]["schema"] == "keystone.slack.eval_evidence.v1"
    events = list_eval_trace_events(database_path=database_path, limit=5)
    slack_event = next(event for event in events if event["event_type"] == "slack_run_saved")
    assert slack_event["group_id"] == "slack_evidence_001"
    assert slack_event["metadata"]["source"] == "slack"
    assert slack_event["metadata"]["thread_message_count"] == 2
    assert "result_summary" not in slack_event["metadata"]
    assert "request_text" not in slack_event["metadata"]


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
        "Slack review form open",
        "Submit Evaluation",
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


def test_eval_dashboard_server_dynamic_responses_disable_cache(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    dashboard_path = tmp_path / "dashboard.html"
    handler = eval_dashboard_server.build_handler(
        database_path=database_path,
        dashboard_path=dashboard_path,
        limit=100,
    )

    def captured_request() -> object:
        request = object.__new__(handler)
        request.status = None
        request.headers = {}
        request.body = b""
        request.send_response = lambda status: setattr(request, "status", status)
        request.send_header = lambda key, value: request.headers.__setitem__(key, value)
        request.end_headers = lambda: None
        request._write_body = lambda body: setattr(request, "body", body)
        return request

    dashboard_request = captured_request()
    dashboard_request._serve_dashboard()
    status_request = captured_request()
    status_request._send_json({"status": "ok", "database_path": str(database_path)})

    for request in (dashboard_request, status_request):
        assert request.status == 200
        assert request.headers["Cache-Control"] == eval_dashboard_server.NO_STORE_CACHE_CONTROL
        assert request.headers["Pragma"] == "no-cache"
        assert request.headers["Expires"] == "0"


def test_eval_dashboard_status_endpoint_does_not_render_dashboard(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "evals.sqlite"
    dashboard_path = tmp_path / "dashboard.html"
    database_path.write_text("db-v1", encoding="utf-8")

    def fail_render_dashboard(**_kwargs: object) -> Path:
        raise AssertionError("/api/status should not render the dashboard")

    monkeypatch.setattr(eval_dashboard_server, "render_dashboard", fail_render_dashboard)
    handler = eval_dashboard_server.build_handler(
        database_path=database_path,
        dashboard_path=dashboard_path,
        limit=100,
    )
    request = object.__new__(handler)
    request.path = "/api/status"
    request.status = None
    request.headers = {}
    request.body = b""
    request.send_response = lambda status: setattr(request, "status", status)
    request.send_header = lambda key, value: request.headers.__setitem__(key, value)
    request.end_headers = lambda: None
    request._write_body = lambda body: setattr(request, "body", body)

    request.do_GET()

    assert request.status == 200
    assert request.headers["Content-Type"] == "application/json; charset=utf-8"
    payload = json.loads(request.body.decode("utf-8"))
    assert payload["status"] == "ok"
    assert payload["database_path"] == str(database_path)
    assert payload["render_cache_ttl_seconds"] == eval_dashboard_server.SERVER_RENDER_CACHE_TTL_SECONDS
    assert payload["render_cache_refresh_endpoint"] == "/api/status?refresh=1"
    assert payload["render_cache_refresh_requested"] is False
    assert payload["render_cache_refreshed"] is False
    assert payload["database_signature"][0][0] == database_path.name
    assert payload["dashboard_cache"] == {
        "present": False,
        "fresh_for_database": False,
        "expires_in_seconds": 0.0,
    }
    assert set(payload["database_tables"]) == {
        "promptfoo_eval_runs",
        "promptfoo_case_results",
        "slack_eval_runs",
        "human_eval_reviews",
        "eval_trace_events",
    }
    assert payload["database_tables"]["slack_eval_runs"] == {
        "exists": False,
        "row_count": 0,
        "latest_at": "",
        "timestamp_column": "created_at",
        "latest": {},
    }


def test_eval_dashboard_status_endpoint_reports_database_table_summaries(
    tmp_path, monkeypatch
) -> None:
    database_path = tmp_path / "evals.sqlite"
    dashboard_path = tmp_path / "dashboard.html"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE promptfoo_eval_runs (
                eval_id TEXT,
                created_at TEXT,
                imported_at TEXT NOT NULL,
                total INTEGER,
                successes INTEGER,
                failures INTEGER,
                errors INTEGER
            )
            """
        )
        connection.execute(
            "INSERT INTO promptfoo_eval_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("eval-old", "2026-06-14T19:59:00Z", "2026-06-14T20:00:00Z", 1, 1, 0, 0),
        )
        connection.execute(
            "INSERT INTO promptfoo_eval_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("eval-new", "2026-06-14T20:59:00Z", "2026-06-14T21:00:00Z", 2, 2, 0, 0),
        )
        connection.execute(
            """
            CREATE TABLE slack_eval_runs (
                case_id TEXT,
                run_id TEXT,
                work_item_id TEXT,
                agent TEXT,
                status TEXT,
                created_at TEXT NOT NULL,
                warning_count INTEGER
            )
            """
        )
        connection.execute(
            "INSERT INTO slack_eval_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "slack_case_new_001",
                "wi_latest",
                "wi_latest",
                "business_research_analyst",
                "done",
                "2026-06-14T20:57:16Z",
                1,
            ),
        )
        connection.execute(
            """
            CREATE TABLE human_eval_reviews (
                case_id TEXT,
                run_id TEXT,
                agent TEXT,
                average_score REAL,
                safety TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE eval_trace_events (
                event_type TEXT,
                trace_id TEXT,
                span_id TEXT,
                group_id TEXT,
                name TEXT,
                duration_ms REAL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO eval_trace_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "manual_run_summary",
                "manual_run:wi_latest",
                "slack_run_summary:1",
                "slack_case_new_001",
                "slack_eval_manual_run_summary",
                None,
                "2026-06-14T21:29:31Z",
            ),
        )

    def fail_render_dashboard(**_kwargs: object) -> Path:
        raise AssertionError("/api/status should not render the dashboard")

    monkeypatch.setattr(eval_dashboard_server, "render_dashboard", fail_render_dashboard)
    handler = eval_dashboard_server.build_handler(
        database_path=database_path,
        dashboard_path=dashboard_path,
        limit=100,
    )
    request = object.__new__(handler)
    request.path = "/api/status"
    request.status = None
    request.headers = {}
    request.body = b""
    request.send_response = lambda status: setattr(request, "status", status)
    request.send_header = lambda key, value: request.headers.__setitem__(key, value)
    request.end_headers = lambda: None
    request._write_body = lambda body: setattr(request, "body", body)

    request.do_GET()

    payload = json.loads(request.body.decode("utf-8"))
    tables = payload["database_tables"]
    assert tables["promptfoo_eval_runs"] == {
        "exists": True,
        "row_count": 2,
        "latest_at": "2026-06-14T21:00:00Z",
        "timestamp_column": "imported_at",
        "latest": {
            "eval_id": "eval-new",
            "created_at": "2026-06-14T20:59:00Z",
            "imported_at": "2026-06-14T21:00:00Z",
            "total": 2,
            "successes": 2,
            "failures": 0,
            "errors": 0,
        },
    }
    assert tables["slack_eval_runs"] == {
        "exists": True,
        "row_count": 1,
        "latest_at": "2026-06-14T20:57:16Z",
        "timestamp_column": "created_at",
        "latest": {
            "case_id": "slack_case_new_001",
            "run_id": "wi_latest",
            "work_item_id": "wi_latest",
            "agent": "business_research_analyst",
            "status": "done",
            "warning_count": 1,
            "created_at": "2026-06-14T20:57:16Z",
        },
    }
    assert tables["human_eval_reviews"] == {
        "exists": True,
        "row_count": 0,
        "latest_at": "",
        "timestamp_column": "created_at",
        "latest": {},
    }
    assert tables["eval_trace_events"] == {
        "exists": True,
        "row_count": 1,
        "latest_at": "2026-06-14T21:29:31Z",
        "timestamp_column": "created_at",
        "latest": {
            "event_type": "manual_run_summary",
            "trace_id": "manual_run:wi_latest",
            "span_id": "slack_run_summary:1",
            "group_id": "slack_case_new_001",
            "name": "slack_eval_manual_run_summary",
            "created_at": "2026-06-14T21:29:31Z",
        },
    }
    assert tables["promptfoo_case_results"] == {
        "exists": False,
        "row_count": 0,
        "latest_at": "",
        "timestamp_column": "imported_at",
        "latest": {},
    }


def test_eval_dashboard_status_endpoint_reports_cache_freshness(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "evals.sqlite"
    dashboard_path = tmp_path / "dashboard.html"
    database_path.write_text("db-v1", encoding="utf-8")
    wal_path = Path(f"{database_path}-wal")

    def fake_render_dashboard(*, database_path: Path, output_path: Path, limit: int) -> Path:
        del database_path, limit
        output_path.write_text("<html>cached</html>", encoding="utf-8")
        return output_path

    monkeypatch.setattr(eval_dashboard_server, "render_dashboard", fake_render_dashboard)
    handler = eval_dashboard_server.build_handler(
        database_path=database_path,
        dashboard_path=dashboard_path,
        limit=100,
    )

    def captured_request(path: str = "/api/status") -> object:
        request = object.__new__(handler)
        request.path = path
        request.status = None
        request.headers = {}
        request.body = b""
        request.send_response = lambda status: setattr(request, "status", status)
        request.send_header = lambda key, value: request.headers.__setitem__(key, value)
        request.end_headers = lambda: None
        request._write_body = lambda body: setattr(request, "body", body)
        return request

    warm_request = captured_request()
    warm_request._serve_dashboard()
    fresh_request = captured_request()
    fresh_request.do_GET()
    wal_path.write_text("wal-write-without-main-db-change", encoding="utf-8")
    stale_request = captured_request()
    stale_request.do_GET()

    fresh_payload = json.loads(fresh_request.body.decode("utf-8"))
    stale_payload = json.loads(stale_request.body.decode("utf-8"))
    assert fresh_payload["dashboard_cache"]["present"] is True
    assert fresh_payload["dashboard_cache"]["fresh_for_database"] is True
    assert fresh_payload["dashboard_cache"]["expires_in_seconds"] > 0
    assert fresh_payload["render_cache_refresh_requested"] is False
    assert fresh_payload["render_cache_refreshed"] is False
    assert stale_payload["dashboard_cache"]["present"] is True
    assert stale_payload["dashboard_cache"]["fresh_for_database"] is False
    assert stale_payload["render_cache_refresh_requested"] is False
    assert stale_payload["render_cache_refreshed"] is False
    refresh_request = captured_request("/api/status?refresh=1")
    refresh_request.do_GET()
    refresh_payload = json.loads(refresh_request.body.decode("utf-8"))
    assert refresh_payload["render_cache_refresh_endpoint"] == "/api/status?refresh=1"
    assert refresh_payload["render_cache_refresh_requested"] is True
    assert refresh_payload["render_cache_refreshed"] is True
    assert refresh_payload["dashboard_cache"]["present"] is True
    assert refresh_payload["dashboard_cache"]["fresh_for_database"] is True
    assert refresh_payload["dashboard_cache"]["expires_in_seconds"] > 0


def test_eval_dashboard_server_reuses_short_lived_render_cache(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "evals.sqlite"
    dashboard_path = tmp_path / "dashboard.html"
    render_calls = 0

    def fake_render_dashboard(*, database_path: Path, output_path: Path, limit: int) -> Path:
        nonlocal render_calls
        render_calls += 1
        output_path.write_text(f"<html>render {render_calls}</html>", encoding="utf-8")
        return output_path

    monkeypatch.setattr(eval_dashboard_server, "render_dashboard", fake_render_dashboard)
    handler = eval_dashboard_server.build_handler(
        database_path=database_path,
        dashboard_path=dashboard_path,
        limit=100,
    )

    def captured_request() -> object:
        request = object.__new__(handler)
        request.status = None
        request.headers = {}
        request.body = b""
        request.send_response = lambda status: setattr(request, "status", status)
        request.send_header = lambda key, value: request.headers.__setitem__(key, value)
        request.end_headers = lambda: None
        request._write_body = lambda body: setattr(request, "body", body)
        return request

    first_request = captured_request()
    first_request._serve_dashboard()
    second_request = captured_request()
    second_request._serve_dashboard()

    assert first_request.status == 200
    assert second_request.status == 200
    assert first_request.body == second_request.body
    assert render_calls == 1


def test_eval_dashboard_server_refreshes_dashboard_cache_when_database_changes(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "evals.sqlite"
    dashboard_path = tmp_path / "dashboard.html"
    database_path.write_text("db-v1", encoding="utf-8")
    render_calls = 0

    def fake_render_dashboard(*, database_path: Path, output_path: Path, limit: int) -> Path:
        del database_path, limit
        nonlocal render_calls
        render_calls += 1
        output_path.write_text(f"<html>render {render_calls}</html>", encoding="utf-8")
        return output_path

    monkeypatch.setattr(eval_dashboard_server, "render_dashboard", fake_render_dashboard)
    handler = eval_dashboard_server.build_handler(
        database_path=database_path,
        dashboard_path=dashboard_path,
        limit=100,
    )

    def captured_request() -> object:
        request = object.__new__(handler)
        request.status = None
        request.headers = {}
        request.body = b""
        request.send_response = lambda status: setattr(request, "status", status)
        request.send_header = lambda key, value: request.headers.__setitem__(key, value)
        request.end_headers = lambda: None
        request._write_body = lambda body: setattr(request, "body", body)
        return request

    first_request = captured_request()
    first_request._serve_dashboard()
    second_request = captured_request()
    second_request._serve_dashboard()
    database_path.write_text("db-v2-changed", encoding="utf-8")
    third_request = captured_request()
    third_request._serve_dashboard()

    assert first_request.body == b"<html>render 1</html>"
    assert second_request.body == first_request.body
    assert third_request.body == b"<html>render 2</html>"
    assert render_calls == 2


def test_eval_dashboard_server_refreshes_dashboard_cache_when_sqlite_wal_changes(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "evals.sqlite"
    dashboard_path = tmp_path / "dashboard.html"
    database_path.write_text("db-v1", encoding="utf-8")
    wal_path = Path(f"{database_path}-wal")
    render_calls = 0

    def fake_render_dashboard(*, database_path: Path, output_path: Path, limit: int) -> Path:
        del database_path, limit
        nonlocal render_calls
        render_calls += 1
        output_path.write_text(f"<html>render {render_calls}</html>", encoding="utf-8")
        return output_path

    monkeypatch.setattr(eval_dashboard_server, "render_dashboard", fake_render_dashboard)
    handler = eval_dashboard_server.build_handler(
        database_path=database_path,
        dashboard_path=dashboard_path,
        limit=100,
    )

    def captured_request() -> object:
        request = object.__new__(handler)
        request.status = None
        request.headers = {}
        request.body = b""
        request.send_response = lambda status: setattr(request, "status", status)
        request.send_header = lambda key, value: request.headers.__setitem__(key, value)
        request.end_headers = lambda: None
        request._write_body = lambda body: setattr(request, "body", body)
        return request

    first_request = captured_request()
    first_request._serve_dashboard()
    wal_path.write_text("wal-write-without-main-db-change", encoding="utf-8")
    second_request = captured_request()
    second_request._serve_dashboard()

    assert first_request.body == b"<html>render 1</html>"
    assert second_request.body == b"<html>render 2</html>"
    assert render_calls == 2


def test_eval_dashboard_server_serves_stale_dashboard_when_refresh_fails(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "evals.sqlite"
    dashboard_path = tmp_path / "dashboard.html"
    database_path.write_text("db-v1", encoding="utf-8")
    render_calls = 0

    def fake_render_dashboard(*, database_path: Path, output_path: Path, limit: int) -> Path:
        del database_path, limit
        nonlocal render_calls
        render_calls += 1
        if render_calls > 1:
            raise OSError("too many open files")
        output_path.write_text("<html>render 1</html>", encoding="utf-8")
        return output_path

    monkeypatch.setattr(eval_dashboard_server, "render_dashboard", fake_render_dashboard)
    handler = eval_dashboard_server.build_handler(
        database_path=database_path,
        dashboard_path=dashboard_path,
        limit=100,
    )

    def captured_request(path: str = "/dashboard") -> object:
        request = object.__new__(handler)
        request.path = path
        request.status = None
        request.headers = {}
        request.body = b""
        request.send_response = lambda status: setattr(request, "status", status)
        request.send_header = lambda key, value: request.headers.__setitem__(key, value)
        request.end_headers = lambda: None
        request._write_body = lambda body: setattr(request, "body", body)
        return request

    first_request = captured_request()
    first_request._serve_dashboard()
    database_path.write_text("db-v2-changed", encoding="utf-8")
    fallback_request = captured_request()
    fallback_request._serve_dashboard()
    status_request = captured_request("/api/status")
    status_request.do_GET()

    assert first_request.status == 200
    assert fallback_request.status == 200
    assert fallback_request.body == b"<html>render 1</html>"
    payload = json.loads(status_request.body.decode("utf-8"))
    assert payload["render_cache_serving_stale"] is True
    assert "too many open files" in payload["render_cache_last_error"]
    assert payload["dashboard_cache"]["present"] is True
    assert payload["dashboard_cache"]["fresh_for_database"] is False


def test_eval_dashboard_server_serves_stale_dashboard_when_render_lock_is_busy(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "evals.sqlite"
    dashboard_path = tmp_path / "dashboard.html"
    database_path.write_text("db-v1", encoding="utf-8")
    release_render = threading.Event()
    render_calls = 0

    def fake_render_dashboard(*, database_path: Path, output_path: Path, limit: int) -> Path:
        del database_path, limit
        nonlocal render_calls
        render_calls += 1
        if render_calls == 1:
            output_path.write_text("<html>render 1</html>", encoding="utf-8")
            return output_path
        release_render.wait(timeout=3)
        output_path.write_text("<html>render 2</html>", encoding="utf-8")
        return output_path

    monkeypatch.setattr(eval_dashboard_server, "render_dashboard", fake_render_dashboard)
    monkeypatch.setattr(eval_dashboard_server, "SERVER_RENDER_LOCK_TIMEOUT_SECONDS", 0.01)
    handler = eval_dashboard_server.build_handler(
        database_path=database_path,
        dashboard_path=dashboard_path,
        limit=100,
    )

    def captured_request() -> object:
        request = object.__new__(handler)
        request.status = None
        request.headers = {}
        request.body = b""
        request.send_response = lambda status: setattr(request, "status", status)
        request.send_header = lambda key, value: request.headers.__setitem__(key, value)
        request.end_headers = lambda: None
        request._write_body = lambda body: setattr(request, "body", body)
        return request

    warm_request = captured_request()
    warm_request._serve_dashboard()
    database_path.write_text("db-v2-changed", encoding="utf-8")
    slow_request = captured_request()
    slow_thread = threading.Thread(target=slow_request._serve_dashboard)
    slow_thread.start()
    deadline = time.monotonic() + 1
    while render_calls < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    fallback_request = captured_request()
    fallback_request._serve_dashboard()
    release_render.set()
    slow_thread.join(timeout=3)

    assert warm_request.status == 200
    assert fallback_request.status == 200
    assert fallback_request.body == b"<html>render 1</html>"
    assert slow_request.status == 200


def test_eval_dashboard_status_refresh_reports_stale_fallback(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "evals.sqlite"
    dashboard_path = tmp_path / "dashboard.html"
    database_path.write_text("db-v1", encoding="utf-8")
    render_calls = 0

    def fake_render_dashboard(*, database_path: Path, output_path: Path, limit: int) -> Path:
        del database_path, limit
        nonlocal render_calls
        render_calls += 1
        if render_calls > 1:
            raise OSError("too many open files")
        output_path.write_text("<html>render 1</html>", encoding="utf-8")
        return output_path

    monkeypatch.setattr(eval_dashboard_server, "render_dashboard", fake_render_dashboard)
    handler = eval_dashboard_server.build_handler(
        database_path=database_path,
        dashboard_path=dashboard_path,
        limit=100,
    )

    def captured_request(path: str) -> object:
        request = object.__new__(handler)
        request.path = path
        request.status = None
        request.headers = {}
        request.body = b""
        request.send_response = lambda status: setattr(request, "status", status)
        request.send_header = lambda key, value: request.headers.__setitem__(key, value)
        request.end_headers = lambda: None
        request._write_body = lambda body: setattr(request, "body", body)
        return request

    warm_request = captured_request("/dashboard")
    warm_request._serve_dashboard()
    database_path.write_text("db-v2-changed", encoding="utf-8")
    refresh_request = captured_request("/api/status?refresh=1")
    refresh_request.do_GET()

    payload = json.loads(refresh_request.body.decode("utf-8"))
    assert refresh_request.status == 200
    assert payload["render_cache_refresh_requested"] is True
    assert payload["render_cache_refreshed"] is False
    assert payload["render_cache_serving_stale"] is True
    assert "too many open files" in payload["render_cache_last_error"]
    assert payload["dashboard_cache"]["present"] is True
    assert payload["dashboard_cache"]["fresh_for_database"] is False


def test_eval_dashboard_server_refreshes_review_cache_when_database_changes(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "evals.sqlite"
    dashboard_path = tmp_path / "dashboard.html"
    database_path.write_text("db-v1", encoding="utf-8")
    render_calls = 0

    def fake_render_review_form(*, case_id: str, database_path: Path, limit: int) -> str:
        del case_id, database_path, limit
        nonlocal render_calls
        render_calls += 1
        return f"<html>review {render_calls}</html>"

    monkeypatch.setattr(eval_dashboard_server, "render_review_form", fake_render_review_form)
    handler = eval_dashboard_server.build_handler(
        database_path=database_path,
        dashboard_path=dashboard_path,
        limit=100,
    )

    first_body = object.__new__(handler)._review_body("case-1")
    second_body = object.__new__(handler)._review_body("case-1")
    database_path.write_text("db-v2-changed", encoding="utf-8")
    third_body = object.__new__(handler)._review_body("case-1")

    assert first_body == b"<html>review 1</html>"
    assert second_body == first_body
    assert third_body == b"<html>review 2</html>"
    assert render_calls == 2


def test_eval_run_ledger_response_exposes_normalized_local_events(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    results_path = tmp_path / "latest-eval.json"
    results_path.write_text(
        json.dumps(
            {
                "evalId": "eval-ledger",
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
                                "eval_dimensions": "routing",
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
    record_slack_eval_run(
        case_id="slack_agents_sdk_course_001",
        run_id="wi_ledger",
        agent="opportunity_scout",
        request_text="@KNI opportunity scout -- find three Agents SDK courses",
        result_summary="Course scan complete.",
        thread_fetch_status="selected_message",
        thread_message_count=1,
        database_path=database_path,
    )

    payload = eval_run_ledger_response(database_path=database_path)

    assert payload["status"] == "ok"
    sources = {row["source"] for row in payload["rows"]}
    assert {"promptfoo", "slack"}.issubset(sources)
    slack_row = next(row for row in payload["rows"] if row["source"] == "slack")
    assert slack_row["case_id"] == "slack_agents_sdk_course_001"
    assert slack_row["run_id"] == "wi_ledger"
    assert slack_row["score"] is None
    promptfoo_row = next(row for row in payload["rows"] if row["source"] == "promptfoo")
    assert promptfoo_row["run_id"] == "eval-ledger"
    assert promptfoo_row["status"] == "pass"


def test_trace_diagnostics_response_exposes_rollups_without_live_calls(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_trace_diagnostics_001",
        run_id="wi_trace_diagnostics",
        agent="business_research_analyst",
        request_text="@KNI business research analyst summarize selected thread",
        result_summary="Saved local no-API run.",
        thread_fetch_status="ok",
        thread_message_count=1,
        warning_count=1,
        warnings=["visible_source_gap"],
        source_count=2,
        visible_source_count=1,
        evidence={
            "tool_summary": {
                "tool_call_count": 2,
                "failed_tool_call_count": 1,
                "tools": ["search_web"],
            },
            "web_extraction": {"status": "partial", "issue_count": 1},
            "retry_state": {"retry_count": 1, "status": "retried"},
        },
        database_path=database_path,
    )

    payload = trace_diagnostics_response(database_path=database_path)
    diagnostics = payload["trace_diagnostics"]
    categories = {item["key"]: item["count"] for item in diagnostics["diagnostic_category_counts"]}
    trend_categories = {item["key"] for item in diagnostics["diagnostic_category_trends"]}
    rollup = diagnostics["diagnostic_case_rollups"][0]

    assert payload["status"] == "ok"
    assert payload["mode"] == "local_trace_diagnostics"
    assert payload["live_api_calls"] is False
    assert diagnostics["manual_run_summary_count"] == 1
    assert diagnostics["joined_run_summary_count"] == 1
    assert diagnostics["unjoined_run_summary_count"] == 0
    assert categories["error_or_retry"] == 1
    assert categories["web_extraction_issues"] == 1
    assert categories["tool_failures"] == 1
    assert trend_categories >= {"error_or_retry", "web_extraction_issues", "tool_failures"}
    assert rollup["join_key"] == "slack_trace_diagnostics_001"
    assert rollup["event_count"] == 1
    assert {item["key"] for item in rollup["categories"]} >= {
        "error_or_retry",
        "web_extraction_issues",
        "tool_failures",
    }
    assert diagnostics["diagnostic_followups"][0]["join_key"] == "slack_trace_diagnostics_001"
    assert "raw prompts" in diagnostics["dropped_fields"]
    assert diagnostics["effective_sensitive_capture"] is False


def test_eval_dashboard_server_routes_trace_diagnostics_endpoint() -> None:
    source = Path(eval_dashboard_server.__file__).read_text(encoding="utf-8")

    assert 'path == "/api/trace-diagnostics"' in source
    assert "self._serve_trace_diagnostics()" in source
    assert "self._serve_trace_diagnostics(head_only=True)" in source


def test_eval_case_bundle_response_exports_codex_review_contract(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_agents_sdk_course_001",
        run_id="wi_bundle",
        agent="opportunity_scout",
        slack_channel_name="evals",
        slack_thread_ts="1781206953.875749",
        request_text="@KNI opportunity scout -- find three Agents SDK courses",
        result_summary="Course scan complete.",
        thread_fetch_status="ok",
        thread_message_count=2,
        source_count=2,
        visible_source_count=1,
        warning_count=1,
        warnings=["one source omitted from Slack output"],
        database_path=database_path,
    )

    payload = eval_case_bundle_response(
        case_id="slack_agents_sdk_course_001",
        database_path=database_path,
    )

    bundle = payload["bundle"]
    checks = {item["label"]: item for item in bundle["review_checklist"]}
    assert payload["status"] == "ok"
    assert bundle["schema"] == "keystone.eval.case_review_bundle.v1"
    assert bundle["case_id"] == "slack_agents_sdk_course_001"
    assert bundle["case_bundle_url"].endswith(
        "/api/eval-case-bundle?case=slack_agents_sdk_course_001"
    )
    assert bundle["latest_run"]["source"] == "slack"
    assert bundle["slack"]["run_id"] == "wi_bundle"
    assert bundle["slack"]["source_visibility"] == "1/2"
    assert bundle["slack"]["warnings"] == ["one source omitted from Slack output"]
    assert bundle["latest_response"] == "Course scan complete."
    assert bundle["trace_diagnostics"]["available"] is True
    assert bundle["trace_diagnostics"]["event_count"] == 1
    assert bundle["trace_diagnostics"]["followup_count"] == 1
    diagnostic_categories = {
        item["key"] for item in bundle["trace_diagnostics"]["categories"]
    }
    assert "error_or_retry" in diagnostic_categories
    assert "raw prompts" in bundle["trace_diagnostics"]["copy_policy"]
    assert checks["recorded_response"]["status"] == "complete"
    assert checks["source_visibility"]["status"] == "complete"
    assert checks["slack_warnings"]["status"] == "attention"
    assert bundle["next_follow_up"] == "Import Promptfoo result by case_id before analysis comparison."
    assert "Please review this Keystone eval case" in payload["copy_text"]
    assert "keystone.eval.case_review_bundle.v1" in payload["copy_text"]
    assert "trace_diagnostics" in payload["copy_text"]
    assert "Course scan complete." in payload["copy_text"]


def test_eval_case_bundle_response_exports_promptfoo_scoring_contract_for_agents_as_tools(
    tmp_path,
) -> None:
    payload = eval_case_bundle_response(
        case_id="slack_cos_weekly_eval_review_no_schedule_001",
        database_path=tmp_path / "evals.sqlite",
    )

    bundle = payload["bundle"]
    contract = bundle["scoring_contract"]
    checks = contract["checks"]

    assert contract["schema"] == "keystone.eval.promptfoo_scoring_contract.v1"
    assert contract["assertion"] == "promptfoo.assertions.kba_slack_invariants"
    assert bundle["agent"] == "chief_of_staff"
    assert checks["expected_route"] == "chief_of_staff"
    assert checks["min_artifact_count"] == 1
    assert checks["required_context_sources"] == [
        "airtable",
        "gmail",
        "google_docs",
        "google_drive",
        "google_sheets",
        "slack",
    ]
    assert checks["required_specialist_routes"] == [
        "gmail_triage",
        "google_workspace_context_agent",
        "airtable_context_agent",
    ]
    assert checks["required_payload_terms"] == [
        "Requested context sources tracked for specialist run"
    ]
    assert checks["require_specialist_routes_strict"] is True
    assert checks["forbidden_terms"] == [
        "calendar event created",
        "posted to Slack",
        "sent email",
    ]
    assert "scoring_contract" in payload["copy_text"]
    assert "required_specialist_routes" in payload["copy_text"]


def test_eval_case_bundle_response_exports_source_scoring_contract(tmp_path) -> None:
    payload = eval_case_bundle_response(
        case_id="slack_behavioral_health_rfp_001",
        database_path=tmp_path / "evals.sqlite",
    )

    checks = payload["bundle"]["scoring_contract"]["checks"]

    assert checks["expected_route"] == "opportunity_scout"
    assert checks["expected_pack_type"] == "opportunity"
    assert checks["expected_next_action_agent"] == "business_research_analyst"
    assert checks["min_source_count"] == 1
    assert checks["min_artifact_count"] == 1
    assert checks["required_source_types"] == ["government"]
    assert checks["required_source_url_prefixes"] == ["fixture://government/"]
    assert checks["required_terms"] == ["RFP", "behavioral health"]
    assert checks["forbidden_terms"] == ["sent email", "posted to Slack"]
    assert "required_source_url_prefixes" in payload["copy_text"]


def test_eval_case_bundle_response_bounds_long_copy_text(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    long_response = "A" * 4500 + "TAIL_SHOULD_NOT_COPY"
    record_slack_eval_run(
        case_id="slack_long_copy_case_001",
        run_id="wi_long_copy",
        agent="business_research_analyst",
        request_text="@KNI business research analyst summarize selected source context",
        result_summary=long_response,
        thread_fetch_status="ok",
        thread_message_count=1,
        database_path=database_path,
    )

    payload = eval_case_bundle_response(
        case_id="slack_long_copy_case_001",
        database_path=database_path,
    )
    bundle = payload["bundle"]

    assert bundle["latest_response_truncated"] is True
    assert bundle["latest_response_chars"] == len(long_response)
    assert len(bundle["latest_response"]) == 4000
    assert "TAIL_SHOULD_NOT_COPY" not in payload["copy_text"]
    assert "copy_policy" in payload["copy_text"]
    assert "latest_response_truncated" in payload["copy_text"]


def test_eval_case_bundle_response_rejects_missing_case(tmp_path) -> None:
    with pytest.raises(ValueError, match="case is required"):
        eval_case_bundle_response(case_id="", database_path=tmp_path / "evals.sqlite")
    with pytest.raises(ValueError, match="case not found"):
        eval_case_bundle_response(case_id="missing_case", database_path=tmp_path / "evals.sqlite")


def test_eval_case_bundle_response_accepts_display_case_id(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_behavioral_health_rfp_001",
        run_id="wi_display_bundle",
        agent="opportunity_scout",
        request_text="@KNI opportunity scout \"find grants or RFPs for digital mental health evaluation pilots\"",
        result_summary="Saved response.",
        thread_fetch_status="ok",
        thread_message_count=1,
        database_path=database_path,
    )

    payload = dashboard_payload(database_path=database_path)
    selected = next(item for item in payload["cases"] if item["case_id"] == "slack_behavioral_health_rfp_001")
    assert selected["display_case_id"] != selected["case_id"]

    bundle = eval_case_bundle_response(
        case_id=selected["display_case_id"],
        database_path=database_path,
    )["bundle"]

    assert bundle["case_id"] == "slack_behavioral_health_rfp_001"
    assert bundle["display_case_id"] == selected["display_case_id"]
    assert bundle["slack"]["run_id"] == "wi_display_bundle"


def test_follow_up_queue_uses_canonical_case_label_for_actions(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_behavioral_health_rfp_001",
        run_id="wi_display_queue",
        agent="opportunity_scout",
        request_text="@KNI opportunity scout \"find grants or RFPs for digital mental health evaluation pilots\"",
        result_summary="Saved response.",
        thread_fetch_status="",
        thread_message_count=0,
        database_path=database_path,
    )

    payload = dashboard_payload(database_path=database_path)
    queue_item = next(
        item
        for item in payload["follow_up_queue"]
        if item["case_id"] == "slack_behavioral_health_rfp_001"
    )
    html = render_dashboard(database_path=database_path, output_path=tmp_path / "dashboard.html").read_text(
        encoding="utf-8"
    )

    assert queue_item["case_label"] == "slack_behavioral_health_rfp_001"
    assert queue_item["display_case_id"] != queue_item["case_id"]
    assert queue_item["display_prompt_number"]
    assert queue_item["dashboard_url"].endswith("?case=slack_behavioral_health_rfp_001")
    assert queue_item["review_url"].endswith("?case=slack_behavioral_health_rfp_001")
    assert queue_item["case_bundle_url"].endswith(
        "/api/eval-case-bundle?case=slack_behavioral_health_rfp_001"
    )
    assert "const caseLabel = String(row.case_label || row.case_id || 'case tbd')" in html
    assert "<strong>${escapeHtml(caseLabel)}</strong>" in html
    assert "row.display_case_id || row.case_id || 'case tbd'" not in html


def test_case_specific_review_paths_ignore_dashboard_row_limit(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="zz_overlimit_manual_case_001",
        run_id="wi_overlimit",
        agent="zz_test_agent",
        request_text="@KNI test agent summarize the selected thread",
        result_summary="Over-limit case response is saved.",
        thread_fetch_status="ok",
        thread_message_count=1,
        database_path=database_path,
    )

    limited_payload = dashboard_payload(database_path=database_path, limit=1)
    assert all(
        item["case_id"] != "zz_overlimit_manual_case_001"
        for item in limited_payload["cases"]
    )

    bundle = eval_case_bundle_response(
        case_id="zz_overlimit_manual_case_001",
        database_path=database_path,
        limit=1,
    )["bundle"]
    html = render_review_form(
        case_id="zz_overlimit_manual_case_001",
        database_path=database_path,
        limit=1,
    )
    review = save_human_review_payload(
        {
            "case_id": "zz_overlimit_manual_case_001",
            "run_id": "wi_overlimit",
            "agent": "zz_test_agent",
            "scores": _review_scores(usefulness=5),
            "safety": "pass",
            "notes": "Over-limit score saved.",
        },
        database_path=database_path,
        require_recorded_response=True,
    )

    assert bundle["case_id"] == "zz_overlimit_manual_case_001"
    assert bundle["slack"]["run_id"] == "wi_overlimit"
    assert "Over-limit case response is saved." in html
    assert '<button type="submit">Update review</button>' in html
    assert review["case_id"] == "zz_overlimit_manual_case_001"
    assert review["run_id"] == "wi_overlimit"
    assert review["trace_event_type"] == "human_review_saved"


def test_eval_dashboard_flags_retry_heavy_slack_cases(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    for index in range(4):
        record_slack_eval_run(
            case_id="slack_retry_noise_001",
            run_id=f"wi_retry_{index}",
            agent="business_research_analyst",
            request_text="@KNI business research analyst summarize selected thread",
            result_summary=f"Retry result {index}.",
            thread_fetch_status="ok",
            thread_message_count=1,
            database_path=database_path,
        )

    payload = dashboard_payload(database_path=database_path)
    case = next(item for item in payload["cases"] if item["case_id"] == "slack_retry_noise_001")
    checks = {item["label"]: item for item in case["case_review_checklist"]}
    quality_checks = {item["key"]: item for item in payload["data_quality"]["checks"]}

    assert payload["summary"]["slack_run_rows"] == 4
    assert payload["summary"]["slack_linked"] == 1
    assert payload["summary"]["slack_retry_cases"] == 1
    assert payload["workflow_readiness"]["counts"]["slack_thread_runs"] == 4
    assert payload["workflow_readiness"]["counts"]["slack_thread_cases"] == 1
    assert checks["slack_retry_volume"]["status"] == "attention"
    assert "review the latest run_id" in checks["slack_retry_volume"]["detail"]
    assert quality_checks["slack_retry_volume"]["status"] == "pending"

    bundle = eval_case_bundle_response(
        case_id="slack_retry_noise_001",
        database_path=database_path,
    )["bundle"]
    bundle_checks = {item["label"]: item for item in bundle["review_checklist"]}
    assert bundle["slack"]["run_count"] == 4
    assert bundle_checks["slack_retry_volume"]["status"] == "attention"


def test_data_quality_response_flags_local_readiness_gaps(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_quality_gap_001",
        run_id="wi_quality",
        agent="business_research_analyst",
        request_text="@KNI business research analyst \"summarize a source-backed company profile\"",
        result_summary="Saved response.",
        thread_fetch_status="",
        thread_message_count=0,
        database_path=database_path,
    )

    payload = data_quality_response(database_path=database_path)
    quality = payload["data_quality"]

    assert payload["status"] == "ok"
    assert quality["mode"] == "local_preflight"
    assert quality["live_api_calls"] is False
    assert quality["total_checks"] >= 8
    checks = {item["key"]: item for item in quality["checks"]}
    assert checks["prompt_text"]["status"] == "complete"
    assert checks["slack_thread_evidence"]["status"] == "pending"
    assert checks["trace_processor"]["status"] == "pending"
    assert checks["trace_sensitive_capture"]["status"] == "complete"
    assert quality["human_review_queue_count"] >= len(quality["human_review_queue"]) >= 1
    assert quality["review_blocked_count"] >= len(quality["review_blocked_cases"]) >= 1
    review_item = quality["human_review_queue"][0]
    assert review_item["case_id"] == "slack_quality_gap_001"
    assert review_item["display_case_id"]
    assert review_item["reason"] == "response saved but human review missing"
    assert review_item["primary_label"] == "human_review"
    assert review_item["primary_label_display"] == "Human review"
    assert "human_review" in review_item["labels"]
    assert "Human review" in review_item["label_display"]
    assert "Missing:" in review_item["follow_up_summary"]
    assert "Machine check" in review_item["follow_up_summary"]
    assert "Human review" in review_item["follow_up_summary"]
    assert review_item["dashboard_url"].endswith("?case=slack_quality_gap_001")
    assert review_item["review_url"].endswith("?case=slack_quality_gap_001")
    assert review_item["case_bundle_url"].endswith("/api/eval-case-bundle?case=slack_quality_gap_001")
    blocked_item = quality["review_blocked_cases"][0]
    assert blocked_item["case_id"]
    assert blocked_item["reason"] == "missing recorded response"
    assert blocked_item["primary_label"] == "recorded_response"
    assert blocked_item["primary_label_display"] == "Recorded response"
    assert "recorded_response" in blocked_item["labels"]
    assert "Recorded response" in blocked_item["label_display"]
    assert "Missing: Recorded response" in blocked_item["follow_up_summary"]
    assert blocked_item["dashboard_url"].endswith(f"?case={blocked_item['case_id']}")
    assert blocked_item["review_url"].endswith(f"?case={blocked_item['case_id']}")
    assert blocked_item["case_bundle_url"].endswith(
        f"/api/eval-case-bundle?case={blocked_item['case_id']}"
    )


def test_dashboard_follow_up_queue_prioritizes_current_eval_gaps(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_quality_gap_older_001",
        run_id="wi_quality_older",
        agent="business_research_analyst",
        request_text="@KNI business research analyst \"summarize a source-backed company profile\"",
        result_summary="Older saved response.",
        thread_fetch_status="",
        thread_message_count=0,
        database_path=database_path,
    )
    record_slack_eval_run(
        case_id="slack_quality_gap_newer_001",
        run_id="wi_quality_newer",
        agent="business_research_analyst",
        request_text="@KNI business research analyst \"summarize a newer source-backed company profile\"",
        result_summary="Newer saved response.",
        thread_fetch_status="",
        thread_message_count=0,
        database_path=database_path,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE slack_eval_runs SET created_at = ? WHERE run_id = ?",
            ("2026-06-11T20:00:00Z", "wi_quality_older"),
        )
        connection.execute(
            "UPDATE slack_eval_runs SET created_at = ? WHERE run_id = ?",
            ("2026-06-14T20:00:00Z", "wi_quality_newer"),
        )
        connection.commit()

    payload = dashboard_payload(database_path=database_path)
    queue = payload["follow_up_queue"]
    dated_case_ids = [
        item["case_id"]
        for item in queue
        if item["case_id"] in {"slack_quality_gap_older_001", "slack_quality_gap_newer_001"}
    ]
    dated_indexes = [
        index
        for index, item in enumerate(queue)
        if item["case_id"] in {"slack_quality_gap_older_001", "slack_quality_gap_newer_001"}
    ]
    first = next(item for item in queue if item["case_id"] == "slack_quality_gap_older_001")
    newer = next(item for item in queue if item["case_id"] == "slack_quality_gap_newer_001")
    html = render_dashboard(database_path=database_path, output_path=tmp_path / "dashboard.html").read_text(
        encoding="utf-8"
    )

    assert dated_case_ids == ["slack_quality_gap_older_001", "slack_quality_gap_newer_001"]
    assert dated_indexes == [0, 1]
    assert all(item.get("has_latest_run") for item in queue)
    assert first["primary_label"] == "slack_thread_evidence"
    assert first["primary_label_display"] == "Slack thread evidence"
    assert first["status"] == "missing"
    assert "human_review" in first["labels"]
    assert "Slack thread evidence" in first["missing_label_display"]
    assert "Human review" in first["missing_label_display"]
    assert first["follow_up_summary"] == "Missing: Slack thread evidence, Machine check, Human review"
    assert newer["latest_run_at"] > first["latest_run_at"]
    assert first["review_url"].endswith("?case=slack_quality_gap_older_001")
    assert first["case_bundle_url"].endswith("/api/eval-case-bundle?case=slack_quality_gap_older_001")
    assert "Eval Follow-up Queue" in html
    assert "follow_up_queue" in html
    assert "data-copy-followup-case" in html
    assert "followUpSummary" in html
    assert "Copy eval review packet" in html
    assert "human_review_queue_count" in html
    assert "review_blocked_count" in html
    assert "Review blocked" in html


def test_dashboard_follow_up_queue_omits_seed_only_cases_after_clean_slate() -> None:
    seed_only_case = {
        "case_id": "slack_seed_only_001",
        "display_case_id": "slack_seed_only_001",
        "agent": "business_research_analyst",
        "user_input": "@KNI business research analyst summarize sources",
        "case_review_checklist": [
            {"label": "recorded_response", "status": "missing", "detail": "No response yet."},
            {"label": "machine_check", "status": "missing", "detail": "No machine row yet."},
        ],
    }
    runtime_case = {
        **seed_only_case,
        "case_id": "slack_runtime_gap_001",
        "latest_slack_run_id": "wi_runtime_gap",
        "latest_run_at": "2026-06-20T20:00:00Z",
        "slack_run_count": 1,
    }

    queue = eval_dashboard_module._follow_up_queue([seed_only_case, runtime_case])

    assert [item["case_id"] for item in queue] == ["slack_runtime_gap_001"]


def test_dashboard_follow_up_queue_names_slack_gap_before_human_review_for_machine_only_case(
    tmp_path,
) -> None:
    results_path = tmp_path / "machine-only-eval.json"
    results_path.write_text(
        json.dumps(
            {
                "evalId": "eval-machine-only",
                "results": {
                    "timestamp": "2026-06-14T19:00:00Z",
                    "stats": {"successes": 1, "failures": 0, "errors": 0},
                    "results": [
                        {
                            "id": "result-machine-only",
                            "testIdx": 0,
                            "success": True,
                            "score": 1,
                            "vars": {
                                "case_id": "slack_agents_sdk_course_001",
                                "agent_under_test": "opportunity_scout",
                                "eval_dimensions": "slack_interface,human_review",
                                "user_input": (
                                    "@KNI opportunity scout -- find three Agents SDK "
                                    "courses that are reasonably cost and good for "
                                    "someone with some experience"
                                ),
                            },
                            "gradingResult": {"reason": "All assertions passed"},
                            "response": {"output": "Saved machine response only."},
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "evals.sqlite"
    import_promptfoo_results(results_path, database_path=database_path)

    queue = dashboard_payload(database_path=database_path)["follow_up_queue"]
    item = next(
        row
        for row in queue
        if row["case_id"] == "slack_agents_sdk_course_001"
    )

    assert item["missing_labels"] == ["slack_run", "human_review"]
    assert item["missing_label_display"] == ["Slack run", "Human review"]
    assert (
        item["follow_up_summary"]
        == "Missing: Slack run, Human review | Needs attention: Source visibility"
    )
    assert item["primary_label"] == "slack_run"
    assert item["primary_label_display"] == "Slack run"
    assert item["detail"] == (
        "No Slack run is linked; run the case in #evals to capture Slack behavior/evidence."
    )
    assert item["next_follow_up"] == item["detail"]


def test_follow_up_queue_response_exposes_sorted_missing_work(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_slack_eval_run(
        case_id="slack_queue_endpoint_older_001",
        run_id="wi_queue_older",
        agent="business_research_analyst",
        request_text="@KNI business research analyst \"summarize an older company profile\"",
        result_summary="Older saved response.",
        thread_fetch_status="",
        thread_message_count=0,
        database_path=database_path,
    )
    record_slack_eval_run(
        case_id="slack_queue_endpoint_newer_001",
        run_id="wi_queue_newer",
        agent="business_research_analyst",
        request_text="@KNI business research analyst \"summarize a newer company profile\"",
        result_summary="Newer saved response.",
        thread_fetch_status="",
        thread_message_count=0,
        database_path=database_path,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE slack_eval_runs SET created_at = ? WHERE run_id = ?",
            ("2026-06-11T20:00:00Z", "wi_queue_older"),
        )
        connection.execute(
            "UPDATE slack_eval_runs SET created_at = ? WHERE run_id = ?",
            ("2026-06-14T20:00:00Z", "wi_queue_newer"),
        )
        connection.commit()

    payload = follow_up_queue_response(database_path=database_path)
    rows = payload["rows"]
    dated_rows = [
        item
        for item in rows
        if item["case_id"] in {"slack_queue_endpoint_older_001", "slack_queue_endpoint_newer_001"}
    ]

    assert payload["status"] == "ok"
    assert payload["mode"] == "local_follow_up_queue"
    assert payload["live_api_calls"] is False
    assert payload["total"] == len(rows)
    assert [item["case_id"] for item in dated_rows] == [
        "slack_queue_endpoint_older_001",
        "slack_queue_endpoint_newer_001",
    ]
    first = dated_rows[0]
    assert first["primary_label"] == "slack_thread_evidence"
    assert first["primary_label_display"] == "Slack thread evidence"
    assert first["follow_up_summary"] == "Missing: Slack thread evidence, Machine check, Human review"
    assert first["missing_label_display"] == [
        "Slack thread evidence",
        "Machine check",
        "Human review",
    ]
    assert first["dashboard_url"].endswith("?case=slack_queue_endpoint_older_001")
    assert first["review_url"].endswith("?case=slack_queue_endpoint_older_001")
    assert first["case_bundle_url"].endswith(
        "/api/eval-case-bundle?case=slack_queue_endpoint_older_001"
    )


def test_eval_dashboard_server_routes_follow_up_queue_endpoint() -> None:
    source = Path(eval_dashboard_server.__file__).read_text(encoding="utf-8")

    assert 'path == "/api/follow-up-queue"' in source
    assert "self._serve_follow_up_queue()" in source
    assert "self._serve_follow_up_queue(head_only=True)" in source


def test_promptfoo_provider_compacts_ask_agent_payload(monkeypatch) -> None:
    stdout_payload = {
        "status": "done",
        "route": "opportunity_scout",
        "human_summary": "Source-backed result fixture://source",
        "nested_specialist_results": [
            {"specialist_route": "business_research_analyst"},
            {"route": "opportunity_scout"},
        ],
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
            "audit_notes": [
                "Requested context sources tracked for specialist run: airtable, gmail"
            ],
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
                    "slack_context": {
                        "channel_id": "CDOCS123",
                        "channel_name": "docs",
                        "thread_ts": "1800000000.000100",
                        "selected_message_ts": "1800000000.000100",
                    },
                    "manager_loop_efficiency": {
                        "final_synthesis_executed": True,
                        "live_sdk": False,
                        "live_search": False,
                    },
                }
            },
        },
        "side_effects": {
            "email_sent": False,
            "gmail_draft_created": False,
            "gmail_label_changed": False,
            "slack_message_posted": False,
            "crm_write_performed": False,
            "calendar_write_performed": False,
            "external_file_write_performed": False,
            "blocked_write_attempts": [],
            "evidence_complete": True,
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
                    "channel_id": "CDOCS123",
                    "channel_name": "docs",
                    "thread_ts": "1800000000.000100",
                    "selected_message_ts": "1800000000.000100",
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
    assert output["context_sources"] == ["airtable", "gmail"]
    assert output["nested_specialist_routes"] == [
        "business_research_analyst",
        "opportunity_scout",
    ]
    assert output["source_types"] == ["fixture", "government"]
    assert output["next_action_agent"] == "business_research_analyst"
    assert output["context_pack_type"] == "opportunity"
    assert output["final_synthesis_executed"] is True
    assert output["slack_context_attached"] is True
    assert output["slack_channel_id"] == "CDOCS123"
    assert output["slack_channel_name"] == "docs"
    assert output["slack_thread_ts"] == "1800000000.000100"
    assert output["send_enabled"] is False
    assert output["side_effects"]["external_write_performed"] is False
    assert output["side_effect_evidence_complete"] is True


@pytest.mark.parametrize(
    ("channel_id", "channel_name"),
    [
        ("CAIAWFLOW", "ai-agents-workflow"),
        ("CANNOUNCE", "announcements"),
        ("CCALENDAR", "calendar"),
        ("CCOLLAB", "collaborations"),
        ("CDOCS123", "docs"),
        ("CEVALS123", "evals"),
        ("CGENERAL", "general"),
        ("CGIT123", "git"),
        ("CGMAIL123", "gmail"),
        ("CGRANTS", "grants-and-funding"),
        ("CKNOWHUB", "knowledge-hub"),
        ("CMEETINGS", "meetings"),
        ("CRUNTIME", "runtime-updates"),
    ],
)
def test_promptfoo_compact_payload_preserves_source_slack_channel(
    channel_id: str,
    channel_name: str,
) -> None:
    output = keystone_agent_provider._compact_run_payload(
        {
            "status": "done",
            "route": "chief_of_staff",
            "human_summary": "*Answer:*\nRead-only channel diagnostic.",
            "work_item": {
                "status": "done",
                "current_route": "chief_of_staff",
                "target": {
                    "metadata": {
                        "slack_context": {
                            "channel_id": channel_id,
                            "channel_name": channel_name,
                            "thread_ts": "1800000000.000100",
                            "selected_message_ts": "1800000000.000100",
                        }
                    }
                },
            },
            "side_effects": _side_effects(),
        }
    )

    assert output["slack_context_attached"] is True
    assert output["slack_channel_id"] == channel_id
    assert output["slack_channel_name"] == channel_name
    assert output["slack_thread_ts"] == "1800000000.000100"


def test_promptfoo_provider_handles_missing_route_result(monkeypatch) -> None:
    stdout_payload = {
        "status": "blocked",
        "route": "chief_of_staff",
        "human_summary": "Read-only plan blocked until owner is confirmed.",
        "orchestrator_preflight": None,
        "work_item": {
            "status": "blocked",
            "current_route": "chief_of_staff",
            "audit_notes": [
                "Requested context sources tracked for specialist run: airtable, slack"
            ],
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
        "@KNI chief of staff \"make a decision log\"",
        {"config": {"python": ".venv/bin/python", "agent": "chief_of_staff"}},
        {
            "vars": {
                "surface": "slack",
                "user_input": "@KNI chief of staff \"make a decision log\"",
            }
        },
    )

    output = json.loads(result["output"])
    assert output["provider_status"] == "ok"
    assert output["route"] == "chief_of_staff"
    assert output["status"] == "blocked"
    assert output["context_sources"] == ["airtable", "slack"]
    assert output["side_effects"]["approval_ref"] == ""


def test_promptfoo_provider_isolates_child_state_from_operator_database(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("DATABASE_URL", "sqlite:///keystone_agents.db")
    monkeypatch.setenv("KEYSTONE_HOME", "/operator/keystone-home")
    monkeypatch.setenv(
        "KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB",
        "/operator/human-reviews.sqlite",
    )
    monkeypatch.setenv("KEYSTONE_TRACE_SUMMARY_DB", "/operator/trace-summaries.sqlite")

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        env = dict(kwargs["env"])
        captured["env"] = env
        database_url = str(env["DATABASE_URL"])
        database_path = Path(database_url.removeprefix("sqlite:///"))
        captured["state_directory"] = database_path.parent
        assert database_path.parent.is_dir()
        assert database_path != Path("keystone_agents.db")
        database_path.write_text("isolated test state", encoding="utf-8")
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "done",
                    "route": "chief_of_staff",
                    "human_summary": "Offline result.",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(keystone_agent_provider.subprocess, "run", fake_run)
    result = keystone_agent_provider.call_api(
        '@KNI chief of staff "summarize this note"',
        {"config": {"python": ".venv/bin/python", "agent": "chief_of_staff"}},
        {
            "vars": {
                "surface": "slack",
                "user_input": '@KNI chief of staff "summarize this note"',
            }
        },
    )

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PYTHON_DOTENV_DISABLED"] == "1"
    assert env["KEYSTONE_TEST_MODE"] == "1"
    assert env["DATABASE_URL"] != "sqlite:///keystone_agents.db"
    assert env["KEYSTONE_HOME"] != "/operator/keystone-home"
    assert env["KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB"] != "/operator/human-reviews.sqlite"
    assert env["KEYSTONE_TRACE_SUMMARY_DB"] != "/operator/trace-summaries.sqlite"
    state_directory = captured["state_directory"]
    assert isinstance(state_directory, Path)
    assert not state_directory.exists()

    output = json.loads(result["output"])
    assert output["state_isolation"] == {
        "schema": "keystone.promptfoo.state_isolation.v1",
        "isolated": True,
        "storage_scope": "per_case_temporary",
        "operator_database_used": False,
        "dotenv_loading_disabled": True,
        "test_mode": True,
    }


def test_promptfoo_provider_normalizes_missing_safe_side_effect_evidence(monkeypatch) -> None:
    stdout_payload = {
        "status": "done",
        "route": "opportunity_scout",
        "human_summary": "Read-only result from fixture://source",
        "orchestrator_preflight": {
            "route_result": {
                "route": "opportunity_scout",
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
                {"url": "fixture://source", "title": "Fixture source", "source_type": "fixture"}
            ],
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
        '@KNI opportunity scout "find safe opportunities"',
        {"config": {"python": ".venv/bin/python", "agent": "opportunity_scout"}},
        {
            "vars": {
                "surface": "slack",
                "user_input": '@KNI opportunity scout "find safe opportunities"',
            }
        },
    )

    output = json.loads(result["output"])
    assert output["side_effects"]["instrumentation_present"] is True
    assert output["side_effects"]["evidence_state"] == "explicit_none"
    assert output["side_effect_evidence_complete"] is True
    assert output["external_write_performed"] is False


def test_promptfoo_provider_infers_pack_type_for_blocked_selected_specialist(
    monkeypatch,
) -> None:
    stdout_payload = {
        "status": "blocked",
        "mode": "blocked",
        "selected_agent": "outreach_composer",
        "message": "Provide an approved CompanyProfile or OpportunityRecord before requesting an outreach draft.",
        "block_reason": "Approved CompanyProfile or approved OpportunityRecord is required before outreach.",
        "orchestrator_preflight": {
            "route_result": {
                "route": "clarification",
                "approval_required": True,
                "approval_rationale": (
                    "Outreach drafting is blocked until a human-approved drafting context is present."
                ),
                "send_enabled": False,
                "forbidden_actions": ["send_email"],
            }
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
        '@KNI outreach composer "draft a LinkedIn note"',
        {"config": {"python": ".venv/bin/python", "agent": "orchestrator"}},
        {
            "vars": {
                "surface": "slack",
                "user_input": '@KNI outreach composer "draft a LinkedIn note"',
            }
        },
    )

    output = json.loads(result["output"])
    assert output["route"] == "outreach_composer"
    assert output["context_pack_type"] == "outreach"
    assert "approval" in output["human_summary"].lower()
    assert "context" in output["human_summary"].lower()


def test_promptfoo_provider_compacts_direct_context_agent_payload(monkeypatch) -> None:
    stdout_payload = {
        "status": "done",
        "selected_agent": "airtable_context_agent",
        "output_type": "AirtableContextResult",
        "output": {
            "summary": (
                "Airtable schema mapping and record identity questions are ready; "
                "approval needs and no-write blockers are explicit."
            ),
        },
        "side_effects": {
            "evidence_complete": True,
            "blocked_write_attempts": ["airtable_context_agent: no write attempted"],
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
        "@KNI airtable context agent inspect tracker schema",
        {"config": {"python": ".venv/bin/python", "agent": "orchestrator"}},
        {
            "vars": {
                "surface": "slack",
                "user_input": "@KNI airtable context agent inspect tracker schema",
            }
        },
    )

    output = json.loads(result["output"])
    assert output["provider_status"] == "ok"
    assert output["route"] == "airtable_context_agent"
    assert output["status"] == "done"
    assert output["output_type"] == "AirtableContextResult"
    assert "schema mapping" in output["human_summary"]
    assert "record identity" in output["human_summary"]
    assert output["side_effects"]["external_write_performed"] is False
    assert output["side_effect_evidence_complete"] is True


def test_promptfoo_context_file_keeps_case_id_non_routing() -> None:
    context_file = keystone_agent_provider._write_slack_context(
        {
            "case_id": "slack_cos_eval_gap_summary_001",
            "user_input": "@KNI chief of staff summarize eval gaps",
            "slack_context": {
                "channel_id": "C0BA17Y9C01",
                "channel_name": "evals",
                "selected_message_ts": "1800001290.000100",
            },
        }
    )

    try:
        assert context_file is not None
        payload = json.loads(context_file.read_text(encoding="utf-8"))
        assert payload["metadata"]["source"] == "promptfoo"
        assert payload["metadata"]["promptfoo_case_id"] == "slack_cos_eval_gap_summary_001"
        assert "case_id" not in payload["metadata"]
    finally:
        keystone_agent_provider._cleanup_promptfoo_context_file(context_file)


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


def test_promptfoo_provider_blocks_live_sdk_without_budget_controls() -> None:
    result = keystone_agent_provider.call_api(
        "@KNI opportunity scout",
        {"config": {"python": ".venv/bin/python", "agent": "orchestrator", "live_sdk": True}},
        {"vars": {"case_id": "slack_live_guard_001", "user_input": "@KNI opportunity scout"}},
    )

    output = json.loads(result["output"])
    assert output["provider_status"] == "error"
    assert output["failure_kind"] == "live_eval_guard"
    assert "budget_usd" in output["error"]
    assert "approval_ref" in output["error"]


def test_promptfoo_provider_blocks_live_allowlist_larger_than_max_cases() -> None:
    result = keystone_agent_provider.call_api(
        "@KNI opportunity scout",
        {
            "config": {
                "python": ".venv/bin/python",
                "agent": "orchestrator",
                "live_sdk": True,
                "live_run_approved": True,
                "approval_ref": "APPROVED-123",
                "budget_usd": 1.25,
                "max_cases": 1,
                "case_allowlist": ["slack_live_guard_001", "slack_live_guard_002"],
                "run_label": "paid-smoke",
                "model_provider": "openai",
                "model_name": "gpt-5.4-mini",
            }
        },
        {"vars": {"case_id": "slack_live_guard_001", "user_input": "@KNI opportunity scout"}},
    )

    output = json.loads(result["output"])
    assert output["provider_status"] == "error"
    assert output["failure_kind"] == "live_eval_guard"
    assert "exceeding max_cases=1" in output["error"]


def test_promptfoo_provider_blocks_selected_case_count_larger_than_max_cases() -> None:
    result = keystone_agent_provider.call_api(
        "@KNI opportunity scout",
        {
            "config": {
                "python": ".venv/bin/python",
                "agent": "orchestrator",
                "live_sdk": True,
                "live_run_approved": True,
                "approval_ref": "APPROVED-123",
                "budget_usd": 1.25,
                "max_cases": 1,
                "selected_case_count": 2,
                "case_allowlist": ["slack_live_guard_001"],
                "run_label": "paid-smoke",
                "model_provider": "openai",
                "model_name": "gpt-5.4-mini",
            }
        },
        {"vars": {"case_id": "slack_live_guard_001", "user_input": "@KNI opportunity scout"}},
    )

    output = json.loads(result["output"])
    assert output["provider_status"] == "error"
    assert output["failure_kind"] == "live_eval_guard"
    assert "selected_case_count=2 exceeds max_cases=1" in output["error"]


def test_promptfoo_provider_live_search_is_explicit_and_records_provenance(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("KEYSTONE_OPENAI_API_KEY", "test-openai-key-promptfoo-live")
    monkeypatch.setenv("OPENAI_API_KEY", "generic-wrong-project-key")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "SLACK_TOKEN_SHOULD_NOT_PASS")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "gmail-should-not-pass")
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://127.0.0.1:18080")
    stdout_payload = {
        "status": "done",
        "route": "opportunity_scout",
        "human_summary": "Found source fixture://source",
        "orchestrator_preflight": {
            "route_result": {
                "route": "opportunity_scout",
                "forbidden_actions": ["send_email"],
            }
        },
        "work_item": {
            "status": "done",
            "current_route": "opportunity_scout",
            "sources": [{"url": "fixture://source", "title": "Source"}],
            "target": {
                "metadata": {
                    "slack_context": {"channel_id": "C0BA17Y9C01"},
                    "manager_loop_efficiency": {
                        "live_sdk": True,
                        "live_search": True,
                        "final_synthesis_executed": True,
                    },
                }
            },
        },
    }

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        captured["command"] = list(command)
        captured["env"] = dict(kwargs["env"])
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps(stdout_payload),
            stderr="",
        )

    monkeypatch.setattr(keystone_agent_provider.subprocess, "run", fake_run)
    result = keystone_agent_provider.call_api(
        "@KNI opportunity scout",
        {
            "config": {
                "python": ".venv/bin/python",
                "agent": "orchestrator",
                "live_sdk": True,
                "live_search": True,
                "live_run_approved": True,
                "approval_ref": "APPROVED-123",
                "budget_usd": 1.25,
                "max_cases": 1,
                "case_allowlist": ["slack_live_guard_001"],
                "run_label": "paid-smoke",
                "model_provider": "openai",
                "model_name": "gpt-5.4-mini",
                "search_provider": "searxng",
                "max_search_calls": 2,
                "max_search_results": 5,
            }
        },
        {
            "vars": {
                "case_id": "slack_live_guard_001",
                "user_input": "@KNI opportunity scout",
            }
        },
    )

    output = json.loads(result["output"])
    command = captured["command"]
    env = captured["env"]
    assert output["provider_status"] == "ok"
    assert "--live-sdk" in command
    assert "--live-search" in command
    assert env["KEYSTONE_ENABLE_LIVE_RESEARCH"] == "true"
    assert env["SEARCH_PROVIDER"] == "searxng"
    assert env["KEYSTONE_OPENAI_API_KEY"] == "test-openai-key-promptfoo-live"
    assert env["SEARXNG_BASE_URL"] == "http://127.0.0.1:18080"
    assert "OPENAI_API_KEY" not in env
    assert "SLACK_BOT_TOKEN" not in env
    assert "GMAIL_REFRESH_TOKEN" not in env
    assert output["provenance"]["run_mode"] == "live_sdk"
    assert output["provenance"]["search_provider"] == "searxng"
    assert output["provenance"]["allowed_credential_categories"] == ["openai", "searxng"]
    assert output["side_effects"]["external_write_performed"] is False


def test_promptfoo_provider_blocks_ambient_live_search_without_case_opt_in(
    monkeypatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.delenv("KEYSTONE_DRY_RUN", raising=False)

    result = keystone_agent_provider.call_api(
        "@KNI opportunity scout",
        {
            "config": {
                "python": ".venv/bin/python",
                "agent": "orchestrator",
                "live_sdk": True,
                "live_run_approved": True,
                "approval_ref": "APPROVED-123",
                "budget_usd": 1.25,
                "max_cases": 1,
                "case_allowlist": ["slack_live_guard_001"],
                "run_label": "paid-smoke",
                "model_provider": "openai",
                "model_name": "gpt-5.4-mini",
            }
        },
        {
            "vars": {
                "case_id": "slack_live_guard_001",
                "user_input": "@KNI opportunity scout",
            }
        },
    )

    output = json.loads(result["output"])
    assert output["provider_status"] == "error"
    assert "ambient live search" in output["error"]


def test_promptfoo_provider_cleans_temp_context_and_redacts_failure_payload(
    monkeypatch,
) -> None:
    observed_context: dict[str, Path] = {}

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        context_path = Path(command[command.index("--context-file") + 1])
        observed_context["path"] = context_path
        assert context_path.exists()
        payload = json.loads(context_path.read_text(encoding="utf-8"))
        assert payload["metadata"]["sensitivity"] == "slack_context_local_only"
        return subprocess.CompletedProcess(
            args=command,
            returncode=3,
            stdout=(
                "Original request: find the private company\n"
                "KEYSTONE_OPENAI_API_KEY=TEST_SECRET_VALUE\n"
                '"selected_message": "do not persist this raw slack line"'
            ),
            stderr="slack token TEST_SLACK_SECRET_VALUE",
        )

    monkeypatch.setattr(keystone_agent_provider.subprocess, "run", fake_run)
    result = keystone_agent_provider.call_api(
        "@KNI opportunity scout",
        {"config": {"python": ".venv/bin/python", "agent": "orchestrator"}},
        {
            "vars": {
                "case_id": "slack_context_cleanup_001",
                "user_input": "@KNI opportunity scout",
                "slack_context": {
                    "selected_message": {
                        "ts": "1800000000.000100",
                        "user_id": "U_EVAL",
                        "username": "anup",
                        "text": "find the private company",
                    }
                },
            }
        },
    )

    output = json.loads(result["output"])
    assert output["provider_status"] == "error"
    assert output["failure_payload_redacted"] is True
    assert output["structured_log"]["schema"] == "keystone.structured_log.v1"
    assert output["structured_log"]["component"] == "promptfoo_provider"
    assert output["structured_log"]["event"] == "provider_error"
    assert output["structured_log"]["level"] == "error"
    assert output["structured_log"]["correlation"]["case_id"] == "slack_context_cleanup_001"
    assert output["structured_log"]["correlation"]["agent"] == "orchestrator"
    assert output["structured_log"]["correlation"]["failure_kind"] == "non_zero_exit"
    assert output["structured_log"]["redaction"]["raw_payload_included"] is False
    assert "stdout" not in output
    assert "stderr" not in output
    rendered = json.dumps(output, sort_keys=True)
    assert "sk-SECRET" not in rendered
    assert "TEST_SLACK_SECRET_VALUE" not in rendered
    assert "do not persist this raw slack line" not in rendered
    assert observed_context["path"].exists() is False
