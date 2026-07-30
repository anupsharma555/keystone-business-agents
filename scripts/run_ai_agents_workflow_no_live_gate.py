#!/usr/bin/env python3
"""Run the current ai-agents-workflow no-live validation ladder.

This intentionally excludes the legacy 102-case Promptfoo suite. That suite is
reserved for future migration and reactivation after the current agents and
backend-selected graph paths are stable.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OPERATOR_DATABASE = PROJECT_ROOT / "keystone_agents.db"
LIVE_CREDENTIAL_KEYS = (
    "KEYSTONE_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "SERPER_API_KEY",
    "TAVILY_API_KEY",
    "EXA_API_KEY",
    "FIRECRAWL_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_WEBHOOK_URL",
    "GOOGLE_APPLICATION_CREDENTIALS",
)

FOCUSED_TESTS = (
    "tests/test_advanced_manager_scenarios.py",
    "tests/test_basic_agent_execution_smoke_tasks.py",
    "tests/test_human_agent_execution_jobs.py",
    "tests/test_agent_operational_validation_status.py",
    "tests/test_announcement_context_tools.py",
    "tests/test_announcement_context_read_validation.py",
    "tests/test_semantic_execution.py",
    "tests/test_semantic_routing_variations.py",
    "tests/test_slack_natural_replay.py",
    "tests/test_manual_request_plan.py",
    "tests/test_manager_delegation_readiness.py",
    "tests/test_slack_entrypoint_readiness.py",
    "tests/test_workflow_runner.py",
    "tests/test_langgraph_workflow.py",
    "tests/test_langgraph_quality.py",
    "tests/test_cli_work_items.py",
)


def gate_commands(
    *,
    include_anu60: bool = True,
    include_evals_readiness: bool = False,
    full_pytest: bool = False,
) -> list[list[str]]:
    pytest_command = [sys.executable, "-m", "pytest", "-q"]
    if not full_pytest:
        pytest_command[3:3] = FOCUSED_TESTS
    commands = [
        pytest_command,
        [
            sys.executable,
            "scripts/compare_langgraph_quality.py",
            "--all-scenarios",
            "--require-ready",
        ],
        [
            sys.executable,
            "scripts/run_slack_agent_expansion_gate.py",
            "--quiet",
            "--python",
            sys.executable,
        ],
        [
            sys.executable,
            "scripts/run_advanced_manager_acceptance.py",
        ],
        [
            sys.executable,
            "scripts/run_manager_delegation_readiness.py",
        ],
        [
            sys.executable,
            "scripts/run_slack_entrypoint_readiness.py",
        ],
    ]
    if include_anu60:
        commands.append(["npm", "run", "eval:slack:anu60-preflight"])
    if include_evals_readiness:
        commands.append(["npm", "run", "eval:slack:strict-readiness", "--", "--json"])
    return commands


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run current KBA ai-agents-workflow validation without live APIs."
    )
    parser.add_argument("--list", action="store_true", help="Print commands without running them.")
    parser.add_argument("--skip-anu60", action="store_true")
    parser.add_argument(
        "--include-evals-readiness",
        action="store_true",
        help=(
            "Also check the deferred #evals dashboard/runtime. This is not part of "
            "the current agent/graph gate."
        ),
    )
    parser.add_argument(
        "--full-pytest",
        action="store_true",
        help=(
            "Run the ordinary full pytest suite instead of the focused current-workflow "
            "slice while retaining all operator-state invariants."
        ),
    )
    parser.add_argument("--json-output", default="")
    parser.add_argument(
        "--operator-database",
        default=str(DEFAULT_OPERATOR_DATABASE),
        help=(
            "Operator SQLite database whose table and orphan counts must remain "
            "unchanged while the no-live gate runs."
        ),
    )
    args = parser.parse_args(argv)

    commands = gate_commands(
        include_anu60=not args.skip_anu60,
        include_evals_readiness=args.include_evals_readiness,
        full_pytest=args.full_pytest,
    )
    if args.list:
        for command in commands:
            print(" ".join(command))
        return 0

    operator_database = Path(args.operator_database).expanduser().resolve()
    operator_state_before = _operator_state_snapshot(operator_database)
    rows: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="kba-no-live-gate-") as state_dir_value:
        state_dir = Path(state_dir_value)
        validation_env = _isolated_validation_env(state_dir)
        for command in commands:
            print(f"\n== {' '.join(command)} ==", flush=True)
            completed = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                check=False,
                env=validation_env,
            )
            rows.append({"command": command, "exit_code": completed.returncode})
            if completed.returncode != 0:
                operator_state_after = _operator_state_snapshot(operator_database)
                state_unchanged, state_changes = _compare_operator_state(
                    operator_state_before,
                    operator_state_after,
                )
                _write_summary(
                    args.json_output,
                    rows,
                    passed=False,
                    operator_database=operator_database,
                    operator_state_before=operator_state_before,
                    operator_state_after=operator_state_after,
                    state_unchanged=state_unchanged,
                    state_changes=state_changes,
                )
                return completed.returncode

    operator_state_after = _operator_state_snapshot(operator_database)
    state_unchanged, state_changes = _compare_operator_state(
        operator_state_before,
        operator_state_after,
    )
    _write_summary(
        args.json_output,
        rows,
        passed=state_unchanged,
        operator_database=operator_database,
        operator_state_before=operator_state_before,
        operator_state_after=operator_state_after,
        state_unchanged=state_unchanged,
        state_changes=state_changes,
    )
    if not state_unchanged:
        print("\nOperator-state isolation failed:")
        for change in state_changes:
            print(f"- {change}")
        return 2
    print(
        "\nai-agents-workflow no-live gate passed; no live API/search/connector call "
        "ran and operator SQLite table/orphan counts were unchanged."
    )
    return 0


def _isolated_validation_env(state_dir: Path) -> dict[str, str]:
    """Return a credential-free child environment with temporary local state."""

    env = dict(os.environ)
    for key in LIVE_CREDENTIAL_KEYS:
        env.pop(key, None)
    env.update(
        {
            "PYTHON_DOTENV_DISABLED": "1",
            "KEYSTONE_TEST_MODE": "1",
            "KEYSTONE_HOME": str(state_dir / ".keystone"),
            "KEYSTONE_RUNTIME_STATE_DIR": str(state_dir),
            "DATABASE_URL": f"sqlite:///{state_dir / 'keystone_agents.db'}",
            "KEYSTONE_SDK_SESSION_DB": str(state_dir / "sdk_sessions.sqlite3"),
            "KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB": str(
                state_dir / "human-reviews.sqlite"
            ),
            "KEYSTONE_TRACE_SUMMARY_DB": str(state_dir / "trace-summaries.sqlite"),
            "KEYSTONE_LIVE_MODE": "false",
            "KEYSTONE_DRY_RUN": "true",
            "KEYSTONE_ENABLE_LIVE_GMAIL": "false",
            "KEYSTONE_ENABLE_LIVE_SLACK": "false",
            "KEYSTONE_ENABLE_LIVE_RESEARCH": "false",
            "KEYSTONE_ENABLE_LIVE_CRM": "false",
            "KEYSTONE_ENABLE_WEBSITE_EXTRACTION": "false",
            "SEARCH_PROVIDER": "dry-run",
            "AUTO_SEND_EMAIL": "false",
        }
    )
    return env


def _operator_state_snapshot(path: Path) -> dict[str, object]:
    """Return non-sensitive row and orphan counts from the operator database."""

    if not path.is_file():
        return {
            "exists": False,
            "table_counts": {},
            "orphan_counts": {},
        }
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        table_counts = {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in tables
        }
        orphan_queries = {
            "work_item_events_without_work_item": (
                "SELECT COUNT(*) FROM work_item_events AS child "
                "LEFT JOIN work_items AS parent ON parent.id = child.work_item_id "
                "WHERE child.work_item_id <> '' AND parent.id IS NULL"
            ),
            "work_item_artifacts_without_work_item": (
                "SELECT COUNT(*) FROM work_item_artifacts AS child "
                "LEFT JOIN work_items AS parent ON parent.id = child.work_item_id "
                "WHERE child.work_item_id <> '' AND parent.id IS NULL"
            ),
            "memory_index_without_item": (
                "SELECT COUNT(*) FROM memory_index AS child "
                "LEFT JOIN memory_items AS parent ON parent.id = child.memory_id "
                "WHERE parent.id IS NULL"
            ),
            "tool_events_without_agent_run": (
                "SELECT COUNT(*) FROM tool_events AS child "
                "LEFT JOIN agent_runs AS parent ON parent.id = child.run_id "
                "WHERE child.run_id <> '' AND parent.id IS NULL"
            ),
            "agent_run_logs_without_agent_run": (
                "SELECT COUNT(*) FROM agent_run_logs AS child "
                "LEFT JOIN agent_runs AS parent ON parent.id = child.run_id "
                "WHERE child.run_id <> '' AND parent.id IS NULL"
            ),
        }
        orphan_counts = {
            name: int(connection.execute(query).fetchone()[0])
            for name, query in orphan_queries.items()
            if all(table in table_counts for table in _query_tables(name))
        }
    finally:
        connection.close()
    return {
        "exists": True,
        "table_counts": table_counts,
        "orphan_counts": orphan_counts,
    }


def _query_tables(orphan_name: str) -> tuple[str, ...]:
    return {
        "work_item_events_without_work_item": ("work_item_events", "work_items"),
        "work_item_artifacts_without_work_item": ("work_item_artifacts", "work_items"),
        "memory_index_without_item": ("memory_index", "memory_items"),
        "tool_events_without_agent_run": ("tool_events", "agent_runs"),
        "agent_run_logs_without_agent_run": ("agent_run_logs", "agent_runs"),
    }[orphan_name]


def _compare_operator_state(
    before: dict[str, object],
    after: dict[str, object],
) -> tuple[bool, list[str]]:
    """Require exact table counts and no increase in known orphan counts."""

    changes: list[str] = []
    if before.get("exists") != after.get("exists"):
        changes.append(
            f"database existence changed: {before.get('exists')} -> {after.get('exists')}"
        )
    before_tables = dict(before.get("table_counts") or {})
    after_tables = dict(after.get("table_counts") or {})
    for table in sorted(set(before_tables) | set(after_tables)):
        old = before_tables.get(table)
        new = after_tables.get(table)
        if old != new:
            changes.append(f"table {table}: {old} -> {new}")
    before_orphans = dict(before.get("orphan_counts") or {})
    after_orphans = dict(after.get("orphan_counts") or {})
    for name in sorted(set(before_orphans) | set(after_orphans)):
        old = int(before_orphans.get(name) or 0)
        new = int(after_orphans.get(name) or 0)
        if new > old:
            changes.append(f"orphan count {name}: {old} -> {new}")
    return not changes, changes


def _write_summary(
    path_value: str,
    rows: list[dict[str, object]],
    *,
    passed: bool,
    operator_database: Path,
    operator_state_before: dict[str, object],
    operator_state_after: dict[str, object],
    state_unchanged: bool,
    state_changes: list[str],
) -> None:
    if not path_value:
        return
    path = Path(path_value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "keystone.ai_agents_workflow.no_live_gate.v1",
                "passed": passed,
                "live_sdk": False,
                "live_search": False,
                "legacy_promptfoo_102_included": False,
                "commands": rows,
                "operator_state_isolation": {
                    "checked": True,
                    "database_filename": operator_database.name,
                    "state_unchanged": state_unchanged,
                    "changes": state_changes,
                    "table_counts_before": operator_state_before.get("table_counts", {}),
                    "table_counts_after": operator_state_after.get("table_counts", {}),
                    "orphan_counts_before": operator_state_before.get("orphan_counts", {}),
                    "orphan_counts_after": operator_state_after.get("orphan_counts", {}),
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
