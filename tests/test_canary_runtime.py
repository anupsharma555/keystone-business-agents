from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from keystone_agents import canary_runtime_entrypoint
from keystone_agents.canary_runtime import (
    CANARY_ALLOWED_AGENTS,
    CANARY_MAX_OPENAI_REQUESTS_ENV,
    CANARY_SCRUBBED_ENV_KEYS,
    CANARY_STATE_DIR_ENV,
    MUTATION_DISABLED_ENV,
    CanaryRuntimeConfig,
    validate_canary_state_dir,
)


def _config(tmp_path: Path, *, ceiling: int = 4) -> CanaryRuntimeConfig:
    return CanaryRuntimeConfig.from_environment(
        repo_root=Path.cwd(),
        env={
            CANARY_STATE_DIR_ENV: str(tmp_path / "canary-state"),
            CANARY_MAX_OPENAI_REQUESTS_ENV: str(ceiling),
        },
        allowed_state_roots=(tmp_path,),
    )


def test_canary_environment_forces_mutations_off_and_isolates_state(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    child = config.build_child_environment(
        {
            "AIRTABLE_ALLOW_WRITES": "true",
            "GOOGLE_WORKSPACE_WRITES_ENABLED": "true",
            "KEYSTONE_GOOGLE_CALENDAR_ALLOW_WRITES": "true",
            "KEYSTONE_GMAIL_ALLOW_TEST_SENDS": "true",
            "KEYSTONE_ENABLE_LIVE_RESEARCH": "true",
            "KEYSTONE_OPENAI_API_KEY": "secret-not-serialized",
            "SLACK_APP_TOKEN": "app-secret",
            "SLACK_SIGNING_SECRET": "signing-secret",
        }
    )

    for key, expected in MUTATION_DISABLED_ENV.items():
        assert child[key] == expected
    assert child["KEYSTONE_ENABLE_LIVE_RESEARCH"] == "true"
    assert child["KEYSTONE_OPENAI_API_KEY"] == "secret-not-serialized"
    assert child["SLACK_BOT_TOKEN"] == "disabled-in-kba-canary"
    for key in CANARY_SCRUBBED_ENV_KEYS:
        assert key not in child
    assert child["DATABASE_URL"] == config.database_url
    assert child["GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH"] == str(config.google_workspace_token_path)
    assert child["GOOGLE_TOKEN_FILE"] == str(config.google_workspace_token_path)
    assert child["KEYSTONE_LIVE_MODEL_MAX_RETRIES"] == "0"
    assert child["KEYSTONE_PLAYWRIGHT_ENABLED"] == "false"
    assert child["KEYSTONE_ENABLE_GEMINI_FALLBACK"] == "false"
    assert child["KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES"] == "0"
    assert child["KEYSTONE_SDK_STRUCTURED_OUTPUT_MAX_RETRIES"] == "0"
    assert child["KEYSTONE_TRACING_DISABLED"] == "true"
    assert Path(child["KEYSTONE_SDK_SESSION_DB"]).is_relative_to(config.state_dir)
    assert Path(child["KEYSTONE_TRACE_SUMMARY_DB"]).is_relative_to(config.state_dir)
    assert str(Path.cwd()) in child["PYTHONPATH"].split(os.pathsep)

    serialized = json.dumps(config.public_manifest(), sort_keys=True)
    assert "secret-not-serialized" not in serialized
    assert "KEYSTONE_OPENAI_API_KEY" not in serialized


def test_canary_stages_workspace_token_once_without_rewriting_source(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    source = tmp_path / "operator-token.json"
    source.write_text('{"refresh_token":"operator-original"}', encoding="utf-8")

    staged = config.stage_google_workspace_token(source)
    assert staged.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert staged.stat().st_mode & 0o777 == 0o600

    staged.write_text('{"refresh_token":"canary-refreshed"}', encoding="utf-8")
    assert config.stage_google_workspace_token(source) == staged
    assert staged.read_text(encoding="utf-8") == '{"refresh_token":"canary-refreshed"}'
    assert source.read_text(encoding="utf-8") == '{"refresh_token":"operator-original"}'


def test_canary_rewrites_bridge_database_and_caps_existing_request_limit(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, ceiling=4)
    rewritten = config.rewrite_python_arguments(
        [
            "-m",
            "keystone_agents.cli",
            "ask",
            "--database-url",
            "sqlite:////unsafe/operator.sqlite3",
            "--max-openai-requests",
            "9",
            "--agent",
            "google_workspace_context_agent",
            "summarize the selected work document",
        ]
    )

    assert rewritten[rewritten.index("--database-url") + 1] == config.database_url
    assert rewritten[rewritten.index("--max-openai-requests") + 1] == "4"
    assert "sqlite:////unsafe/operator.sqlite3" not in rewritten


@pytest.mark.parametrize(
    "arguments",
    [
        ["-c", "print('unsafe')"],
        ["scripts/run_company_research.py"],
        ["-m", "keystone_agents.cli", "health"],
        [
            "-m",
            "keystone_agents.cli",
            "ask",
            "--agent",
            "gmail_triage",
            "read messages",
        ],
        ["-m", "keystone_agents.cli", "ask", "missing an explicit agent"],
        [
            "-m",
            "keystone_agents.cli",
            "ask",
            "--agent",
            "google_workspace_context_agent",
            "--agent",
            "business_research_analyst",
            "ambiguous owner",
        ],
    ],
)
def test_canary_rejects_unapproved_python_commands(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    config = _config(tmp_path)

    with pytest.raises(ValueError, match="Canary"):
        config.rewrite_python_arguments(arguments)


def test_canary_adds_request_ceiling_to_canonical_ask(tmp_path: Path) -> None:
    config = _config(tmp_path, ceiling=6)
    rewritten = config.rewrite_python_arguments(
        [
            "-m",
            "keystone_agents.cli",
            "ask",
            "--agent",
            "business_research_analyst",
            "deeply research an anchor and two competitors",
        ]
    )

    ask_index = rewritten.index("ask")
    assert rewritten[ask_index + 1 : ask_index + 3] == [
        "--max-openai-requests",
        "6",
    ]


def test_canary_state_must_be_specific_temp_child(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute path"):
        validate_canary_state_dir("relative/state", allowed_roots=(tmp_path,))
    with pytest.raises(ValueError, match="child of an approved temp root"):
        validate_canary_state_dir(tmp_path, allowed_roots=(tmp_path,))
    with pytest.raises(ValueError, match="child of an approved temp root"):
        validate_canary_state_dir(
            tmp_path.parent / "operator-state",
            allowed_roots=(tmp_path,),
        )


def test_wrapper_preview_has_no_exec_or_secret_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir = tmp_path / "preview-state"
    monkeypatch.setenv(CANARY_STATE_DIR_ENV, str(state_dir))
    monkeypatch.setenv(CANARY_MAX_OPENAI_REQUESTS_ENV, "4")
    monkeypatch.setenv("KEYSTONE_OPENAI_API_KEY", "preview-secret")

    assert canary_runtime_entrypoint.main(["--canary-preview"]) == 0

    output = capsys.readouterr()
    assert "preview-secret" not in output.out
    assert json.loads(output.out)["provider_read_policy_allowed"] is True
    assert set(json.loads(output.out)["allowed_agents"]) == CANARY_ALLOWED_AGENTS
    assert state_dir.is_dir()


def test_wrapper_execs_repo_interpreter_with_confined_args_and_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "exec-state"
    monkeypatch.setenv(CANARY_STATE_DIR_ENV, str(state_dir))
    monkeypatch.setenv(CANARY_MAX_OPENAI_REQUESTS_ENV, "4")
    monkeypatch.setenv("AIRTABLE_ALLOW_WRITES", "true")
    source_token = tmp_path / "operator-token.json"
    source_token.write_text('{"refresh_token":"operator-original"}', encoding="utf-8")
    monkeypatch.setenv("GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH", str(source_token))
    captured: dict[str, object] = {}

    def fake_execve(
        path: str,
        argv: list[str],
        env: dict[str, str],
    ) -> None:
        captured.update(path=path, argv=argv, env=env)

    monkeypatch.setattr(canary_runtime_entrypoint.os, "execve", fake_execve)

    assert (
        canary_runtime_entrypoint.main(
            [
                "-m",
                "keystone_agents.cli",
                "ask",
                "--database-url",
                "sqlite:////unsafe/operator.sqlite3",
                "--max-openai-requests",
                "8",
                "--agent",
                "google_workspace_context_agent",
                "read the selected work file",
            ]
        )
        == 127
    )

    child_argv = captured["argv"]
    child_env = captured["env"]
    assert isinstance(child_argv, list)
    assert isinstance(child_env, dict)
    assert captured["path"] == str(Path.cwd() / ".venv" / "bin" / "python")
    assert child_argv[child_argv.index("--database-url") + 1] == (
        f"sqlite:///{state_dir.resolve() / 'keystone-agents.sqlite3'}"
    )
    assert child_argv[child_argv.index("--max-openai-requests") + 1] == "4"
    assert child_env["AIRTABLE_ALLOW_WRITES"] == "false"
    assert child_env["KEYSTONE_ENABLE_LIVE_SLACK"] == "false"
    assert (
        Path(child_env["GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH"]).read_text(encoding="utf-8")
        == '{"refresh_token":"operator-original"}'
    )
    assert source_token.read_text(encoding="utf-8") == '{"refresh_token":"operator-original"}'


def test_shell_wrapper_uses_repo_interpreter_for_fixed_import_check(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "shell-state"
    env = {
        **os.environ,
        CANARY_STATE_DIR_ENV: str(state_dir),
        CANARY_MAX_OPENAI_REQUESTS_ENV: "4",
    }

    completed = subprocess.run(
        [str(Path.cwd() / "scripts" / "kba_no_write_python"), "--canary-import-check"],
        cwd=Path.cwd(),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert Path(payload["python"]).resolve() == (Path.cwd() / ".venv" / "bin" / "python").resolve()
    assert Path(payload["keystone_agents"]).is_relative_to(Path.cwd())
    assert Path(payload["execution_telemetry"]).is_relative_to(Path.cwd())
    assert Path(payload["provider_read"]).is_relative_to(Path.cwd())
