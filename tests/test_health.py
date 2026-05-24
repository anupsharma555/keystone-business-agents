from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import keystone_agents.health as health
from keystone_agents.agent_registry import list_agent_specs
from keystone_agents.health import (
    MASKED_VALUE,
    SEVERITY_HIGH,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_WARNING,
    format_health_report,
    report_to_json,
    run_health_check,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

HEALTH_ENV_VARS = (
    "AUTO_SEND_EMAIL",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "KEYSTONE_OPENAI_API_KEY",
    "KEYSTONE_OPENAI_BASE_URL",
    "KEYSTONE_OPENAI_MODEL",
    "SLACK_BOT_TOKEN",
    "SLACK_CHANNEL_APPROVALS",
    "SERPER_API_KEY",
    "TAVILY_API_KEY",
    "TAVILY_BASE_URL",
    "TAVILY_SEARCH_DEPTH",
    "TAVILY_MCP_LINK",
    "TAVILY_MCP_link",
    "KEYSTONE_TAVILY_MONTHLY_CREDIT_LIMIT",
    "KEYSTONE_TAVILY_MONTHLY_SOFT_LIMIT",
    "KEYSTONE_TAVILY_CREDIT_ENFORCEMENT",
    "KEYSTONE_TAVILY_USAGE_PATH",
    "APIFY_API_TOKEN",
    "BROWSERLESS_API_KEY",
    "GOOGLE_CREDENTIALS_FILE",
    "GOOGLE_TOKEN_FILE",
    "GMAIL_ACCOUNT",
    "GMAIL_LIVE_READ_ENABLED",
    "GMAIL_OAUTH_CLIENT_SECRET_PATH",
    "GMAIL_OAUTH_TOKEN_PATH",
    "KEYSTONE_ENABLE_LIVE_GMAIL",
    "GOOGLE_CALENDAR_ACCOUNT",
    "GOOGLE_CALENDAR_ID",
    "CALENDAR_LIVE_READ_ENABLED",
    "CALENDAR_OAUTH_CLIENT_SECRET_PATH",
    "CALENDAR_OAUTH_TOKEN_PATH",
    "GOOGLE_DRIVE_ACCOUNT",
    "GOOGLE_DRIVE_KNI_OPS_FOLDER",
    "GOOGLE_WORKSPACE_OAUTH_CLIENT_SECRET_PATH",
    "GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH",
    "GOOGLE_WORKSPACE_REQUIRE_ACCOUNT_MATCH",
    "GOOGLE_WORKSPACE_WRITES_ENABLED",
    "GOOGLE_OAUTH_APP_PUBLISHING_STATUS",
    "KEYSTONE_ENABLE_LIVE_SLACK",
    "KEYSTONE_ENABLE_LIVE_RESEARCH",
    "KEYSTONE_LIVE_MODE",
    "KEYSTONE_DRY_RUN",
    "KNI_BUSINESS_AGENTS_SLACK_CONTEXT_ENABLED",
    "KNI_BUSINESS_AGENTS_BACKGROUND_RUNS",
    "KNI_BUSINESS_AGENTS_APPROVALS_ENABLED",
    "KNI_BUSINESS_AGENTS_MESSAGE_ACTIONS_ENABLED",
    "KNI_BUSINESS_AGENTS_HISTORY_CONTEXT_ENABLED",
    "KNI_BUSINESS_AGENTS_LIVE_SDK",
    "KNI_BUSINESS_AGENTS_LIVE_SEARCH",
    "KNI_BUSINESS_AGENTS_LIVE_SLACK",
    "KNI_BUSINESS_AGENTS_LIVE_GMAIL_DRAFTS",
    "KNI_BUSINESS_AGENTS_APPROVAL_CHANNEL",
    "SEARCH_PROVIDER",
    "SEARXNG_BASE_URL",
    "SEARXNG_API_KEY",
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA",
)

REQUIRED_GITIGNORE_PATTERNS = {
    ".env",
    ".env.*",
    ".envrc",
    "credentials.json",
    "token.json",
    "client_secret*.json",
    "authorized_user*.json",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.db",
    "keystone_agents.db",
    "*.sqlite",
    "*.sqlite3",
    ".pytest_cache/",
    ".ruff_cache/",
    "__pycache__/",
}


@pytest.fixture(autouse=True)
def clear_health_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in HEALTH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_gitignore_covers_local_credentials_databases_and_caches() -> None:
    patterns = {
        line.strip()
        for line in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }

    assert REQUIRED_GITIGNORE_PATTERNS <= patterns


def test_health_check_runs_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "_distribution_version", lambda package_name: None)
    monkeypatch.setattr(health, "_distribution_requirements", lambda package_name: [])

    report = run_health_check(database_url=":memory:", env={})

    assert report.python["version"]
    assert report.database["status"] != STATUS_ERROR
    assert all(item.status != STATUS_ERROR for item in report.imports)
    assert all(item.status != STATUS_ERROR for item in report.agent_builders)
    assert report.overall_status == STATUS_OK


def test_health_agent_checks_follow_registry() -> None:
    report = run_health_check(database_url=":memory:", env={})

    assert {item.name for item in report.agent_builders} == {
        spec.builder_name for spec in list_agent_specs()
    }
    assert {item.details["route_name"] for item in report.agent_builders} == {
        spec.route_name for spec in list_agent_specs()
    }
    assert all(item.details["output_type_matches_registry"] for item in report.agent_builders)


def test_health_reports_litellm_gateway_mode_without_requiring_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health, "_distribution_version", lambda package_name: None)
    monkeypatch.setattr(health, "_distribution_requirements", lambda package_name: [])

    report = run_health_check(database_url=":memory:", env={})

    assert report.model_provider["litellm_mode"] == "gateway"
    assert report.model_provider["python_litellm_required"] is False
    assert report.model_provider["python_litellm_package_present"] is False
    assert report.model_provider["status"] == STATUS_OK


def test_health_warns_when_in_process_litellm_pins_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = {"litellm": "1.83.12", "openai": "2.32.0"}

    monkeypatch.setattr(
        health,
        "_distribution_version",
        lambda package_name: versions.get(package_name),
    )
    monkeypatch.setattr(
        health,
        "_distribution_requirements",
        lambda package_name: ["openai==2.24.0"] if package_name == "litellm" else [],
    )

    report = run_health_check(database_url=":memory:", env={})

    assert report.model_provider["status"] == STATUS_WARNING
    assert report.model_provider["python_litellm_exact_openai_pin"] is True
    assert any(message.code == "litellm_package_conflict" for message in report.messages)


def test_health_check_masks_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_openai_key = "sk-" + ("a" * 24)
    raw_keystone_openai_key = "sk-" + ("c" * 24)
    raw_slack_token = "slack-secret-value-" + ("b" * 24)
    monkeypatch.setenv("OPENAI_API_KEY", raw_openai_key)
    monkeypatch.setenv("OPENAI_MODEL", "openclaw-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "openclaw.local/v1")
    monkeypatch.setenv("KEYSTONE_OPENAI_API_KEY", raw_keystone_openai_key)
    monkeypatch.setenv("SLACK_BOT_TOKEN", raw_slack_token)
    monkeypatch.setenv("SLACK_CHANNEL_APPROVALS", "C0123456789")

    report = run_health_check(database_url=":memory:")
    payload = report_to_json(report)
    text = format_health_report(report)
    env_by_name = {item.name: item for item in report.environment}

    assert raw_openai_key not in payload
    assert raw_keystone_openai_key not in payload
    assert raw_slack_token not in payload
    assert raw_openai_key not in text
    assert raw_keystone_openai_key not in text
    assert raw_slack_token not in text
    assert "OPENAI_API_KEY" not in env_by_name
    assert "OPENAI_MODEL" not in env_by_name
    assert "OPENAI_BASE_URL" not in env_by_name
    assert env_by_name["KEYSTONE_OPENAI_API_KEY"].details["value"] == MASKED_VALUE
    assert env_by_name["SLACK_BOT_TOKEN"].details["value"] == MASKED_VALUE


def test_disabled_optional_integrations_are_ok_not_failures() -> None:
    report = run_health_check(database_url=":memory:", env={})
    integrations = {item.name: item for item in report.live_integrations}

    assert integrations["gmail"].status == STATUS_OK
    assert integrations["gmail"].details["readiness"] == "disabled"
    assert integrations["gmail"].details["live_enabled"] is False
    assert integrations["slack"].status != STATUS_ERROR
    assert integrations["slack"].details["required_now"] is False
    assert integrations["slack_business_agents"].status == STATUS_OK
    assert integrations["slack_business_agents"].details["required_now"] is False
    assert integrations["serper"].status != STATUS_ERROR
    assert integrations["serper"].details["required_now"] is False
    assert not any(message.severity == "error" for message in report.messages)
    assert report.overall_status != STATUS_ERROR


def test_live_flags_warn_when_credentials_are_missing() -> None:
    report = run_health_check(
        database_url=":memory:",
        env={
            "KEYSTONE_ENABLE_LIVE_RESEARCH": "true",
            "KEYSTONE_ENABLE_LIVE_SLACK": "true",
            "SEARCH_PROVIDER": "serper",
        },
    )
    integrations = {item.name: item for item in report.live_integrations}

    assert integrations["serper"].status == STATUS_WARNING
    assert integrations["slack"].status == STATUS_WARNING
    assert integrations["openai"].status == STATUS_OK
    assert report.overall_status == STATUS_WARNING
    assert any(message.code == "live_credentials_missing" for message in report.messages)


def test_slack_business_agents_health_warns_for_enabled_context_missing_config() -> None:
    report = run_health_check(
        database_url=":memory:",
        env={
            "KNI_BUSINESS_AGENTS_SLACK_CONTEXT_ENABLED": "true",
            "KNI_BUSINESS_AGENTS_BACKGROUND_RUNS": "true",
        },
    )
    item = {item.name: item for item in report.live_integrations}["slack_business_agents"]

    assert item.status == STATUS_WARNING
    assert item.details["required_now"] is True
    assert item.details["missing"] == ["SLACK_BOT_TOKEN"]
    assert any(message.code == "live_credentials_missing" for message in report.messages)


def test_slack_business_agents_health_accepts_configured_live_slack() -> None:
    report = run_health_check(
        database_url=":memory:",
        env={
            "KNI_BUSINESS_AGENTS_SLACK_CONTEXT_ENABLED": "true",
            "KNI_BUSINESS_AGENTS_LIVE_SLACK": "true",
            "SLACK_BOT_TOKEN": "fake-slack-token",
            "KNI_BUSINESS_AGENTS_APPROVAL_CHANNEL": "#ai-agents-workflow",
        },
    )
    item = {item.name: item for item in report.live_integrations}["slack_business_agents"]

    assert item.status == STATUS_OK
    assert item.details["required_now"] is True
    assert item.details["configured"] is True
    assert item.details["live_slack_enabled"] is True


def test_missing_prompt_is_reported_as_error(tmp_path) -> None:
    report = run_health_check(database_url=":memory:", env={}, prompts_root=tmp_path)

    assert report.overall_status == STATUS_ERROR
    assert all(item.status == STATUS_ERROR for item in report.prompts)
    assert any(message.code == "prompts_failed" for message in report.messages)


def test_missing_database_file_is_initialized(tmp_path) -> None:
    database_path = tmp_path / "missing-health.db"

    report = run_health_check(database_url=f"sqlite:///{database_path}", env={})

    assert database_path.exists()
    assert report.database["status"] == STATUS_OK
    assert report.database["required_tables_missing"] == []


def test_live_gmail_missing_oauth_files_is_misconfigured(tmp_path) -> None:
    report = run_health_check(
        database_url=":memory:",
        env={
            "KEYSTONE_ENABLE_LIVE_GMAIL": "true",
            "GOOGLE_TOKEN_FILE": str(tmp_path / "missing-token.json"),
            "GOOGLE_CREDENTIALS_FILE": str(tmp_path / "missing-credentials.json"),
        },
    )
    gmail = {item.name: item for item in report.live_integrations}["gmail"]

    assert gmail.status in {STATUS_WARNING, STATUS_ERROR}
    assert gmail.details["readiness"] == "misconfigured"
    assert gmail.details["token_file_present"] is False
    assert gmail.details["credentials_file_present"] is False
    assert report.overall_status in {STATUS_WARNING, STATUS_ERROR}
    assert any(message.code == "gmail_oauth_misconfigured" for message in report.messages)


def test_live_gmail_ready_with_local_oauth_files(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "token.json"
    credentials_file = tmp_path / "credentials.json"
    token_file.write_text(
        json.dumps({"refresh_token": "test-refresh-token"}),
        encoding="utf-8",
    )
    credentials_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "test-client-id",
                    "client_secret": "test-client-secret",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setenv("GOOGLE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("GOOGLE_CREDENTIALS_FILE", str(credentials_file))

    report = run_health_check(database_url=":memory:")
    payload = report_to_json(report)
    text = format_health_report(report, verbose=True)
    gmail = {item.name: item for item in report.live_integrations}["gmail"]

    assert gmail.status == STATUS_OK
    assert gmail.details["readiness"] == "ready"
    assert gmail.details["configured"] is True
    assert gmail.details["token_file_valid"] is True
    assert gmail.details["credentials_file_valid"] is True
    assert gmail.details["send_supported"] is False
    assert "test-refresh-token" not in payload
    assert "test-client-secret" not in payload
    assert "test-refresh-token" not in text
    assert "test-client-secret" not in text


def test_gmail_oauth_cli_error_does_not_include_credential_contents(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.gmail_oauth_login as oauth_cli

    secret_value = "SHOULD_NOT_APPEAR_CLIENT_SECRET_123456789"
    credentials_file = tmp_path / "oauth_client_secret_fixture.json"
    token_file = tmp_path / "token_fixture.json"
    credentials_file.write_text(
        json.dumps({"installed": {"client_secret": secret_value}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gmail_oauth_login.py",
            "--credentials-file",
            str(credentials_file),
            "--token-file",
            str(token_file),
            "--print-url-only",
        ],
    )

    with pytest.raises(ValueError) as exc_info:
        oauth_cli.main()

    error = str(exc_info.value)
    assert "client_id or client_secret" in error
    assert secret_value not in error
    assert "SHOULD_NOT_APPEAR" not in error


def test_auto_send_email_true_produces_high_severity_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTO_SEND_EMAIL", "true")

    report = run_health_check(database_url=":memory:")

    assert report.auto_send_email["enabled"] is True
    assert any(
        message.code == "auto_send_email_enabled" and message.severity == SEVERITY_HIGH
        for message in report.messages
    )


def test_health_report_json_serializes() -> None:
    report = run_health_check(database_url=":memory:", env={})
    payload = report.to_dict()
    encoded = json.dumps(payload)
    decoded = json.loads(encoded)

    assert decoded["overall_status"] == report.overall_status
    assert decoded["database"]["required_tables_missing"] == []


def test_health_check_confirms_no_auto_send_in_text_output() -> None:
    report = run_health_check(database_url=":memory:", env={})
    text = format_health_report(report)

    assert report.auto_send_email["draft_only_confirmed"] is True
    assert report.auto_send_email["send_capability_enabled"] is False
    assert "Auto-send: disabled" in text


def test_docs_cover_safe_operator_controlled_gmail_scheduling() -> None:
    deployment = (PROJECT_ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    runbook = (PROJECT_ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    combined = f"{deployment}\n{runbook}".lower()

    assert "operator-controlled periodic gmail triage" in combined
    assert "scheduled live gmail processing is not enabled by default" in combined
    assert "--live-gmail --no-dry-run" in combined
    assert "--label-filter" in combined
    assert "--max-messages 1" in combined
    assert "--save" in combined
    assert "no-auto-send" in combined
    assert "approval queue" in combined
    assert "cron" in combined
    assert "launchd" in combined
    assert "task scheduler" in combined
    assert "daemon" in combined
    assert "background service" in combined


def test_no_scheduler_or_background_service_code_is_added() -> None:
    code_roots = (PROJECT_ROOT / "scripts", PROJECT_ROOT / "src")
    forbidden_filename_terms = (
        "cron",
        "launchd",
        "scheduler",
        "schedule_gmail",
        "background_service",
        "daemon",
    )

    for root in code_roots:
        for path in root.rglob("*.py"):
            lower_name = path.name.lower()
            assert not any(term in lower_name for term in forbidden_filename_terms), (
                f"automatic scheduling code found at {path.relative_to(PROJECT_ROOT)}"
            )
