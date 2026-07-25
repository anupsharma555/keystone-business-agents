import sqlite3
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace


def _load_gate_module():
    path = Path("scripts/run_ai_agents_workflow_no_live_gate.py")
    spec = spec_from_file_location("run_ai_agents_workflow_no_live_gate", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_current_no_live_gate_excludes_legacy_promptfoo_and_live_flags() -> None:
    module = _load_gate_module()
    commands = module.gate_commands()
    rendered = [" ".join(command) for command in commands]

    assert any(
        "compare_langgraph_quality.py --all-scenarios --require-ready" in row
        for row in rendered
    )
    assert "tests/test_semantic_execution.py" in commands[0]
    assert "tests/test_semantic_routing_variations.py" in commands[0]
    assert any("run_slack_agent_expansion_gate.py --quiet" in row for row in rendered)
    expansion_command = next(
        command
        for command in commands
        if any(part.endswith("run_slack_agent_expansion_gate.py") for part in command)
    )
    assert expansion_command[-2:] == ["--python", module.sys.executable]
    assert any("run_advanced_manager_acceptance.py" in row for row in rendered)
    assert any("run_manager_delegation_readiness.py" in row for row in rendered)
    assert any("run_slack_entrypoint_readiness.py" in row for row in rendered)
    assert any("eval:slack:anu60-preflight" in row for row in rendered)
    assert all("eval:slack:strict-readiness" not in row for row in rendered)
    assert all("eval:promptfoo:json" not in row for row in rendered)
    assert all("--live-sdk" not in row for row in rendered)
    assert all("--live-search" not in row for row in rendered)
    assert all("strict-live-readiness" not in row for row in rendered)


def test_current_no_live_gate_can_isolate_repo_only_checks() -> None:
    module = _load_gate_module()
    commands = module.gate_commands(include_anu60=False)

    assert len(commands) == 6
    assert commands[0][2:4] == ["pytest", "tests/test_advanced_manager_scenarios.py"]


def test_current_no_live_gate_can_optionally_check_deferred_evals_runtime() -> None:
    module = _load_gate_module()
    commands = module.gate_commands(include_evals_readiness=True)

    assert any("eval:slack:strict-readiness" in " ".join(row) for row in commands)


def test_current_no_live_gate_can_run_full_pytest_with_same_state_boundary() -> None:
    module = _load_gate_module()
    commands = module.gate_commands(full_pytest=True)

    assert commands[0] == [module.sys.executable, "-m", "pytest", "-q"]
    assert any("run_slack_agent_expansion_gate.py --quiet" in " ".join(row) for row in commands)


def test_current_no_live_gate_builds_credential_free_temporary_state_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_gate_module()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///keystone_agents.db")
    monkeypatch.setenv("KEYSTONE_HOME", "/operator/state")
    monkeypatch.setenv("KEYSTONE_OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "not-a-real-token")

    env = module._isolated_validation_env(tmp_path)

    assert env["PYTHON_DOTENV_DISABLED"] == "1"
    assert env["KEYSTONE_TEST_MODE"] == "1"
    assert env["DATABASE_URL"] == f"sqlite:///{tmp_path / 'keystone_agents.db'}"
    assert env["KEYSTONE_HOME"] == str(tmp_path / ".keystone")
    assert env["KEYSTONE_RUNTIME_STATE_DIR"] == str(tmp_path)
    assert env["KEYSTONE_SDK_SESSION_DB"] == str(tmp_path / "sdk_sessions.sqlite3")
    assert env["KEYSTONE_LIVE_MODE"] == "false"
    assert env["KEYSTONE_DRY_RUN"] == "true"
    assert env["KEYSTONE_ENABLE_LIVE_GMAIL"] == "false"
    assert env["KEYSTONE_ENABLE_LIVE_SLACK"] == "false"
    assert env["SEARCH_PROVIDER"] == "dry-run"
    assert "KEYSTONE_OPENAI_API_KEY" not in env
    assert "SLACK_BOT_TOKEN" not in env


def test_current_no_live_gate_passes_isolated_env_to_every_child(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_gate_module()
    captured_envs: list[dict[str, str]] = []
    monkeypatch.setattr(
        module,
        "gate_commands",
        lambda **_kwargs: [["offline-check-one"], ["offline-check-two"]],
    )

    def fake_run(command, *, cwd, check, env):
        assert command in (["offline-check-one"], ["offline-check-two"])
        assert cwd == module.PROJECT_ROOT
        assert check is False
        captured_envs.append(dict(env))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert (
        module.main(
            [
                "--skip-anu60",
                "--operator-database",
                str(tmp_path / "operator.db"),
            ]
        )
        == 0
    )
    assert len(captured_envs) == 2
    assert all(env["KEYSTONE_TEST_MODE"] == "1" for env in captured_envs)
    assert all(env["KEYSTONE_DRY_RUN"] == "true" for env in captured_envs)
    assert all(
        env["DATABASE_URL"] != "sqlite:///keystone_agents.db" for env in captured_envs
    )
    assert len({env["DATABASE_URL"] for env in captured_envs}) == 1


def test_operator_state_snapshot_and_comparison_detect_new_rows_and_orphans(tmp_path) -> None:
    module = _load_gate_module()
    database_path = tmp_path / "operator.sqlite"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE work_items (id TEXT PRIMARY KEY);
            CREATE TABLE work_item_events (id INTEGER PRIMARY KEY, work_item_id TEXT);
            CREATE TABLE work_item_artifacts (id INTEGER PRIMARY KEY, work_item_id TEXT);
            CREATE TABLE memory_items (id TEXT PRIMARY KEY);
            CREATE TABLE memory_index (memory_id TEXT);
            CREATE TABLE agent_runs (id TEXT PRIMARY KEY);
            CREATE TABLE tool_events (id INTEGER PRIMARY KEY, run_id TEXT);
            CREATE TABLE agent_run_logs (id INTEGER PRIMARY KEY, run_id TEXT);
            INSERT INTO work_items (id) VALUES ('wi_1');
            INSERT INTO work_item_events (work_item_id) VALUES ('wi_1');
            """
        )
        connection.commit()
    finally:
        connection.close()

    before = module._operator_state_snapshot(database_path)
    unchanged, changes = module._compare_operator_state(before, before)
    assert unchanged is True
    assert changes == []

    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "INSERT INTO work_item_events (work_item_id) VALUES ('missing')"
        )
        connection.commit()
    finally:
        connection.close()

    after = module._operator_state_snapshot(database_path)
    unchanged, changes = module._compare_operator_state(before, after)
    assert unchanged is False
    assert "table work_item_events: 1 -> 2" in changes
    assert "orphan count work_item_events_without_work_item: 0 -> 1" in changes


def test_operator_state_snapshot_handles_missing_database(tmp_path) -> None:
    module = _load_gate_module()
    missing = module._operator_state_snapshot(tmp_path / "missing.sqlite")

    assert missing == {
        "exists": False,
        "table_counts": {},
        "orphan_counts": {},
    }
