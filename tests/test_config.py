from __future__ import annotations

import socket

import pytest

from keystone_agents.config import (
    cli_default_dry_run,
    cli_default_live_gmail,
    cli_default_live_research,
    cli_default_live_sdk,
    cli_live_test_stage_enabled,
    default_database_url,
    load_settings,
    runtime_state_dir,
    with_cli_environment,
)
from keystone_agents.storage.sqlite_store import database_url_from_env, sqlite_path_from_url
from keystone_agents.tools.search_provider import SerperConfigurationError, SerperSearchProvider

DOTENV_BACKED_ENV_VARS = (
    "MODEL_PROVIDER",
    "KEYSTONE_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "KEYSTONE_OPENAI_BASE_URL",
    "KEYSTONE_OPENAI_MODEL",
    "KEYSTONE_ORCHESTRATOR_MODEL",
    "KEYSTONE_ORCHESTRATOR_MODEL_PROVIDER",
    "KEYSTONE_ORCHESTRATOR_BASE_URL",
    "KEYSTONE_GMAIL_TRIAGE_MODEL",
    "KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER",
    "KEYSTONE_GMAIL_TRIAGE_BASE_URL",
    "KEYSTONE_BUSINESS_RESEARCH_ANALYST_MODEL",
    "KEYSTONE_BUSINESS_RESEARCH_ANALYST_MODEL_PROVIDER",
    "KEYSTONE_BUSINESS_RESEARCH_ANALYST_BASE_URL",
    "KEYSTONE_OPPORTUNITY_SCOUT_MODEL",
    "KEYSTONE_OPPORTUNITY_SCOUT_MODEL_PROVIDER",
    "KEYSTONE_OPPORTUNITY_SCOUT_BASE_URL",
    "KEYSTONE_OUTREACH_COMPOSER_MODEL",
    "KEYSTONE_OUTREACH_COMPOSER_MODEL_PROVIDER",
    "KEYSTONE_OUTREACH_COMPOSER_BASE_URL",
    "GEMINI_API_KEY",
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GOOGLE_CREDENTIALS_FILE",
    "GOOGLE_TOKEN_FILE",
    "SLACK_BOT_TOKEN",
    "SLACK_CHANNEL_APPROVALS",
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
    "EXA_API_KEY",
    "EXA_BASE_URL",
    "EXA_SERVICE_API_KEY",
    "EXA_API_KEY_ID",
    "EXA_API_KEY_NAME",
    "KEYSTONE_EXA_MONTHLY_FREE_REQUEST_LIMIT",
    "KEYSTONE_EXA_SEARCH_FALLBACK",
    "KEYSTONE_EXA_SEARCH_MAX_CALLS_PER_RUN",
    "KEYSTONE_SERPER_ENABLED",
    "SERPER_API_KEY",
    "SEARXNG_BASE_URL",
    "SEARXNG_API_KEY",
    "TAVILY_API_KEY",
    "TAVILY_BASE_URL",
    "TAVILY_SEARCH_DEPTH",
    "TAVILY_MCP_LINK",
    "TAVILY_MCP_link",
    "KEYSTONE_TAVILY_MONTHLY_CREDIT_LIMIT",
    "KEYSTONE_TAVILY_MONTHLY_SOFT_LIMIT",
    "KEYSTONE_TAVILY_CREDIT_ENFORCEMENT",
    "KEYSTONE_TAVILY_USAGE_PATH",
    "KEYSTONE_TAVILY_SEARCH_MAX_CALLS_PER_RUN",
    "APIFY_API_TOKEN",
    "BROWSERLESS_API_KEY",
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_BASE_URL",
    "KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK",
    "KEYSTONE_AGENTS_WEB_SEARCH_PARALLEL",
    "KEYSTONE_AGENTS_WEB_SEARCH_MAX_CALLS_PER_RUN",
    "KEYSTONE_ENABLE_WEBSITE_EXTRACTION",
    "KEYSTONE_WEBSITE_EXTRACTOR",
    "KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK",
    "KEYSTONE_WEBSITE_EXTRACTION_MAX_PAGES",
    "KEYSTONE_TRACE_WORKFLOW_NAME",
    "KEYSTONE_TRACE_GROUP_ID",
    "KEYSTONE_TRACE_METADATA",
    "KEYSTONE_TRACING_DISABLED",
    "KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA",
    "DATABASE_URL",
    "KEYSTONE_HOME",
    "KEYSTONE_RUNTIME_STATE_DIR",
)


def _clear_dotenv_backed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in DOTENV_BACKED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_pytest_delenv_is_not_repopulated_from_local_dotenv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_dotenv_backed_env(monkeypatch)

    settings = load_settings()

    assert settings.openai_api_key is None
    assert settings.gemini_api_key is None
    assert settings.gmail_client_secret is None
    assert settings.slack_bot_token is None
    assert settings.exa_api_key is None
    assert settings.exa_service_api_key is None
    assert settings.exa_api_key_id is None
    assert settings.exa_api_key_name is None
    assert settings.serper_api_key is None
    assert settings.apify_api_token is None
    assert settings.browserless_api_key is None
    assert settings.website_extractor == "trafilatura"
    assert settings.firecrawl_api_key is None
    assert settings.search_provider == "dry-run"


def test_load_settings_loads_explicit_dotenv_when_test_guard_is_removed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_dotenv_backed_env(monkeypatch)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=dotenv-test-openai-key",
                "OPENAI_MODEL=dotenv-openclaw-model",
                "OPENAI_BASE_URL=openclaw.local/v1",
                "KEYSTONE_OPENAI_API_KEY=dotenv-test-keystone-openai-key",
                "KEYSTONE_OPENAI_MODEL=dotenv-test-model",
                "KEYSTONE_OPENAI_BASE_URL=https://keystone.example/v1",
                "KEYSTONE_GMAIL_TRIAGE_MODEL=dotenv-gmail-model",
                "KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER=gemini",
                "KEYSTONE_GMAIL_TRIAGE_BASE_URL=http://localhost:4000/v1",
                "GEMINI_API_KEY=dotenv-test-gemini-key",
                "SLACK_BOT_TOKEN=dotenv-test-slack-token",
                "SLACK_CHANNEL_APPROVALS=CUNITTEST",
                "SEARCH_PROVIDER=serper",
                "EXA_API_KEY=dotenv-test-exa-key",
                "EXA_BASE_URL=https://exa.example",
                "EXA_SERVICE_API_KEY=dotenv-test-exa-service-key",
                "EXA_API_KEY_ID=dotenv-test-exa-key-id",
                "EXA_API_KEY_NAME=default",
                "KEYSTONE_EXA_MONTHLY_FREE_REQUEST_LIMIT=1000",
                "SERPER_API_KEY=dotenv-test-serper-key",
                "TAVILY_API_KEY=dotenv-test-tavily-key",
                "TAVILY_BASE_URL=https://tavily.example",
                "TAVILY_SEARCH_DEPTH=fast",
                "TAVILY_MCP_LINK=https://mcp.tavily.example",
                "KEYSTONE_TAVILY_MONTHLY_CREDIT_LIMIT=1000",
                "KEYSTONE_TAVILY_MONTHLY_SOFT_LIMIT=850",
                "KEYSTONE_TAVILY_CREDIT_ENFORCEMENT=warn",
                "KEYSTONE_TAVILY_USAGE_PATH=/tmp/keystone-tavily-usage.json",
                "KEYSTONE_WEBSITE_EXTRACTOR=firecrawl",
                "FIRECRAWL_API_KEY=dotenv-test-firecrawl-key",
                "FIRECRAWL_BASE_URL=https://firecrawl.example",
                "KEYSTONE_TRACE_WORKFLOW_NAME=Dotenv test workflow",
                'KEYSTONE_TRACE_METADATA={"agent_name":"gmail_triage","run_type":"test"}',
            ]
        ),
        encoding="utf-8",
    )

    settings = load_settings(env_file=env_file)

    assert settings.openai_api_key == "dotenv-test-keystone-openai-key"
    assert settings.openai_model == "dotenv-test-model"
    assert settings.openai_base_url == "https://keystone.example/v1"
    assert settings.runtime_agent_models["gmail_triage"]["provider"] == "gemini"
    assert settings.runtime_agent_models["gmail_triage"]["model"] == "dotenv-gmail-model"
    assert settings.gemini_api_key == "dotenv-test-gemini-key"
    assert settings.slack_bot_token == "dotenv-test-slack-token"
    assert settings.slack_channel_approvals == "CUNITTEST"
    assert settings.search_provider == "serper"
    assert settings.exa_api_key == "dotenv-test-exa-key"
    assert settings.exa_base_url == "https://exa.example"
    assert settings.exa_service_api_key == "dotenv-test-exa-service-key"
    assert settings.exa_api_key_id == "dotenv-test-exa-key-id"
    assert settings.exa_api_key_name == "default"
    assert settings.exa_monthly_free_request_limit == 1000
    assert settings.serper_api_key == "dotenv-test-serper-key"
    assert settings.tavily_api_key == "dotenv-test-tavily-key"
    assert settings.tavily_base_url == "https://tavily.example"
    assert settings.tavily_search_depth == "fast"
    assert settings.tavily_mcp_link == "https://mcp.tavily.example"
    assert settings.tavily_monthly_credit_limit == 1000
    assert settings.tavily_monthly_soft_limit == 850
    assert settings.tavily_credit_enforcement == "warn"
    assert settings.tavily_usage_path == "/tmp/keystone-tavily-usage.json"
    assert settings.website_extractor == "firecrawl"
    assert settings.firecrawl_api_key == "dotenv-test-firecrawl-key"
    assert settings.firecrawl_base_url == "https://firecrawl.example"
    assert settings.workflow_name == "Dotenv test workflow"
    assert settings.trace_metadata == {"agent_name": "gmail_triage", "run_type": "test"}


def test_live_missing_credentials_stop_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    calls: list[object] = []

    def fail_network(*args: object, **_kwargs: object) -> None:
        calls.append(args)
        raise AssertionError("live credential test must not make a network request")

    monkeypatch.setattr("keystone_agents.tools.search_provider.requests.post", fail_network)

    with pytest.raises(SerperConfigurationError, match="SERPER_API_KEY is required"):
        SerperSearchProvider(live=True).search_web("Curebase", num_results=1)

    assert calls == []


def test_pytest_network_blocker_does_not_echo_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + ("t" * 24)
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    with pytest.raises(AssertionError) as exc:
        socket.getaddrinfo("google.serper.dev", 443)

    assert "Network calls are disabled during pytest" in str(exc.value)
    assert secret not in str(exc.value)


def test_cli_defaults_stay_dry_without_live_test_stage() -> None:
    env: dict[str, str] = {}

    assert cli_default_dry_run(env) is True
    assert cli_live_test_stage_enabled(env) is False
    assert cli_default_live_research(env) is False
    assert cli_default_live_gmail(env) is False
    assert cli_default_live_sdk(env) is False


def test_default_database_url_preserves_legacy_local_default() -> None:
    env: dict[str, str] = {}

    assert default_database_url(env) == "sqlite:///keystone_agents.db"
    assert sqlite_path_from_url(default_database_url(env)) == "keystone_agents.db"


def test_database_url_env_wins_over_keystone_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:////tmp/keystone-explicit.db")
    monkeypatch.setenv("KEYSTONE_HOME", "/tmp/keystone-home")

    assert database_url_from_env() == "sqlite:////tmp/keystone-explicit.db"


def test_keystone_home_sets_runtime_state_database_default(tmp_path) -> None:
    env = {"KEYSTONE_HOME": str(tmp_path / "home")}

    assert runtime_state_dir(env) == tmp_path / "home" / "state"
    assert default_database_url(env) == (
        f"sqlite:///{tmp_path / 'home' / 'state' / 'keystone_agents.db'}"
    )


def test_runtime_state_dir_overrides_keystone_home(tmp_path) -> None:
    env = {
        "KEYSTONE_HOME": str(tmp_path / "home"),
        "KEYSTONE_RUNTIME_STATE_DIR": str(tmp_path / "state"),
    }

    assert runtime_state_dir(env) == tmp_path / "state"
    assert default_database_url(env) == (f"sqlite:///{tmp_path / 'state' / 'keystone_agents.db'}")


def test_cli_defaults_enable_live_test_stage_when_explicitly_configured() -> None:
    env = {
        "KEYSTONE_LIVE_MODE": "true",
        "KEYSTONE_DRY_RUN": "false",
        "KEYSTONE_ENABLE_LIVE_RESEARCH": "true",
        "KEYSTONE_ENABLE_LIVE_GMAIL": "true",
    }

    assert cli_default_dry_run(env) is False
    assert cli_live_test_stage_enabled(env) is True
    assert cli_default_live_research(env) is True
    assert cli_default_live_gmail(env) is True
    assert cli_default_live_sdk(env) is True


def test_cli_environment_force_loads_then_restores_dotenv(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "KEYSTONE_OPENAI_API_KEY=temporary-keystone-key\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    observed: list[bool] = []

    @with_cli_environment(env_file, force_dotenv=True)
    def command() -> None:
        import os

        observed.append(bool(os.getenv("KEYSTONE_OPENAI_API_KEY")))

    command()

    assert observed == [True]
    assert "KEYSTONE_OPENAI_API_KEY" not in __import__("os").environ
