#!/usr/bin/env python3
"""Dry-run preflight for tomorrow's Slack eval loop."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import scripts.handle_slack_agent_action as slack_agent_action_cli
from keystone_agents.cli import main as cli_main
from keystone_agents.slack_action_contract import (
    RUN_AGENT_MESSAGE_CALLBACK_ID,
    RUN_AGENT_TASK_ACTION_ID,
    RUN_AGENT_TASK_BLOCK_ID,
    RUN_AGENT_VIEW_CALLBACK_ID,
)
from keystone_agents.slack_actions import handle_run_agent_interaction
from promptfoo.eval_dashboard import (
    CORE_EVAL_AGENTS,
    EVAL_CASE_TARGET_PER_AGENT,
    render_dashboard,
)
from promptfoo.eval_database import eval_case_status
from promptfoo.eval_dashboard_server import build_parser as build_dashboard_server_parser

EXPECTED_EVAL_CHANNEL_ID = "C0BA17Y9C01"
EXPECTED_EVAL_CHANNEL_NAME = "evals"
DEFAULT_SLACK_REPO = Path(__file__).resolve().parents[2] / "keystone-slack"
REQUIRED_SLACK_EVAL_SCOPES = {
    "app_mentions:read",
    "chat:write",
    "channels:history",
    "groups:history",
}


def main() -> int:
    args = _build_parser().parse_args()
    checks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="kba-eval-slack-readiness-") as raw_tmp:
        tmp_dir = Path(raw_tmp)
        eval_db = tmp_dir / "human-reviews.sqlite"
        old_db = os.environ.get("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB")
        os.environ["KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB"] = str(eval_db)
        try:
            checks.extend(
                _run_checks(
                    tmp_dir=tmp_dir,
                    eval_db=eval_db,
                    live_slack_probe=args.live_slack_probe,
                )
            )
        finally:
            if old_db is None:
                os.environ.pop("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", None)
            else:
                os.environ["KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB"] = old_db
    failures = [check for check in checks if check["status"] == "fail"]
    warnings = [check for check in checks if check["status"] == "warn"]
    tomorrow = _tomorrow_operator_steps()
    strict_warning_failure = bool(args.strict and warnings)
    failed = bool(failures or strict_warning_failure)
    if args.json:
        print(
            json.dumps(
                {
                    "status": "fail" if failed else "pass",
                    "checks": checks,
                    "warning_count": len(warnings),
                    "strict": bool(args.strict),
                    "tomorrow": tomorrow,
                },
                indent=2,
            )
        )
    else:
        for check in checks:
            print(f"{check['status'].upper()}: {check['name']} - {check['detail']}")
        if warnings:
            print(
                "READY WITH WARNINGS: verify warning items before the live Slack test."
            )
        if strict_warning_failure:
            print("STRICT READINESS FAILED: warnings must be resolved before live testing.")
        print(f"READY: eval channel - {tomorrow['eval_channel']}")
        print(f"READY: dashboard server - {tomorrow['dashboard_server']}")
        print(f"READY: Slack thread flow - {tomorrow['slack_thread_flow']}")
    return 1 if failed else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check local readiness for Promptfoo + Slack eval testing."
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable output.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit nonzero when warnings remain.",
    )
    parser.add_argument(
        "--live-slack-probe",
        action="store_true",
        help="Run read-only Slack Web API checks for token and #evals history access.",
    )
    return parser


def _run_checks(
    *,
    tmp_dir: Path,
    eval_db: Path,
    live_slack_probe: bool = False,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    checks.append(_coverage_check())
    checks.append(_eval_channel_config_check())
    checks.append(_keystone_slack_bridge_contract_check())
    checks.append(_keystone_slack_runtime_env_check())
    checks.append(_keystone_slack_scope_declaration_check())
    if live_slack_probe:
        checks.append(_keystone_slack_live_read_probe())
    checks.append(_keystone_slack_socket_status_check())
    bridge = _slack_bridge_check(tmp_dir=tmp_dir, eval_db=eval_db)
    private_metadata = str(bridge.pop("private_metadata", "") or "")
    context_file_path = str(bridge.pop("context_file_path", "") or "")
    dashboard_url = str(bridge.get("dashboard_url") or "")
    checks.append(bridge)
    checks.append(_dashboard_link_target_check(dashboard_url=dashboard_url))
    checks.append(
        _slack_bridge_cli_check(
            tmp_dir=tmp_dir,
            private_metadata=private_metadata,
        )
    )
    checks.append(_dashboard_check(tmp_dir=tmp_dir, eval_db=eval_db))
    checks.append(_dashboard_workflow_readiness_check(tmp_dir=tmp_dir, eval_db=eval_db))
    case_id = str(bridge.get("case_id") or "slack_bridge_readiness_001")
    checks.append(_scorecard_cli_check(case_id=case_id, context_file_path=context_file_path))
    checks.append(
        _score_save_cli_check(
            case_id=case_id,
            eval_db=eval_db,
            context_file_path=context_file_path,
        )
    )
    checks.append(_dashboard_human_review_check(tmp_dir=tmp_dir, eval_db=eval_db, case_id=case_id))
    checks.append(_status_cli_check(case_id=case_id))
    checks.append(_app_mention_thread_flow_check(tmp_dir=tmp_dir, eval_db=eval_db))
    return checks


def _eval_channel_config_check() -> dict[str, Any]:
    configured = str(os.environ.get("KNI_BUSINESS_AGENTS_EVAL_CHANNEL") or "").strip()
    if configured and configured != EXPECTED_EVAL_CHANNEL_ID:
        return _check(
            "eval channel config",
            "fail",
            (
                "KNI_BUSINESS_AGENTS_EVAL_CHANNEL is "
                f"{configured}; expected {EXPECTED_EVAL_CHANNEL_ID} for #{EXPECTED_EVAL_CHANNEL_NAME}"
            ),
        )
    if configured == EXPECTED_EVAL_CHANNEL_ID:
        return _check(
            "eval channel config",
            "pass",
            f"KNI_BUSINESS_AGENTS_EVAL_CHANNEL points to #{EXPECTED_EVAL_CHANNEL_NAME}",
        )
    return _check(
        "eval channel config",
        "pass",
        (
            "KNI_BUSINESS_AGENTS_EVAL_CHANNEL is not set in this shell; "
            f"keystone-slack defaults to {EXPECTED_EVAL_CHANNEL_ID}, export it if overriding"
        ),
    )


def _keystone_slack_bridge_contract_check() -> dict[str, Any]:
    repo = Path(os.environ.get("KEYSTONE_SLACK_REPO") or DEFAULT_SLACK_REPO)
    if not repo.is_dir():
        return _check(
            "keystone-slack bridge contract",
            "pass",
            f"sibling repo not found at {repo}; skipped optional cross-repo contract check",
        )
    files = {
        ".env.example": repo / ".env.example",
        "config": repo / "kni_integrations" / "config.py",
        "business_agents_bridge": repo / "kni_integrations" / "business_agents_bridge.py",
        "slack_socket_mode": repo / "kni_integrations" / "slack_socket_mode.py",
        "tests": repo / "tests" / "test_app_mentions.py",
    }
    missing_files = [name for name, path in files.items() if not path.is_file()]
    if missing_files:
        return _check(
            "keystone-slack bridge contract",
            "fail",
            f"missing expected sibling files: {', '.join(missing_files)}",
        )
    contents = {name: path.read_text(encoding="utf-8") for name, path in files.items()}
    required_snippets = {
        ".env.example": [f"KNI_BUSINESS_AGENTS_EVAL_CHANNEL={EXPECTED_EVAL_CHANNEL_ID}"],
        "config": [
            "KNI_BUSINESS_AGENTS_EVAL_CHANNEL",
            EXPECTED_EVAL_CHANNEL_ID,
        ],
        "business_agents_bridge": [
            "_hidden_eval_case_id_for_context",
            "slack_eval_channel_backend",
            '"eval"',
        ],
        "slack_socket_mode": [
            "_should_fetch_current_thread_history",
            "scorecard",
            "conversations.replies",
        ],
        "tests": [
            "test_eval_scorecard_app_mention_uses_thread_context_and_posts_card",
            "test_eval_scorecard_followup_fetches_current_thread_history",
            "how is this eval doing",
        ],
    }
    missing = [
        f"{name}:{snippet}"
        for name, snippets in required_snippets.items()
        for snippet in snippets
        if snippet not in contents[name]
    ]
    if missing:
        return _check(
            "keystone-slack bridge contract",
            "warn",
            f"missing expected bridge snippets: {missing}",
        )
    return _check(
        "keystone-slack bridge contract",
        "pass",
        "sibling Slack bridge has eval-channel, hidden metadata, and thread-history hooks",
    )


def _keystone_slack_runtime_env_check() -> dict[str, Any]:
    repo = Path(os.environ.get("KEYSTONE_SLACK_REPO") or DEFAULT_SLACK_REPO)
    env_path = repo / ".env"
    if not env_path.is_file():
        return _check(
            "keystone-slack runtime env",
            "pass",
            f"no local Slack .env at {env_path}; verify history context before live testing",
        )
    env = _read_env_file(env_path)
    eval_channel = env.get("KNI_BUSINESS_AGENTS_EVAL_CHANNEL", "")
    live_slack = _truthy_env(env.get("KNI_BUSINESS_AGENTS_LIVE_SLACK", ""))
    history_raw = env.get("KNI_BUSINESS_AGENTS_HISTORY_CONTEXT_ENABLED", "")
    history_enabled = _truthy_env(history_raw) or (not history_raw and live_slack)
    problems: list[str] = []
    if eval_channel and eval_channel != EXPECTED_EVAL_CHANNEL_ID:
        problems.append(
            f"KNI_BUSINESS_AGENTS_EVAL_CHANNEL={eval_channel}, expected {EXPECTED_EVAL_CHANNEL_ID}"
        )
    if not history_enabled:
        problems.append("KNI_BUSINESS_AGENTS_HISTORY_CONTEXT_ENABLED must be true for scorecard follow-ups")
    if problems:
        return _check("keystone-slack runtime env", "fail", "; ".join(problems))
    return _check(
        "keystone-slack runtime env",
        "pass",
        "local Slack env enables thread history and targets #evals",
    )


def _keystone_slack_scope_declaration_check() -> dict[str, Any]:
    repo = Path(os.environ.get("KEYSTONE_SLACK_REPO") or DEFAULT_SLACK_REPO)
    env_path = repo / ".env"
    env = _read_env_file(env_path) if env_path.is_file() else {}
    raw = (
        env.get("SLACK_CONFIGURED_BOT_SCOPES")
        or env.get("SLACK_BOT_SCOPES")
        or os.environ.get("SLACK_CONFIGURED_BOT_SCOPES")
        or os.environ.get("SLACK_BOT_SCOPES")
        or ""
    )
    declared = {item.strip() for item in raw.split(",") if item.strip()}
    if not env_path.is_file() and not declared:
        return _check(
            "keystone-slack scope declaration",
            "warn",
            "no local Slack .env found; verify app_mentions:read, chat:write, channels:history, and groups:history in Slack app settings",
        )
    if not declared:
        return _check(
            "keystone-slack scope declaration",
            "warn",
            "SLACK_CONFIGURED_BOT_SCOPES is not declared; verify app_mentions:read, chat:write, channels:history, and groups:history in Slack app settings",
        )
    missing = sorted(REQUIRED_SLACK_EVAL_SCOPES - declared)
    if missing:
        return _check(
            "keystone-slack scope declaration",
            "fail",
            f"declared Slack bot scopes missing for eval flow: {', '.join(missing)}",
        )
    return _check(
        "keystone-slack scope declaration",
        "pass",
        "declared Slack bot scopes cover app mentions, threaded replies, and history context",
    )


def _keystone_slack_live_read_probe() -> dict[str, Any]:
    repo = Path(os.environ.get("KEYSTONE_SLACK_REPO") or DEFAULT_SLACK_REPO)
    env_path = repo / ".env"
    env = _read_env_file(env_path) if env_path.is_file() else {}
    token = env.get("SLACK_BOT_TOKEN") or os.environ.get("SLACK_BOT_TOKEN") or ""
    if not token:
        return _check(
            "keystone-slack live read probe",
            "fail",
            "SLACK_BOT_TOKEN is not available for read-only Slack API probe",
        )
    channel_id = (
        env.get("KNI_BUSINESS_AGENTS_EVAL_CHANNEL")
        or os.environ.get("KNI_BUSINESS_AGENTS_EVAL_CHANNEL")
        or EXPECTED_EVAL_CHANNEL_ID
    )
    try:
        auth = _slack_api_call(token, "auth.test")
        if not auth.get("ok"):
            return _check(
                "keystone-slack live read probe",
                "fail",
                f"Slack auth.test failed: {_safe_slack_error(auth)}",
            )
        info = _slack_api_call(
            token,
            "conversations.info",
            {"channel": channel_id},
        )
        if not info.get("ok"):
            return _check(
                "keystone-slack live read probe",
                "fail",
                f"Slack conversations.info failed for #evals: {_safe_slack_error(info)}",
            )
        history = _slack_api_call(
            token,
            "conversations.history",
            {"channel": channel_id, "limit": "1"},
        )
        if not history.get("ok"):
            return _check(
                "keystone-slack live read probe",
                "fail",
                f"Slack conversations.history failed for #evals: {_safe_slack_error(history)}",
            )
        messages = history.get("messages") if isinstance(history.get("messages"), list) else []
        if messages:
            ts = str((messages[0] or {}).get("ts") or "")
            if ts:
                replies = _slack_api_call(
                    token,
                    "conversations.replies",
                    {"channel": channel_id, "ts": ts, "limit": "1"},
                )
                if not replies.get("ok"):
                    return _check(
                        "keystone-slack live read probe",
                        "fail",
                        f"Slack conversations.replies failed for #evals: {_safe_slack_error(replies)}",
                    )
        else:
            return _check(
                "keystone-slack live read probe",
                "warn",
                "#evals is readable but has no recent messages to probe conversations.replies",
            )
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return _check(
            "keystone-slack live read probe",
            "fail",
            f"read-only Slack API probe failed: {type(exc).__name__}",
        )
    return _check(
        "keystone-slack live read probe",
        "pass",
        "read-only Slack API probe confirmed token, #evals channel access, history, and replies reads",
    )


def _slack_api_call(
    token: str,
    method: str,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    body = urllib.parse.urlencode(params or {}).encode("utf-8")
    request = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    return data if isinstance(data, dict) else {"ok": False, "error": "invalid_response"}


def _safe_slack_error(payload: dict[str, Any]) -> str:
    text = str(payload.get("error") or "unknown_error")
    return "".join(ch for ch in text if ch.isalnum() or ch in {"_", "-", "."})[:80]


def _keystone_slack_socket_status_check() -> dict[str, Any]:
    repo = Path(os.environ.get("KEYSTONE_SLACK_REPO") or DEFAULT_SLACK_REPO)
    manager = repo / "scripts" / "manage_slack_socket.sh"
    if not manager.is_file():
        return _check(
            "keystone-slack socket status",
            "pass",
            f"socket manager not found at {manager}; verify Slack worker before live testing",
        )
    try:
        completed = subprocess.run(
            [str(manager), "status"],
            cwd=repo,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _check(
            "keystone-slack socket status",
            "fail",
            f"could not inspect Slack Socket Mode worker: {type(exc).__name__}",
        )
    output = f"{completed.stdout}\n{completed.stderr}"
    launchd_running = "state = running" in output
    process_running = "kni_integrations.slack_socket_mode" in output
    running = launchd_running or process_running
    if not running:
        return _check(
            "keystone-slack socket status",
            "fail",
            "Slack Socket Mode worker is not visibly running; run keystone-slack/scripts/manage_slack_socket.sh restart",
        )
    return _check(
        "keystone-slack socket status",
        "pass",
        "Slack Socket Mode worker is running",
    )


def _coverage_check() -> dict[str, Any]:
    counts: Counter[str] = Counter()
    for path in sorted(Path("promptfoo/tests").glob("*.yaml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("agent_under_test:"):
                counts[stripped.split(":", 1)[1].strip()] += 1
    expected = {agent: EVAL_CASE_TARGET_PER_AGENT for agent in CORE_EVAL_AGENTS}
    mismatches = {
        agent: counts.get(agent, 0)
        for agent, target in expected.items()
        if counts.get(agent, 0) != target
    }
    if mismatches:
        return _check(
            "prompt coverage",
            "fail",
            f"expected {expected}, got {dict(sorted(counts.items()))}",
        )
    return _check(
        "prompt coverage",
        "pass",
        f"{EVAL_CASE_TARGET_PER_AGENT} cases for each core agent",
    )


def _slack_bridge_check(*, tmp_dir: Path, eval_db: Path) -> dict[str, Any]:
    case_id = "slack_bridge_readiness_001"
    modal_result = handle_run_agent_interaction(
        _message_action_payload(case_id),
        context_dir=tmp_dir / "contexts",
    )
    run_result = handle_run_agent_interaction(
        _modal_submission(str((modal_result.modal_view or {})["private_metadata"])),
        database_url=f"sqlite:///{tmp_dir / 'work-items.sqlite'}",
        context_dir=tmp_dir / "contexts",
    )
    eval_record = run_result.eval_record or {}
    result_payload = run_result.result or {}
    status = eval_case_status(case_id, database_path=eval_db)
    required_summary_parts = [
        f"Eval: case `{case_id}`",
        "case dashboard",
        "score this case",
    ]
    summary = str(result_payload.get("human_summary") or "")
    ok = (
        eval_record.get("case_id") == case_id
        and str(eval_record.get("dashboard_case_url") or "").endswith(f"?case={case_id}")
        and str(eval_record.get("review_case_url") or "").endswith(f"?case={case_id}")
        and status["slack_run_count"] == 1
        and all(part in summary for part in required_summary_parts)
    )
    detail = "hidden eval metadata recorded and reply guidance appended"
    if not ok:
        detail = f"eval_record={eval_record}, slack_runs={status['slack_run_count']}"
    check = _check("slack bridge eval record", "pass" if ok else "fail", detail)
    check["case_id"] = case_id
    check["dashboard_url"] = str(eval_record.get("dashboard_url") or "")
    check["context_file_path"] = str(modal_result.context_file_path)
    check["private_metadata"] = str((modal_result.modal_view or {})["private_metadata"])
    return check


def _dashboard_link_target_check(*, dashboard_url: str) -> dict[str, Any]:
    server_defaults = build_dashboard_server_parser().parse_args([])
    parsed = urlparse(dashboard_url)
    expected_netloc = f"{server_defaults.host}:{server_defaults.port}"
    ok = (
        parsed.scheme == "http"
        and parsed.netloc == expected_netloc
        and parsed.path.rstrip("/") == "/dashboard"
    )
    detail = f"Slack links target dashboard server {dashboard_url}"
    if not ok:
        detail = f"dashboard_url={dashboard_url!r}, expected http://{expected_netloc}/dashboard"
    return _check("dashboard link target", "pass" if ok else "fail", detail)


def _slack_bridge_cli_check(*, tmp_dir: Path, private_metadata: str) -> dict[str, Any]:
    payload_path = tmp_dir / "slack-view-submission.json"
    payload_path.write_text(
        json.dumps(_modal_submission(private_metadata), ensure_ascii=True),
        encoding="utf-8",
    )
    old_argv = sys.argv
    sys.argv = [
        "handle_slack_agent_action.py",
        "--payload-file",
        str(payload_path),
        "--database-url",
        f"sqlite:///{tmp_dir / 'work-items-cli.sqlite'}",
        "--context-dir",
        str(tmp_dir / "contexts"),
    ]
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            exit_code = slack_agent_action_cli.main()
    finally:
        sys.argv = old_argv
    output = buffer.getvalue()
    ok = (
        exit_code == 0
        and "Thread reply:" in output
        and "case dashboard" in output
        and "score this case" in output
        and "Reply thread: 1781202023.470699" in output
    )
    detail = "script output includes thread reply, dashboard link, review form link, and reply thread"
    if not ok:
        detail = output.strip() or f"exit={exit_code}"
    return _check("slack bridge script output", "pass" if ok else "fail", detail)


def _dashboard_check(*, tmp_dir: Path, eval_db: Path) -> dict[str, Any]:
    output_path = render_dashboard(database_path=eval_db, output_path=tmp_dir / "dashboard.html")
    html = output_path.read_text(encoding="utf-8")
    required = [
        "Machine Avg / 5",
        "Human Avg / 5",
        "Prompt Coverage",
        "Evaluation Dimensions",
        "Slack Runs",
        "Runs & scoring",
        "Database",
        "Download CSV",
        "Interaction guardrails",
        "No API call from dashboard copy",
        "Use dry-run fixtures/cache first; cap live retrieval to accepted root run",
        "No model call; form uses saved case, run id, thread, and response context",
    ]
    missing = [item for item in required if item not in html]
    if missing:
        return _check("dashboard labels", "fail", f"missing {missing}")
    return _check("dashboard labels", "pass", str(output_path))


def _dashboard_workflow_readiness_check(*, tmp_dir: Path, eval_db: Path) -> dict[str, Any]:
    output_path = render_dashboard(
        database_path=eval_db,
        output_path=tmp_dir / "dashboard-workflow-readiness.html",
    )
    html = output_path.read_text(encoding="utf-8")
    try:
        readiness = _dashboard_workflow_readiness_from_html(html)
    except (ValueError, json.JSONDecodeError) as exc:
        return _check("dashboard workflow readiness", "fail", str(exc))
    required_interactions = {
        "Prompt copy": "No API call from dashboard copy",
        "Agent thread reply": "One agent run per accepted Slack root prompt",
        "Promptfoo machine summary": "Use imported Promptfoo result by case_id; do not rerun Promptfoo from Slack thread",
        "Retrieval/source evidence": "Use dry-run fixtures/cache first; cap live retrieval to accepted root run",
        "Human review form open": "No model call; form uses saved case, run id, thread, and response context",
        "Human review save": "No model call; no Slack post",
        "Analysis inclusion": "No rerun; recalculates from database rows",
    }
    if readiness.get("mode") != "local_preview":
        return _check(
            "dashboard workflow readiness",
            "fail",
            f"mode={readiness.get('mode')!r}, expected local_preview",
        )
    if readiness.get("live_api_calls") is not False:
        return _check(
            "dashboard workflow readiness",
            "fail",
            "workflow readiness must be local-only before Slack/API live testing",
        )
    interactions = {
        str(item.get("interaction") or ""): item
        for item in readiness.get("interactions") or []
        if isinstance(item, dict)
    }
    missing_interactions = [
        name
        for name, guardrail in required_interactions.items()
        if interactions.get(name, {}).get("cost_guardrail") != guardrail
        or interactions.get(name, {}).get("live_api_call_now") is not False
    ]
    if missing_interactions:
        return _check(
            "dashboard workflow readiness",
            "fail",
            f"missing local-only cost guardrails for {missing_interactions}",
        )
    starter_plan = readiness.get("starter_run_plan")
    if not isinstance(starter_plan, dict):
        return _check(
            "dashboard workflow readiness",
            "fail",
            "starter Slack run plan missing",
        )
    if (
        str(starter_plan.get("channel") or "") != "#evals"
        or not str(starter_plan.get("case_id") or "")
        or not str(starter_plan.get("paste_text") or "").startswith("@KNI")
        or starter_plan.get("local_only_now") is not True
        or not str(starter_plan.get("human_review_url") or "")
    ):
        return _check(
            "dashboard workflow readiness",
            "fail",
            "starter Slack run plan is not ready for local-only #evals testing",
        )
    thread_sequence = starter_plan.get("thread_sequence")
    if not isinstance(thread_sequence, list) or len(thread_sequence) < 5:
        return _check(
            "dashboard workflow readiness",
            "fail",
            "starter Slack run plan must describe the in-thread communication flow",
        )
    live_steps = starter_plan.get("expected_live_calls_when_enabled")
    if not isinstance(live_steps, list) or len(live_steps) < 4:
        return _check(
            "dashboard workflow readiness",
            "fail",
            "starter Slack run plan must list expected live-call checkpoints",
        )
    counts = readiness.get("counts") if isinstance(readiness.get("counts"), dict) else {}
    return _check(
        "dashboard workflow readiness",
        "pass",
        (
            "local-only workflow contract is available for Slack/API preflight; "
            f"{counts.get('total_cases', 0)} cases, {len(interactions)} interactions; "
            f"starter {starter_plan.get('case_id')}"
        ),
    )


def _dashboard_workflow_readiness_from_html(html: str) -> dict[str, Any]:
    marker = '<script id="eval-data" type="application/json">'
    start = html.find(marker)
    if start < 0:
        raise ValueError("dashboard eval-data payload missing")
    start += len(marker)
    end = html.find("</script>", start)
    if end < 0:
        raise ValueError("dashboard eval-data payload is not closed")
    payload = json.loads(html[start:end])
    readiness = payload.get("workflow_readiness")
    if not isinstance(readiness, dict):
        raise ValueError("dashboard workflow_readiness payload missing")
    return readiness


def _dashboard_human_review_check(*, tmp_dir: Path, eval_db: Path, case_id: str) -> dict[str, Any]:
    output_path = render_dashboard(
        database_path=eval_db,
        output_path=tmp_dir / "dashboard-after-score.html",
    )
    html = output_path.read_text(encoding="utf-8")
    required = [
        "Human Avg / 5",
        "1 reviewed",
        "4.54",
        case_id,
    ]
    missing = [item for item in required if item not in html]
    if missing:
        return _check("dashboard human review", "fail", f"missing {missing}")
    return _check("dashboard human review", "pass", "saved Slack human review appears")


def _scorecard_cli_check(*, case_id: str, context_file_path: str) -> dict[str, Any]:
    payload = _run_cli_json(
        [
            "ask",
            "--json",
            "--context-file",
            context_file_path,
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
    ok = (
        payload.get("case_id") == case_id
        and "Score each dimension 0-5" in str(payload.get("human_summary") or "")
        and str(payload.get("dashboard_case_url") or "").endswith(f"?case={case_id}")
    )
    detail = "deterministic fallback review request resolves case dashboard link"
    if not ok:
        detail = f"payload keys={sorted(payload)} case={payload.get('case_id')}"
    return _check("fallback review cli", "pass" if ok else "fail", detail)


def _score_save_cli_check(
    *,
    case_id: str,
    eval_db: Path,
    context_file_path: str,
) -> dict[str, Any]:
    payload = _run_cli_json(
        [
            "ask",
            "--json",
            "--context-file",
            context_file_path,
            "@KNI",
            _score_reply_text(),
        ]
    )
    status = eval_case_status(case_id, database_path=eval_db)
    latest_review = status.get("latest_human_review")
    ok = (
        payload.get("mode") == "eval_score_saved"
        and payload.get("case_id") == case_id
        and isinstance(latest_review, dict)
        and latest_review.get("case_id") == case_id
        and latest_review.get("safety") == "pass"
        and str(payload.get("dashboard_case_url") or "").endswith(f"?case={case_id}")
    )
    detail = "natural in-thread score reply saves human review"
    if not ok:
        detail = f"payload mode={payload.get('mode')} review={latest_review}"
    return _check("score save cli", "pass" if ok else "fail", detail)


def _status_cli_check(*, case_id: str) -> dict[str, Any]:
    payload = _run_cli_json(["ask", "--json", "@KNI", "eval", "status", "case", case_id])
    eval_status = payload.get("eval_status") if isinstance(payload.get("eval_status"), dict) else {}
    latest_review = (
        eval_status.get("latest_human_review")
        if isinstance(eval_status.get("latest_human_review"), dict)
        else {}
    )
    ok = (
        payload.get("case_id") == case_id
        and str(payload.get("dashboard_case_url") or "").endswith(f"?case={case_id}")
        and str(payload.get("review_case_url") or "").endswith(f"?case={case_id}")
        and latest_review.get("case_id") == case_id
        and latest_review.get("safety") == "pass"
    )
    detail = "eval status returns dashboard link, review form link, and human review"
    if not ok:
        detail = f"payload keys={sorted(payload)} case={payload.get('case_id')}"
    return _check("status cli", "pass" if ok else "fail", detail)


def _app_mention_thread_flow_check(*, tmp_dir: Path, eval_db: Path) -> dict[str, Any]:
    case_id = "slack_app_mention_readiness_001"
    context_file = tmp_dir / "app-mention-history-context.json"
    root_request = (
        "opportunity scout -- find three Agents SDK courses that are reasonably "
        "priced and useful for someone with some experience"
    )
    _write_history_context(
        context_file,
        case_id=case_id,
        request_text=root_request,
        read_context="",
    )
    run_payload = _run_cli_json(
        [
            "ask",
            "--json",
            "--database-url",
            f"sqlite:///{tmp_dir / 'app-mention-work-items.sqlite'}",
            "--context-file",
            str(context_file),
            "@KNI",
            root_request,
        ]
    )
    eval_record = (
        run_payload.get("_eval_record") if isinstance(run_payload.get("_eval_record"), dict) else {}
    )
    run_id = str(eval_record.get("run_id") or "")
    agent = str(eval_record.get("agent") or "")
    run_summary = str(run_payload.get("human_summary") or "")
    _write_history_context(
        context_file,
        case_id=case_id,
        request_text=root_request,
        read_context=(
            "Slack thread history digest\n"
            f"Business Agents Run Completed WorkItem: {run_id} Route: {agent}\n"
        ),
    )
    scorecard_payload = _run_cli_json(
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
    score_payload = _run_cli_json(
        [
            "ask",
            "--json",
            "--context-file",
            str(context_file),
            "@KNI",
            _score_reply_text(),
        ]
    )
    status_payload = _run_cli_json(
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
    status = eval_case_status(case_id, database_path=eval_db)
    ok = (
        eval_record.get("case_id") == case_id
        and str(eval_record.get("dashboard_case_url") or "").endswith(f"?case={case_id}")
        and str(eval_record.get("review_case_url") or "").endswith(f"?case={case_id}")
        and scorecard_payload.get("case_id") == case_id
        and scorecard_payload.get("run_id") == run_id
        and "case dashboard" in run_summary
        and "score this case" in run_summary
        and "Score each dimension 0-5" in str(scorecard_payload.get("human_summary") or "")
        and score_payload.get("mode") == "eval_score_saved"
        and score_payload.get("case_id") == case_id
        and status_payload.get("mode") == "eval_status"
        and status_payload.get("case_id") == case_id
        and status.get("slack_run_count") == 1
        and isinstance(status.get("latest_human_review"), dict)
    )
    detail = (
        "app-mention history context supports root ask, review link, "
        "deterministic fallback score save, and status"
    )
    if not ok:
        detail = (
            f"eval_record={eval_record}, scorecard={scorecard_payload.get('case_id')}, "
            f"score={score_payload.get('mode')}, status={status_payload.get('mode')}"
        )
    return _check("app mention thread flow", "pass" if ok else "fail", detail)


def _run_cli_json(argv: list[str]) -> dict[str, Any]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exit_code = cli_main(argv)
    if exit_code != 0:
        raise RuntimeError(f"CLI failed with exit {exit_code}: {' '.join(argv)}")
    return json.loads(buffer.getvalue())


def _message_action_payload(case_id: str) -> dict[str, Any]:
    return {
        "type": "message_action",
        "callback_id": RUN_AGENT_MESSAGE_CALLBACK_ID,
        "trigger_id": "trigger-readiness",
        "team": {"id": "T123", "domain": "kni"},
        "channel": {"id": "C0BA17Y9C01", "name": "evals"},
        "user": {"id": "U123", "username": "anup"},
        "message": {
            "type": "message",
            "user": "U456",
            "ts": "1781202023.470699",
            "thread_ts": "1781202023.470699",
            "text": "Can someone research Acme Health before the partner call?",
            "permalink": "https://kni.slack.com/archives/C0BA17Y9C01/p1781202023470699",
        },
        "eval": {
            "case_id": case_id,
            "source": "slack_eval_readiness_check",
            "visible_in_prompt": False,
        },
    }


def _modal_submission(private_metadata: str) -> dict[str, Any]:
    return {
        "type": "view_submission",
        "user": {"id": "U123", "username": "anup"},
        "view": {
            "callback_id": RUN_AGENT_VIEW_CALLBACK_ID,
            "private_metadata": private_metadata,
            "state": {
                "values": {
                    RUN_AGENT_TASK_BLOCK_ID: {
                        RUN_AGENT_TASK_ACTION_ID: {
                            "type": "plain_text_input",
                            "value": "research Acme Health",
                        }
                    }
                }
            },
        },
    }


def _score_reply_text() -> str:
    return """Here are my scores:
accuracy 4
relevance 5
explainability 4
readability 5
source_quality 4
search_quality 4
synthesis 4
output 5
format 5
instruction_following 5
usefulness 5
safety pass
notes: Readiness check score reply saved from the same eval thread."""


def _write_history_context(
    path: Path,
    *,
    case_id: str,
    request_text: str,
    read_context: str,
) -> None:
    payload = {
        "schema": "keystone.slack.history_context.v1",
        "source": "slack_app_mention_history",
        "channel_id": EXPECTED_EVAL_CHANNEL_ID,
        "channel_name": EXPECTED_EVAL_CHANNEL_NAME,
        "thread_ts": "1781209000.000100",
        "request_ts": "1781209000.000200",
        "request_text": request_text,
        "read_context": read_context,
        "thread_fetch_status": "ok",
        "warnings": [],
        "eval": {
            "case_id": case_id,
            "source": "slack_eval_channel_backend",
            "visible_in_prompt": False,
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _check(name: str, status: str, detail: str) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail}


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _truthy_env(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _tomorrow_operator_steps() -> dict[str, str]:
    return {
        "eval_channel": (
            f"Use #{EXPECTED_EVAL_CHANNEL_NAME} "
            f"({EXPECTED_EVAL_CHANNEL_ID}); set "
            f"KNI_BUSINESS_AGENTS_EVAL_CHANNEL={EXPECTED_EVAL_CHANNEL_ID} "
            "in the Slack bridge environment if it is overridden."
        ),
        "dashboard_server": (
            "Run `export SLACK_CONFIGURED_BOT_SCOPES=app_mentions:read,chat:write,channels:history,groups:history` "
            "then `npm run eval:slack:strict-live-test-server`; paste the printed committed eval prompt for the "
            "shown agent into #evals and leave the server running before clicking Slack case links."
        ),
        "slack_thread_flow": (
            "Keep the root ask, automatic eval footer, human review form save, and optional status request in one thread."
        ),
    }


if __name__ == "__main__":
    raise SystemExit(main())
